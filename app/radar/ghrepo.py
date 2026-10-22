#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B5 每日 GitHub 热门项目：Search API 高星检索 + LLM 一句话推荐理由 + 当天缓存。

- 数据源 GitHub Search API（免费无 key，搜索限流 10 req/min）：
  topic 检索 + stars:>500 + pushed 近 30 天（活跃维护中），按 star 数降序；
- 每天推 n 个（n 由 ⚙ 配额设置控制，ghrepos 表按 (day, full_name) 缓存，
  全天固定；配额调大时当天补选，调小只展示前 n 个）；
- 历史推过的项目不再推；
- admin 已点击过的项目（按仓库 URL）不再推；
- 确定性选择 random.Random(day)（star 数前 10 抽样），同日同缓存状态结果一致；
- 失败当天项目位留空，绝不阻断主流程。
"""

import random
import re
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from sos_daily import ENV_PATH, call_llm, load_env

DB_PATH = Path(__file__).resolve().parent / "data" / "feedback.db"

GH_SEARCH = "https://api.github.com/search/repositories"
GH_HEADERS = {"Accept": "application/vnd.github+json", "User-Agent": "ai-news-radar"}
TOPICS = ["ai", "llm", "agents", "robotics", "machine-learning"]
STARS_MIN = 500
ACTIVE_DAYS = 30
PER_PAGE = 30
REASON_SYSTEM = (
    "你是开源项目推荐官。基于给定的项目信息，写一句中文推荐理由（不超过 60 字），"
    "说明它是做什么的、为什么火、适合谁。"
    "只依据给定信息写作，不确定的不写、不编造数据。直接输出理由本身，不要任何前后缀。"
)

_PICK_LOCK = threading.Lock()


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


_GHREPOS_COLS = """(
                day TEXT NOT NULL,
                full_name TEXT NOT NULL,
                url TEXT DEFAULT '',
                description TEXT DEFAULT '',
                stars INTEGER DEFAULT 0,
                language TEXT DEFAULT '',
                reason TEXT DEFAULT '',
                created_at TEXT,
                PRIMARY KEY (day, full_name)
            )"""
_INSERT_GHREPOS = """INSERT OR IGNORE INTO ghrepos
                   (day, full_name, url, description, stars, language, reason, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)"""


def init_table() -> None:
    with _conn() as conn:
        conn.execute(f"CREATE TABLE IF NOT EXISTS ghrepos {_GHREPOS_COLS}")
        # 旧表迁移：day 单主键 → (day, full_name) 复合主键（B11 配额支持每天多个）
        info = conn.execute("PRAGMA table_info(ghrepos)").fetchall()
        pk_cols = [
            r["name"] for r in sorted((r for r in info if r["pk"]), key=lambda r: r["pk"])
        ]
        if pk_cols == ["day"]:
            conn.execute("ALTER TABLE ghrepos RENAME TO ghrepos_old")
            conn.execute(f"CREATE TABLE ghrepos {_GHREPOS_COLS}")
            conn.execute(
                """INSERT OR IGNORE INTO ghrepos
                   SELECT day, full_name, url, description, stars, language,
                          reason, created_at
                   FROM ghrepos_old"""
            )
            conn.execute("DROP TABLE ghrepos_old")


def _gh_search(topic: str, since: str) -> list:
    """GitHub Search：topic + 高星 + 近期活跃，star 数降序；先直连、失败走系统代理。"""
    params = {
        "q": f"topic:{topic} stars:>{STARS_MIN} pushed:>{since}",
        "sort": "stars",
        "order": "desc",
        "per_page": PER_PAGE,
    }
    last_exc: Exception | None = None
    for use_proxy in (False, True):
        try:
            resp = httpx.get(
                GH_SEARCH, params=params, headers=GH_HEADERS,
                trust_env=use_proxy, timeout=25,
            )
            if resp.status_code == 403:
                raise RuntimeError("GitHub Search 限流（403）")
            resp.raise_for_status()
            return (resp.json() or {}).get("items") or []
        except Exception as exc:  # noqa: BLE001 - 换通道重试
            last_exc = exc
    assert last_exc is not None
    raise last_exc


def _candidate_pool() -> list:
    """合并各 topic 候选（要求有简介、未归档/停用），去重按 star 数降序。"""
    since = (datetime.now() - timedelta(days=ACTIVE_DAYS)).date().isoformat()
    seen: set = set()
    pool: list = []
    for topic in TOPICS:
        try:
            rows = _gh_search(topic, since)
        except Exception:  # 单 topic 失败跳过
            continue
        for r in rows:
            name = (r.get("full_name") or "").strip()
            if not name or name.lower() in seen:
                continue
            if r.get("archived") or r.get("disabled"):
                continue
            desc = re.sub(r"\s+", " ", (r.get("description") or "").strip())
            if not desc:
                continue
            seen.add(name.lower())
            pool.append(
                {
                    "full_name": name,
                    "url": (r.get("html_url") or f"https://github.com/{name}").strip(),
                    "description": desc,
                    "stars": r.get("stargazers_count") or 0,
                    "language": (r.get("language") or "").strip(),
                }
            )
    pool.sort(key=lambda x: -x["stars"])
    return pool


def _make_reason(gh: dict) -> str:
    """LLM 一句话理由（≤60 字）；失败退回模板（star 数 + 语言）。"""
    fallback = f"GitHub {gh['stars']} 星热门项目" + (
        f"（{gh['language']}），近期活跃" if gh.get("language") else "，近期活跃"
    )
    try:
        env = load_env(ENV_PATH)
        user = (
            f"项目：{gh['full_name']}\n"
            f"Star 数：{gh['stars']}\n"
            f"语言：{gh.get('language') or '未知'}\n"
            f"简介：{(gh.get('description') or '（无）')[:300]}"
        )
        content, _usage, _model = call_llm(env, REASON_SYSTEM, user, max_tokens=2000)
        reason = (content or "").strip().splitlines()[0].strip().strip('"“”')
        return reason[:120] or fallback
    except Exception:  # LLM 失败不阻断，用模板理由
        return fallback


def pick_ghrepos(day: str, n: int = 1) -> list:
    """当天 GitHub 热门项目（最多 n 个）：ghrepos 表按 (day, full_name) 缓存，
    全天固定；配额调大时当天补选；无候选/接口失败返回已缓存部分（可能为空列表）。"""
    init_table()

    def _cached() -> list:
        with _conn() as conn:
            rows = conn.execute(
                "SELECT * FROM ghrepos WHERE day = ? ORDER BY rowid LIMIT ?",
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
            pushed_names = {
                r["full_name"] for r in conn.execute("SELECT full_name FROM ghrepos")
            }
            clicked_urls = {
                r["url"]
                for r in conn.execute(
                    "SELECT DISTINCT url FROM clicks WHERE role = 'admin' AND url != ''"
                )
            }

        pool = [
            g
            for g in _candidate_pool()
            if g["full_name"] not in pushed_names and g["url"] not in clicked_urls
        ]
        picked: list = []
        if pool:
            need = n - len(got)
            # star 数 top10 里按天随机补选（确定性：同日同缓存状态触发结果一致）
            for gh in random.Random(day).sample(pool[:10], min(need, len(pool[:10]))):
                gh = dict(gh)
                gh["reason"] = _make_reason(gh)
                with _conn() as conn:
                    conn.execute(
                        _INSERT_GHREPOS,
                        (
                            day, gh["full_name"], gh["url"], (gh["description"] or "")[:500],
                            gh["stars"], gh["language"], gh["reason"],
                            datetime.now().isoformat(timespec="seconds"),
                        ),
                    )
                picked.append(gh)
        return got + picked
