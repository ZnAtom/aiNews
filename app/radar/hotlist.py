#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""热榜采集共享模块：aihot.news 官方 RSS + RSSHub 36kr 快讯。

供 webapp.py（🔥 热榜精选板块）与 sos_daily.py（🧨 破圈推荐小节）共用。
"""

import sqlite3
import time
from pathlib import Path

import feedparser
import httpx

# (显示名, 地址, 是否走系统代理)。aihot 直连可达；36kr 走本机 RSSHub。
HOT_SOURCES = [
    ("aihot 热榜", "https://aihot.news/rss.xml", False),
    ("36kr 快讯", "http://127.0.0.1:1200/36kr/newsflashes", False),
]

# B23：自定义热榜源存 feedback.db 的 hot_custom_sources 表（⚙ 设置面板可增删）。
_DB_PATH = Path(__file__).resolve().parent / "data" / "feedback.db"

_PER_SOURCE_LIMIT = 10
_CACHE_TTL_SEC = 30 * 60  # webapp 进程内缓存 30 分钟

_cache = {"at": 0.0, "items": []}


def load_custom_sources() -> list:
    """读取用户自定义热榜源，返回与 HOT_SOURCES 同构的 (name, url, use_proxy) 列表。

    表不存在 / 库不可用时返回 []（不阻塞热榜主流程）。自定义源默认不走代理。
    """
    try:
        conn = sqlite3.connect(_DB_PATH, timeout=10)
        try:
            rows = conn.execute(
                "SELECT name, url FROM hot_custom_sources ORDER BY created_at, name"
            ).fetchall()
        finally:
            conn.close()
        return [(name, url, False) for name, url in rows]
    except sqlite3.Error:
        return []


def all_sources() -> list:
    """内置源 + 自定义源，供抓取循环统一遍历。"""
    return HOT_SOURCES + load_custom_sources()


def clear_cache() -> None:
    """清空进程内缓存（webapp 增删自定义源后调用，下次抓取立即生效）。"""
    _cache["at"] = 0.0
    _cache["items"] = []


def _norm(text: str) -> str:
    """归一化：去空白 + 小写，用于标题去重比较。"""
    return "".join((text or "").lower().split())


def fetch_hot_items(force: bool = False) -> list:
    """拉取全部热榜源，每源取最新 _PER_SOURCE_LIMIT 条；任一源失败仅跳过。

    webapp 场景下带 30 分钟进程内缓存；sos_daily 每次为独立进程，等价于直取。
    """
    now = time.time()
    if not force and _cache["items"] and now - _cache["at"] < _CACHE_TTL_SEC:
        return _cache["items"]

    items = []
    for name, url, use_proxy in all_sources():  # 内置 + 用户自定义源
        try:
            resp = httpx.get(url, trust_env=use_proxy, timeout=20, follow_redirects=True)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
            got = 0
            for e in feed.entries[:_PER_SOURCE_LIMIT]:
                title = (e.get("title") or "").strip()
                if not title:
                    continue
                items.append(
                    {
                        "source": name,
                        "title": title,
                        "link": (e.get("link") or "").strip(),
                        "published": (e.get("published") or "").strip(),
                    }
                )
                got += 1
            print(f"[热榜] {name}: 获取 {got} 条")
        except Exception as exc:  # 单源失败不影响整体
            print(f"[热榜] {name}: 获取失败，已跳过（{type(exc).__name__}: {exc}）")

    _cache["items"] = items
    _cache["at"] = now
    return items


def _is_dup(title: str, existing_norm: str) -> bool:
    """标题与已有文本互相包含（取前 20 个归一化字符）即视为重复。"""
    t = _norm(title)
    key = t[:20]
    if len(key) < 8:  # 过短标题不做包含判断，避免误伤
        return False
    return key in existing_norm or t in existing_norm


def filter_duplicates(items: list, existing_text: str) -> list:
    """剔除标题已出现在 existing_text（如日报全文）中的条目。"""
    norm = _norm(existing_text)
    return [it for it in items if not _is_dup(it["title"], norm)]


def pick_hot(existing_text: str, total: int = 3, aihot_first: int = 2) -> list:
    """选 1~3 条破圈热榜：默认 aihot 前 2 + 36kr 前 1，先剔除日报已收录。

    某源不足时从另一源剩余条目中补齐；全空返回 []。
    """
    items = filter_duplicates(fetch_hot_items(), existing_text)
    aihot = [it for it in items if it["source"].startswith("aihot")]
    kr = [it for it in items if it["source"].startswith("36kr")]
    picked = aihot[:aihot_first] + kr[: max(total - aihot_first, 0)]
    if len(picked) < total:  # 不足则用剩余条目补齐
        seen = {id(p) for p in picked}
        for it in items:
            if len(picked) >= total:
                break
            if id(it) not in seen:
                picked.append(it)
                seen.add(id(it))
    return picked[:total]


def build_hot_lines(items: list) -> str:
    """把热榜条目渲染成编号列表文本（供 LLM 提示词使用）。"""
    lines = []
    for i, it in enumerate(items, 1):
        pub = f" | {it['published']}" if it.get("published") else ""
        lines.append(f"{i}. [{it['source']}] {it['title']} | {it['link']}{pub}")
    return "\n".join(lines) if lines else "（无可用条目）"
