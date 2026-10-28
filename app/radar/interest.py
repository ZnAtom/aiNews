"""研究兴趣画像：用户提交最近在看的论文（arXiv 链接或论文标题）→
arXiv 官方检索 API 确认候选 → 用户点选 → 后台 LLM 每篇 1 次调用提取
研究方向关键词（3~6 个）→ 存 feedback.db；
api_feed 排序时按关键词命中加分（高于普通关注词）并打 🎯 徽章。

约束：每篇入库论文仅 1 次 LLM 调用；arXiv 检索/元数据均走免费公开接口。
"""

import json
import re
import sqlite3
import threading
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FEEDBACK_DB = Path(__file__).resolve().parent / "data" / "feedback.db"

ARXIV_EXPORT_API = "https://export.arxiv.org/api/query"
ARXIV_ID_RE = re.compile(r"arxiv\.org/(?:abs|pdf)/([0-9]{4}\.[0-9]{4,5})(v\d+)?", re.I)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(FEEDBACK_DB, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


# B26 建库/迁移共用 DDL：兴趣库按用户隔离，旧数据归属 uid=1（admin）
_PAPERS_DDL = """CREATE TABLE IF NOT EXISTS interest_papers (
    user_id INTEGER NOT NULL DEFAULT 1,
    arxiv_id TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    keywords TEXT NOT NULL DEFAULT '[]',
    error TEXT,
    created_at TEXT,
    UNIQUE (user_id, arxiv_id)
)"""
_KW_DDL = """CREATE TABLE IF NOT EXISTS interest_keywords (
    user_id INTEGER NOT NULL DEFAULT 1,
    keyword TEXT NOT NULL,
    hits INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT,
    UNIQUE (user_id, keyword)
)"""


def init_tables() -> None:
    """建表（feedback.db 已由 webapp init_db 创建目录）。"""
    with _conn() as conn:
        conn.execute(_PAPERS_DDL)
        conn.execute(_KW_DDL)
        # B26 迁移：老表缺 user_id 列时重建（旧数据归属 uid=1 admin），幂等
        cols = {r[1] for r in conn.execute("PRAGMA table_info(interest_papers)")}
        if cols and "user_id" not in cols:
            conn.execute("ALTER TABLE interest_papers RENAME TO interest_papers__uid_old")
            conn.execute(_PAPERS_DDL)
            conn.execute(
                """INSERT INTO interest_papers
                       (user_id, arxiv_id, title, url, status, keywords, error, created_at)
                   SELECT 1, arxiv_id, title, url, status, keywords, error, created_at
                   FROM interest_papers__uid_old"""
            )
            conn.execute("DROP TABLE interest_papers__uid_old")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(interest_keywords)")}
        if cols and "user_id" not in cols:
            conn.execute("ALTER TABLE interest_keywords RENAME TO interest_keywords__uid_old")
            conn.execute(_KW_DDL)
            conn.execute(
                """INSERT INTO interest_keywords (user_id, keyword, hits, updated_at)
                   SELECT 1, keyword, hits, updated_at FROM interest_keywords__uid_old"""
            )
            conn.execute("DROP TABLE interest_keywords__uid_old")


# ---------- arXiv 检索（免费官方 export API，不耗 LLM） ----------


def extract_arxiv_id(text: str) -> str | None:
    """从用户输入中提取 arXiv id（支持 abs/pdf 链接或裸 id）。"""
    text = text.strip()
    m = ARXIV_ID_RE.search(text)
    if m:
        return m.group(1)
    if re.fullmatch(r"[0-9]{4}\.[0-9]{4,5}(v\d+)?", text):
        return re.sub(r"v\d+$", "", text)
    return None


def search_arxiv_by_title(query: str, max_results: int = 5) -> list[dict]:
    """按论文标题检索 arXiv，返回候选列表（供用户点选确认）。"""
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    q = urllib.parse.quote(f'ti:"{query.strip()}"')
    url = f"{ARXIV_EXPORT_API}?search_query={q}&max_results={max_results}&sortBy=relevance"
    for use_proxy in (False, True):
        try:
            resp = httpx.get(
                url, trust_env=use_proxy, timeout=20, follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (radar-interest)"},
            )
            resp.raise_for_status()
            root = ET.fromstring(resp.text)
            out = []
            for entry in root.findall("atom:entry", ns):
                eid = (entry.findtext("atom:id", "", ns) or "").strip()
                m = ARXIV_ID_RE.search(eid)
                if not m:
                    continue
                title = re.sub(r"\s+", " ", entry.findtext("atom:title", "", ns)).strip()
                authors = [
                    (a.findtext("atom:name", "", ns) or "").strip()
                    for a in entry.findall("atom:author", ns)
                ]
                out.append(
                    {
                        "arxiv_id": m.group(1),
                        "title": title,
                        "authors": authors[:6],
                        "published": (entry.findtext("atom:published", "", ns) or "")[:10],
                        "url": f"https://arxiv.org/abs/{m.group(1)}",
                    }
                )
            return out
        except Exception:
            continue
    return []


def get_arxiv_summary(arxiv_id: str) -> dict | None:
    """按 id 拉取单篇元数据（标题/摘要/分类），供关键词提取。"""
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    url = f"{ARXIV_EXPORT_API}?id_list={arxiv_id}&max_results=1"
    for use_proxy in (False, True):
        try:
            resp = httpx.get(
                url, trust_env=use_proxy, timeout=20, follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (radar-interest)"},
            )
            resp.raise_for_status()
            root = ET.fromstring(resp.text)
            entry = root.find("atom:entry", ns)
            if entry is None:
                return None
            return {
                "title": re.sub(r"\s+", " ", entry.findtext("atom:title", "", ns)).strip(),
                "abstract": re.sub(
                    r"\s+", " ", entry.findtext("atom:summary", "", ns) or ""
                ).strip()[:3000],
                "primary_category": (entry.find("atom:primary_category", ns) is not None
                                     and entry.find("atom:primary_category", ns).get("term", ""))
                                    or "",
            }
        except Exception:
            continue
    return None


# ---------- 关键词提取（每篇 1 次 LLM 调用） ----------

KW_SYSTEM_PROMPT = """你是论文研究方向分析器。根据给定的论文标题、摘要与分类，提取 3~6 个最能代表该论文研究方向的短关键词。

要求：
1. 关键词用英文专业术语、全小写（如 world model、loco-manipulation、reinforcement learning、dexterous hand）；
2. 每个关键词 1~4 个词，能用于在论文标题/摘要中做子串匹配；
3. 优先选领域方法论级词汇（会反复出现在同类论文中），避免只属于这一篇的专名；
4. 只输出 JSON 数组（如 ["world model","loco-manipulation"]），不要任何多余内容。"""


def extract_keywords(env: dict, title: str, abstract: str, category: str) -> list[str]:
    """1 次 LLM 调用提取研究方向关键词。"""
    from sos_daily import call_llm

    user = f"标题：{title}\n\n分类：{category or '[未给出]'}\n\n摘要：{abstract or '[未给出]'}"
    # 推理模型（glm-flash 系列）reasoning token 会挤占输出，正文虽短也需给足预算
    content, _usage, _model = call_llm(env, KW_SYSTEM_PROMPT, user, max_tokens=4000)
    m = re.search(r"\[.*\]", content, re.S)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    kws = []
    for k in arr if isinstance(arr, list) else []:
        if isinstance(k, str):
            k = k.strip().lower()
            if 2 <= len(k) <= 40 and k not in kws:
                kws.append(k)
    return kws[:6]


# ---------- 库操作 ----------


def upsert_paper(uid: int, arxiv_id: str, title: str, url: str) -> bool:
    """入库一篇兴趣论文（pending，按用户隔离），已存在则跳过。返回是否新插入。"""
    now = datetime.now().isoformat(timespec="seconds")
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO interest_papers (user_id, arxiv_id, title, url, status, keywords, created_at)
               VALUES (?, ?, ?, ?, 'pending', '[]', ?)
               ON CONFLICT(user_id, arxiv_id) DO NOTHING""",
            (uid, arxiv_id, title, url, now),
        )
        return cur.rowcount > 0


def get_paper(uid: int, arxiv_id: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM interest_papers WHERE user_id = ? AND arxiv_id = ?",
            (uid, arxiv_id),
        ).fetchone()
    return dict(row) if row else None


def list_papers(uid: int) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT arxiv_id, title, url, status, keywords, error, created_at "
            "FROM interest_papers WHERE user_id = ? ORDER BY created_at DESC",
            (uid,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["keywords"] = json.loads(d["keywords"])
        except (json.JSONDecodeError, TypeError):
            d["keywords"] = []
        out.append(d)
    return out


def rebuild_keyword_agg(uid: int) -> list[dict]:
    """按当前用户剩余 ready 论文的 keywords 重建聚合表，返回关键词列表。"""
    now = datetime.now().isoformat(timespec="seconds")
    counter: dict[str, int] = {}
    with _conn() as conn:
        rows = conn.execute(
            "SELECT keywords FROM interest_papers WHERE status = 'ready' AND user_id = ?",
            (uid,),
        ).fetchall()
        for r in rows:
            try:
                kws = json.loads(r["keywords"])
            except (json.JSONDecodeError, TypeError):
                continue
            for k in set(kws):
                counter[k] = counter.get(k, 0) + 1
        conn.execute("DELETE FROM interest_keywords WHERE user_id = ?", (uid,))
        for k, hits in counter.items():
            conn.execute(
                """INSERT INTO interest_keywords (user_id, keyword, hits, updated_at)
                   VALUES (?, ?, ?, ?)""",
                (uid, k, hits, now),
            )
    return [{"keyword": k, "hits": h} for k, h in sorted(counter.items(), key=lambda x: -x[1])]


def list_keywords(uid: int) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT keyword, hits FROM interest_keywords WHERE user_id = ? "
            "ORDER BY hits DESC, keyword",
            (uid,),
        ).fetchall()
    return [dict(r) for r in rows]


def remove_paper(uid: int, arxiv_id: str) -> bool:
    with _conn() as conn:
        cur = conn.execute(
            "DELETE FROM interest_papers WHERE user_id = ? AND arxiv_id = ?",
            (uid, arxiv_id),
        )
        deleted = cur.rowcount > 0
    if deleted:
        rebuild_keyword_agg(uid)
    return deleted


# ---------- 后台分析（每篇 1 次 LLM） ----------


def analyze_interest_paper(env: dict, uid: int, arxiv_id: str) -> None:
    """后台线程入口：取元数据 → 提取关键词 → 更新库 + 重建聚合（按用户隔离）。"""
    paper = get_paper(uid, arxiv_id)
    if paper is None:
        return
    try:
        info = get_arxiv_summary(arxiv_id)
        title = info["title"] if info and info.get("title") else paper["title"]
        abstract = (info or {}).get("abstract", "")
        category = (info or {}).get("primary_category", "")
        kws = extract_keywords(env, title, abstract, category)
        now = datetime.now().isoformat(timespec="seconds")
        with _conn() as conn:
            conn.execute(
                "UPDATE interest_papers SET status=?, keywords=?, title=?, error=NULL "
                "WHERE arxiv_id=? AND user_id=?",
                ("ready" if kws else "error", json.dumps(kws, ensure_ascii=False),
                 title, arxiv_id, uid),
            )
        rebuild_keyword_agg(uid)
        print(f"[Interest] {arxiv_id} 分析完成：{kws}（{now}）")
    except Exception as exc:  # noqa: BLE001 - 后台任务兜底
        err = f"{type(exc).__name__}: {exc}"
        with _conn() as conn:
            conn.execute(
                "UPDATE interest_papers SET status='error', error=? "
                "WHERE arxiv_id=? AND user_id=?",
                (err[:300], arxiv_id, uid),
            )
        print(f"[Interest] {arxiv_id} 分析失败：{err}")


def spawn_analysis(env: dict, uid: int, arxiv_id: str) -> None:
    threading.Thread(
        target=analyze_interest_paper, args=(env, uid, arxiv_id), daemon=True
    ).start()
