#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B4 经典论文回看：OpenAlex 高引检索 + LLM 一句话理由 + 当天缓存。

- 查询词优先取研究兴趣画像关键词（interest_keywords 表），无则用默认词表；
- 每天推 n 篇（n 由 ⚙ 配额设置控制，classics 表按 (day, paper_id) 缓存，
  全天固定；配额调大时当天补选，调小只展示前 n 条）；
- 历史推过的经典不再推；
- admin 已点击过的论文（按 arxiv_id）不再推；
- 只推带 arXiv 版的论文（保证走 B3 解读缓存与 /paper 详情页链路）；
- 数据源：OpenAlex（免费、无 key、限流宽松；Semantic Scholar 免费共享池实测
  持续 429 不可用，故弃用）；失败当天经典位留空，绝不阻断主流程。
"""

import random
import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

import httpx

from interest import list_keywords
from sos_daily import ENV_PATH, call_llm, load_env

DB_PATH = Path(__file__).resolve().parent / "data" / "feedback.db"

OPENALEX_WORKS = "https://api.openalex.org/works"
OPENALEX_PARAMS = {
    "sort": "cited_by_count:desc",
    "per_page": 50,
}
ARXIV_IN_URL_RE = re.compile(r"arxiv\.org/(?:abs|pdf)/([0-9]{4}\.[0-9]{4,5})", re.I)
ARXIV_DOI_RE = re.compile(r"10\.48550/arxiv\.([0-9]{4}\.[0-9]{4,5})", re.I)
DEFAULT_QUERIES = [
    "embodied AI",
    "robot learning",
    "large language model agents",
    "vision language model",
    "reinforcement learning",
]
REASON_SYSTEM = (
    "你是论文推荐官。基于给定的论文信息，写一句中文推荐理由（不超过 60 字），"
    "说明它为什么值得今天的 AI 从业者回看（提出了什么方法 / 影响力如何）。"
    "只依据给定信息写作，不确定的不写、不编造数据。直接输出理由本身，不要任何前后缀。"
)

_PICK_LOCK = threading.Lock()


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


_CLASSICS_COLS = """(
                day TEXT NOT NULL,
                paper_id TEXT NOT NULL,
                arxiv_id TEXT NOT NULL,
                title TEXT NOT NULL,
                url TEXT DEFAULT '',
                abstract TEXT DEFAULT '',
                year INTEGER,
                citation_count INTEGER DEFAULT 0,
                venue TEXT DEFAULT '',
                reason TEXT DEFAULT '',
                created_at TEXT,
                PRIMARY KEY (day, paper_id)
            )"""
_INSERT_CLASSICS = """INSERT OR IGNORE INTO classics
                   (day, paper_id, arxiv_id, title, url, abstract, year,
                    citation_count, venue, reason, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""


def init_table() -> None:
    with _conn() as conn:
        conn.execute(f"CREATE TABLE IF NOT EXISTS classics {_CLASSICS_COLS}")
        # 旧表迁移：day 单主键 → (day, paper_id) 复合主键（B11 配额支持每天多篇）
        info = conn.execute("PRAGMA table_info(classics)").fetchall()
        pk_cols = [
            r["name"] for r in sorted((r for r in info if r["pk"]), key=lambda r: r["pk"])
        ]
        if pk_cols == ["day"]:
            conn.execute("ALTER TABLE classics RENAME TO classics_old")
            conn.execute(f"CREATE TABLE classics {_CLASSICS_COLS}")
            conn.execute(
                """INSERT OR IGNORE INTO classics
                   SELECT day, paper_id, arxiv_id, title, url, abstract, year,
                          citation_count, venue, reason, created_at
                   FROM classics_old"""
            )
            conn.execute("DROP TABLE classics_old")


def _openalex_search(query: str) -> list:
    """OpenAlex 检索（引用数降序）；先直连、失败走系统代理。"""
    params = dict(OPENALEX_PARAMS, search=query)
    last_exc: Exception | None = None
    for use_proxy in (False, True):
        try:
            resp = httpx.get(
                OPENALEX_WORKS, params=params, trust_env=use_proxy, timeout=25
            )
            if resp.status_code == 429:
                raise RuntimeError("OpenAlex 限流（429）")
            resp.raise_for_status()
            return (resp.json() or {}).get("results") or []
        except Exception as exc:  # noqa: BLE001 - 换通道重试
            last_exc = exc
    assert last_exc is not None
    raise last_exc


def _inv2abstract(inv: dict | None) -> str:
    """OpenAlex abstract_inverted_index → 原文摘要。"""
    if not inv:
        return ""
    pos: dict = {}
    for word, idxs in inv.items():
        for i in idxs:
            pos[i] = word
    return " ".join(pos[i] for i in sorted(pos))


def _extract_arxiv_id(w: dict) -> str:
    """从 OpenAlex work 的 DOI 与 locations 提取 arXiv id（无则空串）。"""
    m = ARXIV_DOI_RE.search(w.get("doi") or "")
    if m:
        return m.group(1)
    for loc in w.get("locations") or []:
        m = ARXIV_IN_URL_RE.search(loc.get("landing_page_url") or "")
        if m:
            return m.group(1)
    return ""


def _candidate_pool(queries: list) -> list:
    """合并各查询词候选（要求有 arXiv 版 + 标题 + 摘要），按引用数降序。"""
    seen: set = set()
    pool: list = []
    for q in queries:
        try:
            rows = _openalex_search(q)
        except Exception:  # 单查询词失败跳过
            continue
        for w in rows:
            aid = _extract_arxiv_id(w)
            title = re.sub(r"\s+", " ", (w.get("title") or w.get("display_name") or "").strip())
            if not aid or not title or title.lower() in seen:
                continue
            seen.add(title.lower())
            src = ((w.get("primary_location") or {}).get("source") or {})
            pool.append(
                {
                    "paper_id": (w.get("id") or "").rsplit("/", 1)[-1],
                    "arxiv_id": aid,
                    "title": title,
                    "url": f"https://arxiv.org/abs/{aid}",
                    "abstract": _inv2abstract(w.get("abstract_inverted_index")).strip(),
                    "year": w.get("publication_year"),
                    "citation_count": w.get("cited_by_count") or 0,
                    "venue": (src.get("display_name") or "").strip(),
                }
            )
    pool.sort(key=lambda x: -x["citation_count"])
    return pool


def _make_reason(cl: dict) -> str:
    """LLM 一句话理由（≤60 字）；失败退回模板（引用数 + 年份）。"""
    fallback = f"被引 {cl['citation_count']} 次" + (
        f"（{cl['year']} 年发表）" if cl.get("year") else ""
    )
    try:
        env = load_env(ENV_PATH)
        user = (
            f"标题：{cl['title']}\n"
            f"年份：{cl.get('year') or '未知'}\n"
            f"被引次数：{cl['citation_count']}\n"
            f"发表处：{cl.get('venue') or '未知'}\n"
            f"摘要：{(cl.get('abstract') or '（无）')[:500]}"
        )
        content, _usage, _model = call_llm(env, REASON_SYSTEM, user, max_tokens=2000)
        reason = (content or "").strip().splitlines()[0].strip().strip('"“”')
        return reason[:120] or fallback
    except Exception:  # LLM 失败不阻断，用模板理由
        return fallback


def pick_classics(day: str, n: int = 1) -> list:
    """当天经典论文（最多 n 篇）：classics 表按 (day, paper_id) 缓存，全天固定；
    配额调大时当天补选；无候选/接口失败返回已缓存部分（可能为空列表）。"""
    init_table()

    def _cached() -> list:
        with _conn() as conn:
            rows = conn.execute(
                "SELECT * FROM classics WHERE day = ? ORDER BY rowid LIMIT ?",
                (day, n),
            ).fetchall()
            return [dict(r) for r in rows]

    got = _cached()
    if len(got) >= n:
        return got

    with _PICK_LOCK:  # 防并发重复选 + 重复 LLM 调用
        got = _cached()
        if len(got) >= n:
            return got
        with _conn() as conn:
            pushed_aids = {
                r["arxiv_id"] for r in conn.execute("SELECT DISTINCT arxiv_id FROM classics")
            }
            clicked_aids = {
                r["arxiv_id"]
                for r in conn.execute(
                    "SELECT DISTINCT arxiv_id FROM clicks WHERE role = 'admin' AND arxiv_id != ''"
                )
            }

        queries = [k["keyword"] for k in list_keywords()][:3] or DEFAULT_QUERIES
        pool = [
            c
            for c in _candidate_pool(queries)
            if c["arxiv_id"] not in pushed_aids
            and c["arxiv_id"] not in clicked_aids
            and c["abstract"]
        ]
        picked: list = []
        if pool:
            need = n - len(got)
            # 引用数 top10 里按天随机补选（确定性：同日同缓存状态触发结果一致）
            for cl in random.Random(day).sample(pool[:10], min(need, len(pool[:10]))):
                cl = dict(cl)
                cl["reason"] = _make_reason(cl)
                with _conn() as conn:
                    conn.execute(
                        _INSERT_CLASSICS,
                        (
                            day, cl["paper_id"], cl["arxiv_id"], cl["title"], cl["url"],
                            (cl["abstract"] or "")[:2000], cl["year"], cl["citation_count"],
                            cl["venue"], cl["reason"],
                            datetime.now().isoformat(timespec="seconds"),
                        ),
                    )
                picked.append(cl)
        return got + picked
