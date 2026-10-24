#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""调教蒸馏 CLI：读取 feedback.db 的投票记录，调 LLM 蒸馏出关键词权重并写入 keyword_weights。

用法：uv run python distill.py
"""

import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from sos_daily import ENV_PATH, call_llm, load_env

DB_PATH = Path(__file__).resolve().parent / "data" / "feedback.db"
STATE_PATH = DB_PATH.parent / "distill_state.json"

# 最少投票样本数，不足则退出
MIN_VOTES = 5


def load_state() -> dict:
    """读取蒸馏水位（last_vote_id 等）；文件缺失或损坏返回空 dict。"""
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict) -> None:
    """写入蒸馏水位。"""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def extract_json(text: str) -> dict:
    """从 LLM 响应中提取 JSON 对象（优先 ```json 代码块，其次首尾大括号）。"""
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if m:
        return json.loads(m.group(1))
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError("LLM 响应中未找到 JSON")


def load_votes() -> list:
    """读取全部投票记录。"""
    if not DB_PATH.exists():
        print("[错误] 未找到反馈数据库：", DB_PATH)
        return []
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, item_title, vote, day FROM votes ORDER BY created_at"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def save_keywords(keywords: list, block_keywords: list) -> None:
    """把蒸馏结果写入 keyword_weights：正/负关键词均 upsert，block_keywords 写负权重。"""
    now = datetime.now().isoformat(timespec="seconds")
    conn = sqlite3.connect(DB_PATH, timeout=10)
    for kw in keywords:
        keyword = (kw.get("keyword") or "").strip()
        if not keyword:
            continue
        try:
            weight = float(kw.get("weight", 1.0))
        except (TypeError, ValueError):
            continue
        weight = max(-1.0, min(2.0, weight))
        conn.execute(
            """INSERT INTO keyword_weights (keyword, weight, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(keyword) DO UPDATE SET weight = excluded.weight,
                                                 updated_at = excluded.updated_at""",
            (keyword, weight, now),
        )
    for kw in block_keywords:
        keyword = (kw or "").strip()
        if not keyword:
            continue
        conn.execute(
            """INSERT INTO keyword_weights (keyword, weight, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(keyword) DO UPDATE SET weight = excluded.weight,
                                                 updated_at = excluded.updated_at""",
            (keyword, -1.0, now),
        )
    conn.commit()
    conn.close()


def main() -> int:
    # B15：正式源健康度体检（活跃日归一的展示 vs 点击），与投票蒸馏互不依赖，
    # 每周必跑；失败不影响后面的关键词蒸馏
    try:
        import source_health

        for line in source_health.run()["lines"]:
            print(line)
    except Exception as exc:  # noqa: BLE001
        print(f"[源健康] 体检失败（不影响蒸馏）：{exc}")

    votes = load_votes()
    state = load_state()
    last_id = int(state.get("last_vote_id") or 0)

    # 门槛：已有水位后只看水位之后的新投票，不足 MIN_VOTES 直接跳过（蒸馏仍用全部历史）
    if last_id > 0:
        new_votes = [v for v in votes if v["id"] > last_id]
        if len(new_votes) < MIN_VOTES:
            print(
                f"新样本不足：水位 {last_id} 之后仅 {len(new_votes)} 条新投票，"
                f"至少需要 {MIN_VOTES} 条，本次跳过。"
            )
            return 0
    elif len(votes) < MIN_VOTES:
        print(f"样本不足：当前仅 {len(votes)} 条投票记录，至少需要 {MIN_VOTES} 条，退出。")
        return 0

    up_titles = [v["item_title"] for v in votes if v["vote"] > 0]
    down_titles = [v["item_title"] for v in votes if v["vote"] < 0]
    print(f"[蒸馏] 样本：共 {len(votes)} 条（👍 {len(up_titles)} 条 / 👎 {len(down_titles)} 条）")

    user_prompt = f"""以下是用户在 AI 资讯雷达中的历史反馈，👍 表示喜欢、👎 表示不喜欢：

👍 喜欢的条目标题：
{chr(10).join('- ' + t for t in up_titles) or '（无）'}

👎 不喜欢的条目标题：
{chr(10).join('- ' + t for t in down_titles) or '（无）'}

请分析用户偏好，只输出一个 JSON 对象（不要输出其他文字），结构如下：
{{
  "keywords": [{{"keyword": "关键词（贴合条目用词，2~6 个）", "weight": 1.0}}],
  "block_keywords": ["用户明显不感兴趣的关键词"]
}}

要求：
- keywords 数组 3~8 条，weight 取 -1 到 +2 之间的小数，越喜欢越高；
- block_keywords 数组 0~5 条，写负权重前先放这里；
- 关键词必须来自上面条目标题中出现过的主题词，禁止编造。"""

    env = load_env(ENV_PATH)
    content, usage, model = call_llm(env, SYSTEM_PROMPT, user_prompt)
    result = extract_json(content)

    keywords = result.get("keywords", []) or []
    block_keywords = result.get("block_keywords", []) or []
    save_keywords(keywords, block_keywords)

    # 打印蒸馏结果摘要（不含任何密钥信息）
    print(f"[蒸馏] model: {model}")
    print(f"[蒸馏] token usage: {usage}")
    print(f"[蒸馏] 偏好关键词（写入 keyword_weights）：")
    for kw in keywords:
        print(f"  + {kw.get('keyword')} -> {kw.get('weight')}")
    if block_keywords:
        print(f"[蒸馏] 屏蔽关键词（写入 -1.0）：")
        for kw in block_keywords:
            print(f"  - {kw}")

    # 成功后推进水位，供下次增量门槛判断
    ids = [v["id"] for v in votes if v.get("id") is not None]
    save_state(
        {
            "last_vote_id": max(ids) if ids else last_id,
            "last_distilled_at": datetime.now().isoformat(timespec="seconds"),
        }
    )
    print("[蒸馏] 完成。")
    return 0


SYSTEM_PROMPT = (
    "你是用户的私人 AI 资讯分析师。严格只基于用户提供的条目标题分析偏好，"
    "禁止引入外部知识中的事件；只输出用户要求的 JSON，不要输出其他文字。"
)

if __name__ == "__main__":
    sys.exit(main())
