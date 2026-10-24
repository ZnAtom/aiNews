#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B15 正式源健康度体检（每周随蒸馏服务跑）：活跃日归一的"展示 vs 点击"评估。

用户规则：当天一条都没点 = 今天不想看新闻，不计入考核（按活跃日归一）。
只有当"活跃日 ≥ 5"（证据充足）时才动权重，且只考核正式源（Horizon config
sources.rss 的 feed_name + 内置 hackernews / google_news）：

  - 活跃日内展示 ≥ 15 条且 0 点击 → 权重 -0.3（长期占版面无人看）
  - 活跃日内点击 ≥ 3            → 权重 +0.1（稳定吸引点击）

权重写 source_weights（与 👍/👎 即时加减同一张表、同一 clamp ±3），
radar-web 排序时经 load_weights() 生效。探索试验源由 explorer.py 自己裁决，
不在此列；历史失真数据（source='rss'）天然不在正式源名单里，自动忽略。
"""

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

RADAR_DIR = Path(__file__).resolve().parent
DB_PATH = RADAR_DIR / "data" / "feedback.db"
HORIZON_CONFIG = RADAR_DIR.parent / "Horizon" / "data" / "config.json"

WINDOW_DAYS = 14      # 统计窗口
MIN_ACTIVE_DAYS = 5   # 活跃日少于该数则证据不足，不动权重
SHOWN_FLOOR = 15      # 活跃日内展示达到该条数且 0 点击 → 降权
CLICKS_BONUS = 3      # 活跃日内点击达到该次数 → 加分
DELTA_DOWN = -0.3
DELTA_UP = 0.1
CLAMP_MIN, CLAMP_MAX = -3.0, 3.0
BUILTIN_SOURCES = ("hackernews", "google_news")  # 非 RSS 内置渠道（pushed.source 为 source_type）


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _unescape(s: str) -> str:
    """日报来源行经 _escape_markdown 转义（括号/下划线等带反斜杠），比对前去掉。"""
    return (s or "").replace("\\", "")


def official_sources() -> set:
    """正式源名单（未转义形态）：Horizon config 的 rss feed_name + 内置渠道。"""
    names = set(BUILTIN_SOURCES)
    try:
        cfg = json.loads(HORIZON_CONFIG.read_text(encoding="utf-8"))
        for f in cfg.get("sources", {}).get("rss", []):
            if f.get("name"):
                names.add(str(f["name"]))
        if cfg.get("sources", {}).get("google_news", {}).get("enabled"):
            names.add("google_news")
        if cfg.get("sources", {}).get("hackernews", {}).get("enabled"):
            names.add("hackernews")
    except Exception:
        pass
    return names


def run(window_days: int = WINDOW_DAYS, apply: bool = True) -> dict:
    """跑一次体检，返回报告 dict（apply=False 只算不写，供手动核对）。"""
    today = date.today()
    start = (today - timedelta(days=window_days - 1)).isoformat()
    lines: list = []
    updates: list = []

    conn = get_conn()
    try:
        active = {
            r["day"]
            for r in conn.execute(
                "SELECT DISTINCT day FROM clicks WHERE role='admin' AND day >= ?",
                (start,),
            )
            if r["day"]
        }
        if len(active) < MIN_ACTIVE_DAYS:
            lines.append(
                f"[源健康] 近 {window_days} 天活跃日仅 {len(active)} 天"
                f"（<{MIN_ACTIVE_DAYS}），证据不足，本次不动权重。"
            )
            return {"changed": 0, "lines": lines}

        officials = official_sources()
        # pushed.source 存日报转义形态（括号/下划线带反斜杠），取回后按去转义名归组
        pushed_rows = [
            (_unescape(r["source"]), r["day"])
            for r in conn.execute(
                "SELECT source, day FROM pushed WHERE day >= ? AND source IS NOT NULL",
                (start,),
            )
            if r["source"]
        ]
        seen = {src for src, _d in pushed_rows}
        click_days = {}  # (源, day) → 该源当日点击
        for r in conn.execute(
            "SELECT source, day, COUNT(*) c FROM clicks WHERE role='admin' AND day >= ?"
            " GROUP BY source, day",
            (start,),
        ):
            click_days[(_unescape(r["source"]), r["day"])] = r["c"]

        for name in sorted(seen & officials):
            shown = sum(1 for src, d in pushed_rows if src == name and d in active)
            clicks = sum(c for (src, _day), c in click_days.items() if src == name)
            if shown >= SHOWN_FLOOR and clicks == 0:
                delta, reason = DELTA_DOWN, f"活跃日展示 {shown} 条 0 点击"
            elif clicks >= CLICKS_BONUS:
                delta, reason = DELTA_UP, f"活跃日点击 {clicks} 次"
            else:
                lines.append(f"[源健康] {name}：展示 {shown} / 点击 {clicks}，维持权重")
                continue
            updates.append((name, delta, reason))
            lines.append(f"[源健康] {name}：{reason} → 权重 {delta:+.1f}")

        if apply:
            now = datetime.now().isoformat(timespec="seconds")
            for name, delta, _reason in updates:
                row = conn.execute(
                    "SELECT weight FROM source_weights WHERE source = ?", (name,)
                ).fetchone()
                old = float(row["weight"]) if row else 0.0
                new = max(CLAMP_MIN, min(CLAMP_MAX, old + delta))
                conn.execute(
                    """INSERT INTO source_weights (source, weight, updated_at)
                       VALUES (?, ?, ?)
                       ON CONFLICT(source) DO UPDATE SET weight = excluded.weight,
                                                         updated_at = excluded.updated_at""",
                    (name, new, now),
                )
            conn.commit()
    finally:
        conn.close()

    lines.insert(
        0,
        f"[源健康] 窗口近 {window_days} 天，活跃日 {len(active)} 天，"
        f"调整 {len(updates)} 个源" + ("" if apply else "（dry-run 未写入）"),
    )
    return {"changed": len(updates), "lines": lines}


if __name__ == "__main__":
    for line in run()["lines"]:
        print(line)
