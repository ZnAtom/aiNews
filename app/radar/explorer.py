#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B15 🧪 探索频道：探索-利用闭环，自动发现值得长期订阅的信息源。

机制：每日从源池确定性选 1 个"实验源"，经本地中转 feed 进入 Horizon 全管线
（试验窗 3 天、digest 配额每天 ≤2 条），用 Web 端点击/投票反馈做裁决：

    👎≥2                  → 冷却 45 天
    点击≥2 或 👍≥1        → 全自动转正（追加 Horizon config 正式源）
    仅点击 1 次           → 延长一轮（再试 3 天，最多延到第 3 轮）
    窗口内未真实曝光      → 延长一轮（防上游断更误杀，最多 3 轮）
    视而不见              → 冷却 45 天

数据：feedback.db 的 explorer_pool / explorer_daily 表；
中转：webapp.py 的 GET /explorer/feed.xml 调 build_relay_feed()；
转正：写 app/Horizon/data/config.json（原子写 + .bak），次日 08:00 生效。
"""

import hashlib
import json
import random
import re
import shutil
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

import feedparser
import httpx

RADAR_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = RADAR_DIR.parents[1]
DB_PATH = RADAR_DIR / "data" / "feedback.db"
HORIZON_CONFIG = PROJECT_ROOT / "app" / "Horizon" / "data" / "config.json"

TRIAL_DAYS = 3           # 试验窗：3 天
TRIAL_ITEMS = 4          # 中转 feed 每天吐出的条数（digest 配额另限 2 条/天）
COOLDOWN_DAYS = 45       # 视而不见 / 差评的冷却期
MAX_ROUNDS = 3           # 未真实曝光时的最大试验轮数
PROMOTE_CLICKS = 2       # 窗口内点击 ≥2 次转正
RELAY_TTL_SEC = 30 * 60  # 中转输出进程内缓存

# 探索源池：20 个精选源（doc/rss-sources.md，2026-10-22 国内网络全部实测）。
# (源名, RSS 地址, category, profile, 是否需代理)。转正即按此写入 Horizon config。
POOL = [
    ("IEEE Spectrum Robotics", "https://spectrum.ieee.org/feeds/topic/robotics.rss", "ai-news", "ai-news", False),
    ("OpenAI News", "https://openai.com/news/rss.xml", "ai-news", "ai-news", False),
    ("Google DeepMind", "https://deepmind.google/blog/rss.xml", "ai-news", "ai-news", False),
    ("Simon Willison", "https://simonwillison.net/atom/everything/", "ai-news", "ai-news", False),
    ("Hugging Face Blog", "https://huggingface.co/blog/feed.xml", "ai-news", "ai-news", True),
    ("The Verge AI", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml", "ai-news", "ai-news", False),
    ("Ars Technica AI", "https://arstechnica.com/ai/feed/", "ai-news", "ai-news", False),
    ("MIT Technology Review AI", "https://www.technologyreview.com/topic/artificial-intelligence/feed", "ai-news", "ai-news", False),
    ("Latent Space", "https://www.latent.space/feed", "ai-news", "ai-news", False),
    ("Interconnects", "https://www.interconnects.ai/feed", "ai-news", "ai-news", False),
    ("Ahead of AI", "https://magazine.sebastianraschka.com/feed", "ai-news", "ai-news", False),
    ("Import AI", "https://importai.substack.com/feed", "ai-news", "ai-news", True),
    ("量子位", "https://www.qbitai.com/feed", "ai-news", "ai-news", False),
    ("阮一峰科技爱好者周刊", "https://www.ruanyifeng.com/blog/atom.xml", "ai-news", "ai-news", False),
    ("GitHub Blog", "https://github.blog/feed/", "ai-news", "ai-news", False),
    ("arXiv cs.CL", "https://rss.arxiv.org/rss/cs.CL", "papers", "papers", False),
    ("arXiv cs.AI", "https://rss.arxiv.org/rss/cs.AI", "papers", "papers", False),
    ("Reddit r/robotics", "https://www.reddit.com/r/robotics/.rss", "ai-news", "ai-news", True),
    ("Reddit r/LocalLLaMA", "https://www.reddit.com/r/LocalLLaMA/.rss", "ai-news", "ai-news", True),
    ("Robohub", "https://robohub.org/feed/", "ai-news", "ai-news", True),
]

_relay_cache = {"key": "", "at": 0.0, "xml": b""}


def get_conn() -> sqlite3.Connection:
    """新建 SQLite 连接（每操作一连接，与 webapp.get_conn 同模式）。"""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def init_tables() -> None:
    """建探索频道两张表（幂等；webapp.init_db 与手动脚本均可调）。"""
    with get_conn() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS explorer_pool (
                source TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                category TEXT NOT NULL,
                profile TEXT NOT NULL,
                use_proxy INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'candidate',
                trial_start TEXT,
                trial_end TEXT,
                round INTEGER NOT NULL DEFAULT 0,
                cooldown_until TEXT,
                promoted_at TEXT,
                note TEXT DEFAULT '',
                updated_at TEXT
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS explorer_daily (
                day TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                items_served INTEGER DEFAULT 0,
                updated_at TEXT
            )"""
        )


def ensure_pool() -> None:
    """把 POOL 同步进 explorer_pool（新增源插入；url/代理标注以代码为准）。"""
    now = now_iso()
    with get_conn() as conn:
        for name, url, category, profile, use_proxy in POOL:
            conn.execute(
                """INSERT INTO explorer_pool
                   (source, url, category, profile, use_proxy, status, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'candidate', ?)
                   ON CONFLICT(source) DO UPDATE SET
                       url = excluded.url,
                       use_proxy = excluded.use_proxy,
                       updated_at = excluded.updated_at""",
                (name, url, category, profile, int(use_proxy), now),
            )


# ---------------- 启停开关（app_settings 表，默认开） ----------------


def get_explorer_enabled() -> bool:
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT value FROM app_settings WHERE key = 'explorer_enabled'"
            ).fetchone()
    except sqlite3.Error:
        return True
    if row is None:
        return True
    return (row["value"] or "1").strip().lower() not in ("0", "false", "off")


def set_explorer_enabled(enabled: bool) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO app_settings (key, value, updated_at)
               VALUES ('explorer_enabled', ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                               updated_at = excluded.updated_at""",
            ("1" if enabled else "0", now_iso()),
        )


# ---------------- 试验统计与裁决 ----------------


def trial_stats(source: str, start: str, end: str) -> dict:
    """试验窗内的实时表现：admin 点击数、👍/👎 数、真实曝光条数（pushed）。"""
    with get_conn() as conn:
        clicks = conn.execute(
            "SELECT COUNT(*) AS c FROM clicks WHERE source = ? AND role = 'admin'"
            " AND day BETWEEN ? AND ?",
            (source, start, end),
        ).fetchone()["c"]
        v = conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN vote = 1 THEN 1 ELSE 0 END), 0) AS up,"
            " COALESCE(SUM(CASE WHEN vote = -1 THEN 1 ELSE 0 END), 0) AS down"
            " FROM votes WHERE source = ? AND day BETWEEN ? AND ?",
            (source, start, end),
        ).fetchone()
        pushed = conn.execute(
            "SELECT COUNT(*) AS c FROM pushed WHERE source = ? AND day BETWEEN ? AND ?",
            (source, start, end),
        ).fetchone()["c"]
    return {"clicks": clicks, "votes_up": v["up"], "votes_down": v["down"], "pushed": pushed}


def _promote(conn, row: sqlite3.Row, day: str, reason: str) -> bool:
    """转正：更新池状态 + 追加 Horizon config 正式源（原子写 + .bak）。

    返回是否实际写入 config（同名/同 URL 已存在则只更新池状态）。
    """
    conn.execute(
        """UPDATE explorer_pool SET status = 'promoted', promoted_at = ?,
           trial_start = NULL, trial_end = NULL, cooldown_until = NULL,
           note = ?, updated_at = ? WHERE source = ?""",
        (day, reason, now_iso(), row["source"]),
    )
    cfg = json.loads(HORIZON_CONFIG.read_text(encoding="utf-8"))
    rss = cfg.setdefault("sources", {}).setdefault("rss", [])
    if any(
        f.get("name") == row["source"] or f.get("url") == row["url"] for f in rss
    ):
        return False
    rss.append(
        {
            "name": row["source"],
            "url": row["url"],
            "enabled": True,
            "category": row["category"],
            "profile": row["profile"],
        }
    )
    bak = HORIZON_CONFIG.parent / (HORIZON_CONFIG.name + ".bak")
    shutil.copy2(HORIZON_CONFIG, bak)
    HORIZON_CONFIG.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return True


def resolve_verdicts(day: str) -> list:
    """裁决试验窗已结束的源（每天首次取源时自动跑；返回裁决记录供 UI 展示）。"""
    verdicts = []
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM explorer_pool WHERE status = 'trial'"
            " AND trial_end IS NOT NULL AND trial_end < ?",
            (day,),
        ).fetchall()
        for row in rows:
            src = row["source"]
            stats = trial_stats(src, row["trial_start"], row["trial_end"])
            if stats["votes_down"] >= 2:
                status, note = "cooling", f"👎×{stats['votes_down']} 差评"
            elif stats["clicks"] >= PROMOTE_CLICKS or stats["votes_up"] >= 1:
                status, note = "promoted", ""
            elif stats["pushed"] == 0 and row["round"] < MAX_ROUNDS:
                status, note = "extend", "未曝光（上游断更或未开 Web）"
            elif stats["clicks"] == 1 and row["round"] == 1:
                status, note = "extend", "点击 1 次，观察中"
            else:
                status, note = "cooling", "试验窗视而不见"
            if status == "promoted":
                _promote(conn, row, day, f"试验 {stats['clicks']} 点击 / {stats['votes_up']} 赞，自动转正")
                verdicts.append({"source": src, "verdict": "promoted", "stats": stats, "note": ""})
            elif status == "extend":
                end = (date.fromisoformat(day) + timedelta(days=TRIAL_DAYS - 1)).isoformat()
                conn.execute(
                    """UPDATE explorer_pool SET trial_start = ?, trial_end = ?,
                       round = round + 1, note = ?, updated_at = ? WHERE source = ?""",
                    (day, end, note, now_iso(), src),
                )
                verdicts.append({"source": src, "verdict": "extended", "stats": stats, "note": note})
            else:
                until = (date.fromisoformat(day) + timedelta(days=COOLDOWN_DAYS)).isoformat()
                conn.execute(
                    """UPDATE explorer_pool SET status = 'cooling', cooldown_until = ?,
                       trial_start = NULL, trial_end = NULL, note = ?, updated_at = ?
                       WHERE source = ?""",
                    (until, note, now_iso(), src),
                )
                verdicts.append({"source": src, "verdict": "cooling", "stats": stats, "note": note})
    return verdicts


def today_pick(day: str | None = None) -> dict | None:
    """确定今日实验源（同日多次调用结果一致；选源动作只发生一次）。

    优先级：explorer_daily 冻结记录 → 试验中源继续 → 按权重 1/(1+round)
    从候选（未试 / 冷却到期）确定性随机选一个并开启 3 天试验窗。
    开关关闭时返回 None 且不做任何状态变更。
    """
    day = day or date.today().isoformat()
    if not get_explorer_enabled():
        return None
    resolve_verdicts(day)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT source FROM explorer_daily WHERE day = ?", (day,)
        ).fetchone()
        if row is not None:
            src = row["source"]
        else:
            trial = conn.execute(
                "SELECT source FROM explorer_pool WHERE status = 'trial' AND trial_end >= ?",
                (day,),
            ).fetchone()
            if trial is not None:
                src = trial["source"]
            else:
                cands = conn.execute(
                    "SELECT source, round FROM explorer_pool WHERE status = 'candidate'"
                    " OR (status = 'cooling' AND (cooldown_until IS NULL OR cooldown_until <= ?))",
                    (day,),
                ).fetchall()
                if not cands:
                    return None
                weights = [1.0 / (1 + c["round"]) for c in cands]
                src = random.Random(day).choices(cands, weights=weights, k=1)[0]["source"]
                end = (date.fromisoformat(day) + timedelta(days=TRIAL_DAYS - 1)).isoformat()
                conn.execute(
                    """UPDATE explorer_pool SET status = 'trial', trial_start = ?,
                       trial_end = ?, round = round + 1, cooldown_until = NULL,
                       updated_at = ? WHERE source = ?""",
                    (day, end, now_iso(), src),
                )
            conn.execute(
                """INSERT OR IGNORE INTO explorer_daily (day, source, items_served, updated_at)
                   VALUES (?, ?, 0, ?)""",
                (day, src, now_iso()),
            )
        pool_row = conn.execute(
            "SELECT * FROM explorer_pool WHERE source = ?", (src,)
        ).fetchone()
        served = conn.execute(
            "SELECT items_served FROM explorer_daily WHERE day = ?", (day,)
        ).fetchone()["items_served"]
    picked = dict(pool_row) if pool_row else None
    if picked is not None:
        picked["items_served"] = served
    return picked


# ---------------- 中转 feed ----------------


def _strip_html(text: str, limit: int = 300) -> str:
    """摘要粗清洗：去 HTML 标签 + 截断（描述进 RSS 只做转义不内嵌富文本）。"""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def fetch_upstream(row: dict, limit: int = TRIAL_ITEMS) -> list:
    """抓今日探索源的上游 RSS（按池标注决定是否走系统代理）。"""
    resp = httpx.get(
        row["url"], trust_env=bool(row["use_proxy"]), timeout=20, follow_redirects=True
    )
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)
    items = []
    for e in feed.entries[:limit]:
        title = (e.get("title") or "").strip()
        link = (e.get("link") or "").strip()
        if not title or not link:
            continue
        summary = _strip_html(e.get("summary") or "")
        if not summary:
            # 部分源（如 HF Blog）摘要在 content 字段
            content = e.get("content")
            if content and isinstance(content, list):
                summary = _strip_html(content[0].get("value") or "")
        items.append({"title": title, "link": link, "summary": summary})
    return items


def _channel_xml(title: str, items: list, source_name: str = "", feed_url: str = "") -> bytes:
    """拼 RSS 2.0 输出。pubDate 统一改写为当前时间（"发鲜"：绕开 Horizon 24h
    时间窗，探索期内容只要上游有就必进管线；转正后直连恢复真实时间过滤）。
    每条 item 带 <source url="上游feed">真实源名</source>，Horizon rss.py 补丁
    读它做 feed_name 归组。"""
    now_dt = datetime.now(timezone.utc)
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0"><channel>',
        f"<title>{escape(title)}</title>",
        "<link>http://127.0.0.1:8080/</link>",
        "<description>AI 资讯雷达 · 每日探索一个新信息源</description>",
        f"<lastBuildDate>{format_datetime(now_dt)}</lastBuildDate>",
    ]
    for i, it in enumerate(items):
        pub = format_datetime(now_dt - timedelta(seconds=i))
        guid = "explorer-" + hashlib.sha1(it["link"].encode("utf-8")).hexdigest()[:16]
        desc = it["summary"]
        if not desc:
            # 上游条目不带摘要（如 HF Blog 只有 title/link）时给 LLM 兜底上下文
            desc = f"{source_name} 更新的文章，标题如上，点击链接查看全文。"
        src_el = ""
        if source_name:
            src_el = (
                f'<source url="{escape(feed_url, {chr(34): "&quot;"})}">'
                f"{escape(source_name)}</source>"
            )
        parts += [
            "<item>",
            f"<title>{escape(it['title'])}</title>",
            f"<link>{escape(it['link'])}</link>",
            f"<guid isPermaLink=\"false\">{guid}</guid>",
            f"<pubDate>{pub}</pubDate>",
            f"<description>{escape(desc)}</description>",
            src_el,
            "</item>",
        ]
    parts.append("</channel></rss>")
    return ("\n".join(parts)).encode("utf-8")


def build_relay_feed(day: str | None = None) -> bytes:
    """中转 RSS 输出（webapp /explorer/feed.xml 调用；30 分钟进程内缓存）。"""
    day = day or date.today().isoformat()
    now = time.time()
    if _relay_cache["key"] == day and _relay_cache["xml"] and now - _relay_cache["at"] < RELAY_TTL_SEC:
        return _relay_cache["xml"]

    src = today_pick(day)
    if src is None:
        xml_bytes = _channel_xml("🧪 探索频道（今日未开闸）", [])
    else:
        try:
            items = fetch_upstream(src)
        except Exception as exc:
            print(f"[探索] {src['source']} 上游抓取失败：{exc}")
            items = []
        xml_bytes = _channel_xml(
            f"🧪 探索频道 · {src['source']}", items,
            source_name=src["source"], feed_url=src["url"],
        )
        if items:
            with get_conn() as conn:
                conn.execute(
                    "UPDATE explorer_daily SET items_served = ?, updated_at = ? WHERE day = ?",
                    (len(items), now_iso(), day),
                )
    _relay_cache.update(key=day, at=now, xml=xml_bytes)
    return xml_bytes


# ---------------- ⚙ 面板数据与管理员动作 ----------------


def annotate_trial(sections: list) -> None:
    """给来源为试验中源的日报条目注入 🧪 徽章数据（api_feed 渲染用）。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT source, round FROM explorer_pool WHERE status = 'trial'"
        ).fetchall()
    by_src = {r["source"]: r["round"] for r in rows}
    if not by_src:
        return
    for sec in sections:
        for it in sec["items"]:
            round_no = by_src.get(it.get("source") or "")
            if round_no is not None:
                it["explorer"] = True
                it["explorer_round"] = round_no


def pool_status() -> dict:
    """探索面板数据：开关、今日实验源、全池状态（试验中源附实时统计）。"""
    day = date.today().isoformat()
    with get_conn() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                """SELECT source, url, category, use_proxy, status, trial_start,
                   trial_end, round, cooldown_until, promoted_at, note, updated_at
                   FROM explorer_pool ORDER BY source"""
            ).fetchall()
        ]
        served = dict(
            (r["day"], r)
            for r in conn.execute(
                "SELECT day, source, items_served FROM explorer_daily WHERE day >= ?",
                ((date.fromisoformat(day) - timedelta(days=14)).isoformat(),),
            ).fetchall()
        )
    order = {"trial": 0, "candidate": 1, "cooling": 2, "promoted": 3}
    rows.sort(key=lambda r: (order.get(r["status"], 9), r["source"]))
    for r in rows:
        if r["status"] == "trial" and r["trial_start"]:
            r["stats"] = trial_stats(r["source"], r["trial_start"], r["trial_end"])
        else:
            r["stats"] = None
    today_row = served.get(day)
    today = next((r for r in rows if today_row and r["source"] == today_row["source"]), None)
    if today and today_row:
        today["items_served"] = today_row["items_served"]
    return {"enabled": get_explorer_enabled(), "day": day, "today": today, "pool": rows}


def admin_action(action: str, source: str) -> tuple:
    """管理员手动干预：promote 转正 / cool 立即冷却 / reset 重置为候选。"""
    day = date.today().isoformat()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM explorer_pool WHERE source = ?", (source,)
        ).fetchone()
        if row is None:
            return False, "源不在探索池中"
        if action == "promote":
            if row["status"] == "promoted":
                return False, f"{source} 已是正式源"
            ok = _promote(conn, row, day, "管理员手动转正")
            return True, f"已转正：{source}" + ("" if ok else "（Horizon 已有同名源，未重复写入）")
        if action == "cool":
            until = (date.fromisoformat(day) + timedelta(days=COOLDOWN_DAYS)).isoformat()
            conn.execute(
                """UPDATE explorer_pool SET status = 'cooling', cooldown_until = ?,
                   trial_start = NULL, trial_end = NULL, note = '管理员手动冷却',
                   updated_at = ? WHERE source = ?""",
                (until, now_iso(), source),
            )
            return True, f"已冷却 {COOLDOWN_DAYS} 天：{source}"
        if action == "reset":
            conn.execute(
                """UPDATE explorer_pool SET status = 'candidate', trial_start = NULL,
                   trial_end = NULL, round = 0, cooldown_until = NULL, promoted_at = NULL,
                   note = '管理员重置', updated_at = ? WHERE source = ?""",
                (now_iso(), source),
            )
            return True, f"已重置为候选：{source}"
    return False, f"未知动作：{action}"
