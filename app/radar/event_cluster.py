#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B13 事件聚类：同一板块内把"多家媒体报道同一件事"的条目归为一组并打标。

设计原则：
- 纯注解不删除：只给条目打 cluster（主条目，含 mates 列表）/ cluster_member
  （成员条目）标记，条目仍留在板块里，不影响 record_pushed / annotate_repush /
  filter_clicked / archive_from_feed 链路；
- 同板块内聚类：论文与新闻报道不算同一事件，跨板块不聚；
- 规则预聚 + LLM 复核：difflib ratio + 中文 2-gram/英文词 Jaccard 的并查集
  候选组（size>=2）→ 一次 LLM 调用判定每组是否同一事件并给组名；
  LLM 失败则本次不折叠（返回 None 不缓存，下次请求自动重试）——
  规则阈值偏宽只是粗筛，没有 LLM 裁决宁可不聚也不错聚；
- 当天缓存：event_clusters 表（day 主键 + md_mtime 校验日报是否变更），
  构建放后台线程（webapp 侧），当次请求不折叠、刷新后生效（同 B3 论文预生成模式）。
"""

import difflib
import json
import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from sos_daily import ENV_PATH, call_llm, load_env

DB_PATH = Path(__file__).resolve().parent / "data" / "feedback.db"

# 规则预聚阈值：低于它们不成组（宁可漏聚，错聚交给 LLM 复核兜底）
RATIO_THRESHOLD = 0.45  # difflib 序列相似度
JACCARD_THRESHOLD = 0.35  # 分词 Jaccard
MIN_TITLE_LEN = 8  # 归一化后太短的标题不参与两两比对（短标题 2-gram 极易碰撞）
MAX_GROUP_SIZE = 12  # 单组条目数上限，防 prompt 爆炸

CLUSTER_SYSTEM = (
    "你是新闻编辑，负责判断多组候选条目是否在报道同一个事件。"
    "判定标准：同一产品发布、同一论文发表、同一公司新闻、同一融资、同一事故才算同一事件；"
    "标题相似但主体不同（不同论文、不同版本、不同公司）不算；拿不准的算不同（宁可漏聚不可错聚）。"
    "只输出 JSON 数组，不要输出任何其他内容："
    '[{"group": 1, "same": true, "label": "不超过18字的事件名"}, ...]，'
    "每组都要有返回项，same 为 false 时 label 给空字符串。"
)

_BUILD_LOCK = threading.Lock()

_TABLE_COLS = """(
                day TEXT PRIMARY KEY,
                md_mtime REAL NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT
            )"""


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_table() -> None:
    with _conn() as conn:
        conn.execute(f"CREATE TABLE IF NOT EXISTS event_clusters {_TABLE_COLS}")


# ---------------- 文本相似度 ----------------

_CN_SEG_RE = re.compile(r"[\u4e00-\u9fff]+")
_EN_WORD_RE = re.compile(r"[a-z][a-z0-9+#.\-]{1,}")


def _norm(title: str) -> str:
    """小写、去空白与标点（保留中英文与数字），用于 difflib ratio。"""
    return re.sub(r"[\s\W_]+", "", title.lower())


def _tokens(title: str) -> set:
    """中文 2-gram + 英文单词（len>=2），用于 Jaccard。"""
    t = title.lower()
    toks = set(_EN_WORD_RE.findall(t))
    for seg in _CN_SEG_RE.findall(t):
        if len(seg) >= 2:
            toks.update(seg[i : i + 2] for i in range(len(seg) - 1))
        else:
            toks.add(seg)
    return toks


def _similar(na: str, nb: str, ta: set, tb: set) -> bool:
    if difflib.SequenceMatcher(None, na, nb).ratio() >= RATIO_THRESHOLD:
        return True
    union = ta | tb
    if union:
        return len(ta & tb) / len(union) >= JACCARD_THRESHOLD
    return False


# ---------------- 规则预聚（并查集） ----------------

def _find(parent: dict, x: str) -> str:
    while parent[x] != x:
        parent[x] = parent[parent[x]]
        x = parent[x]
    return x


def _union(parent: dict, a: str, b: str) -> None:
    ra, rb = _find(parent, a), _find(parent, b)
    if ra != rb:
        parent[rb] = ra


def rule_groups(entries: list) -> list:
    """同板块内两两相似度 → union-find 候选组。

    entries: [{"section": name, "items": [{"anchor", "title", ...}]}]
    返回: [{"section": name, "anchors": [主条目, 成员...]}]，主条目取组内第一个。
    """
    out = []
    for sec in entries:
        items = [it for it in sec.get("items", []) if it.get("anchor") and it.get("title")]
        norm = {}
        toks = {}
        for it in items:
            norm[it["anchor"]] = _norm(it["title"])
            toks[it["anchor"]] = _tokens(it["title"])
        parent = {it["anchor"]: it["anchor"] for it in items}
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                a, b = items[i]["anchor"], items[j]["anchor"]
                if len(norm[a]) < MIN_TITLE_LEN or len(norm[b]) < MIN_TITLE_LEN:
                    continue
                if _similar(norm[a], norm[b], toks[a], toks[b]):
                    _union(parent, a, b)
        groups: dict = {}
        for it in items:
            groups.setdefault(_find(parent, it["anchor"]), []).append(it["anchor"])
        for anchors in groups.values():
            if len(anchors) >= 2:
                out.append(
                    {"section": sec.get("section") or "", "anchors": anchors[:MAX_GROUP_SIZE]}
                )
    return out


def build_payload(entries: list) -> list | None:
    """规则预聚 + LLM 复核 → 聚类 payload（可 JSON 序列化存储）。

    返回 None：LLM 复核失败（调用方不应缓存，下次请求重试）；
    返回 []：无候选组（正常空结果，可缓存）；
    返回 [{...}, ...]：payload: [{"section", "label", "anchors": [主, 成员...]}]
    """
    groups = rule_groups(entries)
    if not groups:
        return []

    # 标题映射（anchor -> item），LLM 复核要用
    title_map: dict = {}
    for sec in entries:
        for it in sec.get("items", []):
            if it.get("anchor"):
                title_map[it["anchor"]] = it

    reviewed = _llm_review_groups(groups, title_map)
    if reviewed is None:
        return None
    payload = []
    for gi, g in enumerate(groups):
        same, label = reviewed[gi]
        if not same:
            continue
        payload.append(
            {"section": g["section"], "label": label, "anchors": list(g["anchors"])}
        )
    return payload


def _llm_review_groups(groups: list, title_map: dict) -> list | None:
    """一次 LLM 调用判定全部候选组，返回 [(same, label), ...]（与 groups 对齐）；
    失败返回 None（调用方回退规则结果）。"""
    lines = []
    for gi, g in enumerate(groups, 1):
        lines.append(f"第 {gi} 组:")
        for anchor in g["anchors"]:
            it = title_map.get(anchor, {})
            src = it.get("source") or "未知来源"
            lines.append(f"- [{src}] {it.get('title') or anchor}")
    user = "\n".join(lines)
    try:
        env = load_env(ENV_PATH)
        content, _, _ = call_llm(env, CLUSTER_SYSTEM, user, max_tokens=1500)
        m = re.search(r"\[.*\]", content, re.S)
        if not m:
            return None
        arr = json.loads(m.group(0))
        result = []
        for gi in range(len(groups)):
            same, label = True, ""
            for row in arr:
                if isinstance(row, dict) and row.get("group") == gi + 1:
                    same = bool(row.get("same", True))
                    label = str(row.get("label") or "").strip()[:24]
                    break
            result.append((same, label))
        return result
    except Exception:
        return None


# ---------------- 缓存 ----------------

def load_cached(day: str, md_mtime: float) -> list | None:
    """命中返回 payload；未命中 / 日报已变更返回 None。"""
    try:
        with _conn() as conn:
            row = conn.execute(
                "SELECT md_mtime, payload FROM event_clusters WHERE day = ?", (day,)
            ).fetchone()
    except sqlite3.Error:
        return None
    if row is None or abs(row["md_mtime"] - md_mtime) > 0.5:
        return None
    try:
        return json.loads(row["payload"])
    except (TypeError, ValueError):
        return None


def save_cached(day: str, md_mtime: float, payload: list) -> None:
    with _BUILD_LOCK:
        with _conn() as conn:
            conn.execute(
                """INSERT INTO event_clusters (day, md_mtime, payload, created_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(day) DO UPDATE SET md_mtime = excluded.md_mtime,
                                                   payload = excluded.payload,
                                                   created_at = excluded.created_at""",
                (day, md_mtime, json.dumps(payload, ensure_ascii=False),
                 datetime.now().isoformat(timespec="seconds")),
            )


# ---------------- 注解（纯打标，不删条目） ----------------

def apply_clusters(sections: list, payload: list) -> None:
    """把聚类结果注解到 sections 的条目上。

    主条目: item["cluster"] = {"label", "mates": [{anchor,title,source,url}, ...]}
    成员条目: item["cluster_member"] = {"label", "main_anchor", "main_title"}
    锚点在当前 sections 里找不到的成员（如已被"已点击"过滤）自动跳过；
    全部成员都找不到时主条目也不打标。
    """
    if not payload:
        return
    index: dict = {}
    for sec in sections:
        for it in sec.get("items", []):
            if it.get("anchor"):
                index[it["anchor"]] = it
    for cl in payload:
        anchors = cl.get("anchors") or []
        if len(anchors) < 2:
            continue
        main_anchor = anchors[0]
        main = index.get(main_anchor)
        if main is None:
            continue
        mates = []
        for a in anchors[1:]:
            m = index.get(a)
            if m is not None:
                mates.append(
                    {
                        "anchor": a,
                        "title": m.get("title") or "",
                        "source": m.get("source") or "",
                        "url": m.get("url") or "",
                    }
                )
        if not mates:
            continue
        main["cluster"] = {"label": cl.get("label") or "", "mates": mates}
        for a in anchors[1:]:
            m = index.get(a)
            if m is not None:
                m["cluster_member"] = {
                    "label": cl.get("label") or "",
                    "main_anchor": main_anchor,
                    "main_title": main.get("title") or "",
                }
