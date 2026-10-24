#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SoS 综合生成脚本：融合 Horizon 日报与多源 RSS 条目，调用 LLM 生成综合速览/周报。

用法：
    python sos_daily.py [--date YYYY-MM-DD]   # 生成单日综合（默认今天）
    python sos_daily.py --weekly              # 生成近 7 天周报综合
"""

import argparse
import json
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import feedparser
import httpx

from hotlist import build_hot_lines, fetch_hot_items, filter_duplicates
from mail import send_markdown_mail

# 项目根目录（app/radar/sos_daily.py -> 上两级即项目根）
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = PROJECT_ROOT / "app" / "Horizon" / ".env"
SUMMARIES_DIR = PROJECT_ROOT / "app" / "Horizon" / "data" / "summaries"
SOS_DIR = PROJECT_ROOT / "out" / "sos"
# B16 备用 API 启用标记（与 Horizon 侧 backup.py 写同一文件，供推送邮件提示）
FALLBACK_FLAG = PROJECT_ROOT / "app" / "Horizon" / "data" / "llm_fallback.json"

# RSS 源清单：(显示名, 地址, 是否走系统代理)。本机源必须 trust_env=False，
# 否则系统代理（HTTP_PROXY 指向 clash）会把 127.0.0.1 请求劫持进代理导致失败。
RSS_SOURCES = [
    ("AIbase (RSSHub)", "http://127.0.0.1:1200/aibase/daily", False),
    ("The Batch (RSSHub)", "http://127.0.0.1:1200/deeplearning/the-batch", False),
    ("机器之心 (we-mp-rss)", "http://127.0.0.1:8001/feed/MP_WXS_3073282833.rss?limit=30", False),
    ("新智元 (we-mp-rss)", "http://127.0.0.1:8001/feed/MP_WXS_3271041950.rss?limit=30", False),
    ("TLDR AI", "https://tldr.tech/api/rss/ai", True),
]

SYSTEM_PROMPT = (
    "你是用户的私人 AI 资讯分析师。严格只基于用户提供的当天日报与各源条目写作，"
    "禁止引入你自身知识或训练数据中的任何事件；不确定的不写；不编造来源与日期。"
)


# B17：.env 自愈——app/Horizon/.env 历史上多次被误覆盖成空模板（10-23、10-24 等），
# 雷达全部任务都经 load_env 读它。每次读取前校验必填 key，缺失且权威副本
# （config/.env）完好时自动恢复，避免"配置被清空 → 当日任务失败"复发。
AUTH_ENV_PATH = PROJECT_ROOT / "config" / ".env"
REQUIRED_ENV_KEYS = ("OPENAI_API_KEY", "OPENAI_BASE_URL", "HORIZON_MODEL")


def _parse_env_text(text: str) -> dict:
    """解析 KEY=VALUE 文本（不依赖 python-dotenv）。"""
    env = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        # 去除两端空白与成对引号
        value = value.strip().strip('"').strip("'").strip()
        if key:
            env[key] = value
    return env


def _env_selfheal() -> None:
    """app/Horizon/.env 缺必填 key 时自动从 config/.env 权威副本恢复。"""
    try:
        if not AUTH_ENV_PATH.exists():
            return
        try:
            cur = (
                _parse_env_text(ENV_PATH.read_text(encoding="utf-8"))
                if ENV_PATH.exists()
                else {}
            )
        except Exception:
            cur = {}
        missing = [k for k in REQUIRED_ENV_KEYS if not cur.get(k, "").strip()]
        if not missing:
            return
        auth_text = AUTH_ENV_PATH.read_text(encoding="utf-8")
        auth = _parse_env_text(auth_text)
        if not all(auth.get(k, "").strip() for k in REQUIRED_ENV_KEYS):
            return  # 权威副本自身不完整，不动运行时文件
        tmp = ENV_PATH.with_name(".env.tmp")
        tmp.write_text(auth_text, encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(ENV_PATH)
        print(
            f"[Env] 警告：app/Horizon/.env 缺少 {'、'.join(missing)}"
            "（疑似又被覆盖为空模板），已自动从 config/.env 恢复"
        )
    except Exception as exc:  # 自愈失败不阻断主流程
        print(f"[Env] .env 自愈检查失败（忽略）：{type(exc).__name__}: {exc}")


def load_env(path: Path) -> dict:
    """手写解析 KEY=VALUE 格式的 .env 文件（不依赖 python-dotenv）。"""
    if path == ENV_PATH:
        _env_selfheal()
    if not path.exists():
        return {}
    return _parse_env_text(path.read_text(encoding="utf-8"))


def fetch_rss_entries() -> list:
    """拉取全部 RSS 源，每源取最新 15 条；任一源失败仅跳过不中断。"""
    entries = []
    for name, url, use_proxy in RSS_SOURCES:
        try:
            resp = httpx.get(
                url, trust_env=use_proxy, timeout=20, follow_redirects=True
            )
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
            got = 0
            for e in feed.entries[:15]:
                title = (e.get("title") or "").strip()
                if not title:
                    continue
                entries.append(
                    {
                        "source": name,
                        "title": title,
                        "link": (e.get("link") or "").strip(),
                        "published": (e.get("published") or "").strip(),
                    }
                )
                got += 1
            print(f"[RSS] {name}: 获取 {got} 条")
        except Exception as exc:  # 单源失败不影响整体
            print(f"[RSS] {name}: 获取失败，已跳过（{type(exc).__name__}: {exc}）")
    return entries


def _llm_targets(env: dict) -> list:
    """B16：返回 (base_url, model, api_key) 目标列表——主端点 + 可选备用端点。

    .env 配置 BACKUP_OPENAI_API_KEY + BACKUP_OPENAI_BASE_URL 即启用备用端点；
    BACKUP_HORIZON_MODEL 为备用端点模型名（与主端点不同时才需要，留空沿用主模型）。
    """
    base_url = env.get("OPENAI_BASE_URL", "").rstrip("/")
    model = env.get("HORIZON_MODEL", "")
    api_key = env.get("OPENAI_API_KEY", "")
    if not base_url or not model or not api_key:
        return []
    targets = [(base_url, model, api_key)]
    b_key = env.get("BACKUP_OPENAI_API_KEY", "").strip()
    b_url = env.get("BACKUP_OPENAI_BASE_URL", "").strip().rstrip("/")
    if b_key and b_url:
        b_model = env.get("BACKUP_HORIZON_MODEL", "").strip() or model
        targets.append((b_url, b_model, b_key))
    return targets


def _is_transient_error(message: str) -> bool:
    """B16：判定是否为限额/鉴权/服务不可用/连接类可切换备用端点的错误。"""
    lowered = message.lower()
    return (
        "429" in lowered
        or "rate limit" in lowered
        or "too many requests" in lowered
        or "401" in lowered
        or "403" in lowered
        or "quota" in lowered
        or "exceeded" in lowered
        or "insufficient" in lowered
        or "502" in lowered
        or "503" in lowered
        or "504" in lowered
        or "service unavailable" in lowered
        or "bad gateway" in lowered
        or "connect" in lowered
        or "timeout" in lowered
        or "timed out" in lowered
        or "empty response" in lowered
    )


def _note_backup_used(last_error: str = "") -> None:
    """B16：记录一次备用 API 启用（与 Horizon 侧 backup.py 写同一标记文件）。

    标记仅供推送邮件提示用，任何失败都不影响主流程。
    """
    try:
        today = time.strftime("%Y-%m-%d")
        try:
            data = json.loads(FALLBACK_FLAG.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        if not isinstance(data, dict) or data.get("date") != today:
            data = {"date": today, "count": 0, "horizon": 0, "radar": 0}
        data["count"] = int(data.get("count", 0)) + 1
        data["radar"] = int(data.get("radar", 0)) + 1
        data["last_error"] = (last_error or "")[:200]
        data["last_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        tmp = FALLBACK_FLAG.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(FALLBACK_FLAG)
    except Exception:
        pass


def call_llm(env: dict, system_prompt: str, user_prompt: str, max_tokens: int = 2500) -> tuple:
    """调用 OpenAI 兼容接口；base_url + /chat/completions 404 时回退 /v1/chat/completions。

    B16：主端点因限额/鉴权/5xx/连接类错误失败时，自动改用备用端点
    （.env BACKUP_OPENAI_*）重试一次并落盘当日标记（供推送邮件提示）。

    返回 (content, usage, model)。走系统代理（trust_env=True，与 Horizon 主程序一致）。
    """
    targets = _llm_targets(env)
    if not targets:
        raise RuntimeError(".env 缺少 OPENAI_BASE_URL / HORIZON_MODEL / OPENAI_API_KEY 配置")

    attempts = []
    with httpx.Client(trust_env=True, timeout=120) as client:
        for idx, (base_url, model, api_key) in enumerate(targets):
            if idx > 0 and not _is_transient_error("；".join(attempts)):
                break  # 主端点失败但非限额/不可用类，切备用无意义
            tag = "主" if idx == 0 else "备"
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            body = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.3,
                "max_tokens": max_tokens,
            }
            for suffix in ("/chat/completions", "/v1/chat/completions"):
                url = base_url + suffix
                try:
                    resp = client.post(url, headers=headers, json=body)
                    if resp.status_code == 404:
                        attempts.append(f"[{tag}]{suffix} -> 404，尝试下一个端点")
                        continue
                    resp.raise_for_status()
                    data = resp.json()
                    content = data["choices"][0]["message"]["content"]
                    usage = data.get("usage", {}) or {}
                    if idx > 0:
                        _note_backup_used(attempts[-1])
                        print(f"[LLM] 主端点不可用，已改用备用端点（最近错误：{attempts[-1]}）")
                    return content, usage, model
                except Exception as exc:
                    # 只记录错误类型与状态码，绝不输出请求头/密钥
                    detail = f"HTTP {exc.response.status_code}" if hasattr(exc, "response") and exc.response is not None else type(exc).__name__
                    attempts.append(f"[{tag}]{suffix} -> {detail}")

    raise RuntimeError("LLM 调用失败：" + "；".join(attempts))


def call_llm_stream(env: dict, system_prompt: str, user_prompt: str, max_tokens: int) -> tuple:
    """流式调用 OpenAI 兼容接口：长推理场景下避免"120s 无字节返回"的 ReadTimeout。

    B16：主端点因限额/鉴权/5xx/连接类错误失败时，自动改用备用端点
    （.env BACKUP_OPENAI_*）重试一次并落盘当日标记（供推送邮件提示）。

    返回 (content, usage, model)。
    """
    targets = _llm_targets(env)
    if not targets:
        raise RuntimeError(".env 缺少 OPENAI_BASE_URL / HORIZON_MODEL / OPENAI_API_KEY 配置")

    attempts = []
    with httpx.Client(trust_env=True, timeout=120) as client:
        for idx, (base_url, model, api_key) in enumerate(targets):
            if idx > 0 and not _is_transient_error("；".join(attempts)):
                break  # 主端点失败但非限额/不可用类，切备用无意义
            tag = "主" if idx == 0 else "备"
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            body = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.3,
                "max_tokens": max_tokens,
                "stream": True,
                "stream_options": {"include_usage": True},
            }
            for suffix in ("/chat/completions", "/v1/chat/completions"):
                url = base_url + suffix
                try:
                    parts = []
                    usage = {}
                    with client.stream("POST", url, headers=headers, json=body) as resp:
                        if resp.status_code == 404:
                            attempts.append(f"[{tag}]{suffix} -> 404，尝试下一个端点")
                            continue
                        resp.raise_for_status()
                        for line in resp.iter_lines():
                            line = line.strip()
                            if not line.startswith("data:"):
                                continue
                            payload = line[len("data:"):].strip()
                            if payload == "[DONE]":
                                break
                            try:
                                obj = json.loads(payload)
                            except ValueError:
                                continue
                            if obj.get("usage"):
                                usage = obj["usage"]
                            for choice in obj.get("choices") or []:
                                delta = choice.get("delta") or {}
                                parts.append(delta.get("content") or "")
                    if idx > 0:
                        _note_backup_used(attempts[-1])
                        print(f"[LLM] 主端点不可用，已改用备用端点（最近错误：{attempts[-1]}）")
                    return "".join(parts), usage, model
                except Exception as exc:
                    detail = f"HTTP {exc.response.status_code}" if hasattr(exc, "response") and exc.response is not None else type(exc).__name__
                    attempts.append(f"[{tag}]{suffix} -> {detail}")

    raise RuntimeError("LLM 流式调用失败：" + "；".join(attempts))


def call_llm_robust(env: dict, user_prompt: str) -> tuple:
    """调用 LLM 并处理推理模型把 max_tokens 全耗在思考上的情况。

    正文过短时改用流式 + 更大 max_tokens 重试（该模型不支持关闭 thinking，
    流式可避免长推理期间 read timeout）。
    """
    content, usage, model = call_llm(env, SYSTEM_PROMPT, user_prompt)
    if len(content.strip()) < 30:
        print("[SoS] 正文过短（疑似被推理 token 挤占），切换流式 + 提高 max_tokens 重试一次…")
        content, usage, model = call_llm_stream(
            env, SYSTEM_PROMPT, user_prompt, max_tokens=16000
        )
        if len(content.strip()) < 30:
            raise RuntimeError("LLM 返回正文为空/过短（max_tokens 被推理 token 挤占），请调整模型或配置")
    return content, usage, model


def build_entry_lines(entries: list) -> str:
    """把 RSS 条目渲染成编号列表文本。"""
    lines = []
    for i, e in enumerate(entries, 1):
        pub = f" | {e['published']}" if e.get("published") else ""
        lines.append(f"{i}. [{e['source']}] {e['title']} | {e['link']}{pub}")
    return "\n".join(lines) if lines else "（无可用条目）"


def normalize_headline(text: str, expected_title: str) -> str:
    """确保输出第一行标题与约定一致（LLM 偶尔会改写标题行）。"""
    lines = text.lstrip().splitlines()
    if lines and lines[0].startswith("#"):
        lines[0] = expected_title
        return "\n".join(lines)
    return expected_title + "\n\n" + text


def run_daily(target: date, wait_hours: float = 0) -> int:
    """单日综合生成。wait_hours>0：日报未生成时轮询等待（供定时器使用）。"""
    report_path = SUMMARIES_DIR / f"horizon-{target.isoformat()}-zh.md"
    if not report_path.exists() and wait_hours > 0:
        deadline = time.time() + wait_hours * 3600
        print(f"[SoS] 日报尚未生成，轮询等待中（最长 {wait_hours:g} 小时，每 60 秒检查一次）…", flush=True)
        while not report_path.exists() and time.time() < deadline:
            time.sleep(60)
    if not report_path.exists():
        print(f"[错误] 日报不存在：{report_path}")
        available = sorted(SUMMARIES_DIR.glob("horizon-*-zh.md")) if SUMMARIES_DIR.exists() else []
        if available:
            print("当前可用的日报文件：")
            for p in available:
                print(f"  - {p.name}")
        else:
            print("summaries 目录中没有任何日报文件。")
        return 1

    report_text = report_path.read_text(encoding="utf-8")
    entries = fetch_rss_entries()

    # 热榜材料：剔除日报已收录的条目，供"🧨 破圈推荐"小节选用；失败不阻断主流程
    try:
        hot_items = filter_duplicates(fetch_hot_items(), report_text)
    except Exception as exc:
        print(f"[SoS] 热榜获取失败，破圈小节将留空（{type(exc).__name__}: {exc}）")
        hot_items = []
    hot_lines = build_hot_lines(hot_items)

    user_prompt = f"""以下是 {target.isoformat()} 的日报全文：

<日报>
{report_text}
</日报>

以下是各 RSS 源最新条目（编号 | 来源 | 标题 | 链接 | 日期如有）：

{build_entry_lines(entries)}

以下是全网热榜最新条目（已剔除日报收录；编号 | 来源 | 标题 | 链接 | 日期如有）：

{hot_lines}

请基于以上材料输出 Markdown，第一行必须是：`# SoS 综合速览 - {target.isoformat()}`。
结构要求（必须包含以下四个小节）：

## 🔗 共识要点
仅收录 ≥2 个来源同时提到的事件，每条末尾用括号注明提及的源名列表。

## 🔍 独家观察
单一来源提到但值得注意的点。

## 🧨 破圈推荐
从"热榜条目"中挑 1~2 条未被日报收录、但可能对用户有价值的（AI/科技相关优先），
每条一行：`- [标题](链接) —— 一句话理由`（说明为什么值得跳出既有偏好关注它）。
若确实没有合适的，本节只写"（今日无合适的破圈条目）"。

## 🧭 我的综合判断
2~4 句综合研判，不要罗列细节。

注意：请直接输出最终 Markdown 结果，不要输出思考或推理过程。"""

    env = load_env(ENV_PATH)
    content, usage, model = call_llm_robust(env, user_prompt)

    final_text = normalize_headline(content, f"# SoS 综合速览 - {target.isoformat()}")
    out_path = SOS_DIR / f"sos-{target.isoformat()}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    existed = out_path.exists()
    out_path.write_text(final_text, encoding="utf-8")

    print(f"[SoS] 已生成：{out_path}")
    print(f"[SoS] model: {model}")
    print(f"[SoS] token usage: {usage}")

    # B7：单发一封 SoS 邮件。仅"今天的文件是本次新建"时发——
    # 历史日期重跑、当天重复生成都不会刷邮件；失败不阻断主流程。
    if target == date.today() and not existed:
        try:
            sent = send_markdown_mail(
                env, f"SoS 综合速览 - {target.isoformat()}", final_text
            )
            print(f"[SoS] 邮件已发送（{sent} 位订阅者）")
        except Exception as exc:
            print(f"[SoS] 邮件发送失败（不影响已生成文件）：{type(exc).__name__}: {exc}")
    return 0


def run_weekly() -> int:
    """近 7 天周报综合生成。"""
    today = date.today()
    start = today - timedelta(days=6)
    end = today
    iso_year, iso_week, _ = today.isocalendar()

    report_paths = []
    for i in range(7):
        d = start + timedelta(days=i)
        p = SUMMARIES_DIR / f"horizon-{d.isoformat()}-zh.md"
        if p.exists():
            report_paths.append(p)

    sos_paths = []
    if SOS_DIR.exists():
        for p in sorted(SOS_DIR.glob("sos-*.md")):
            m = re.match(r"sos-(\d{4}-\d{2}-\d{2})\.md$", p.name)
            if not m:
                continue
            try:
                d = date.fromisoformat(m.group(1))
            except ValueError:
                continue
            if start <= d <= end:
                sos_paths.append(p)

    if not report_paths and not sos_paths:
        print(f"[错误] 近 7 天（{start} ~ {end}）没有任何日报或 SoS 文件可作输入。")
        return 1

    parts = []
    for p in report_paths:
        parts.append(f"<日报文件 {p.name}>\n{p.read_text(encoding='utf-8')}\n</日报文件 {p.name}>")
    for p in sos_paths:
        parts.append(f"<已有SoS文件 {p.name}>\n{p.read_text(encoding='utf-8')}\n</已有SoS文件 {p.name}>")

    materials = "\n\n".join(parts)
    entries = fetch_rss_entries()

    user_prompt = f"""以下是 {start.isoformat()} 至 {end.isoformat()} 期间存在的日报与既有 SoS 文档全文：

{materials}

以下是各 RSS 源最新条目（编号 | 来源 | 标题 | 链接 | 日期如有）：

{build_entry_lines(entries)}

请基于以上材料输出 Markdown，第一行必须是：`# SoS 周报综合 - {start.isoformat()} ~ {end.isoformat()}`。
结构要求（必须包含以下三个小节）：

## 🔗 共识要点
仅收录 ≥2 个来源同时提到的事件，每条末尾用括号注明提及的源名列表。

## 🔍 独家观察
单一来源提到但值得注意的点。

## 🧭 我的综合判断
2~4 句综合研判，不要罗列细节。

注意：请直接输出最终 Markdown 结果，不要输出思考或推理过程。"""

    env = load_env(ENV_PATH)
    content, usage, model = call_llm_robust(env, user_prompt)

    title = f"# SoS 周报综合 - {start.isoformat()} ~ {end.isoformat()}"
    final_text = normalize_headline(content, title)
    out_path = SOS_DIR / f"sos-week-{iso_year}-W{iso_week}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    existed = out_path.exists()
    out_path.write_text(final_text, encoding="utf-8")

    print(f"[SoS] 已生成：{out_path}")
    print(f"[SoS] model: {model}")
    print(f"[SoS] token usage: {usage}")

    # B7：周报同理——仅文件本次新建时发（周报只针对当周，无需日期判断）
    if not existed:
        try:
            sent = send_markdown_mail(env, f"SoS 周报综合 - {start.isoformat()} ~ {end.isoformat()}", final_text)
            print(f"[SoS] 邮件已发送（{sent} 位订阅者）")
        except Exception as exc:
            print(f"[SoS] 邮件发送失败（不影响已生成文件）：{type(exc).__name__}: {exc}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="SoS 综合速览生成（日报/周报）")
    parser.add_argument("--date", help="目标日期 YYYY-MM-DD（默认今天）")
    parser.add_argument("--weekly", action="store_true", help="生成近 7 天周报综合")
    parser.add_argument("--wait-hours", type=float, default=0.0, help="日报不存在时轮询等待的小时数（定时器使用）")
    args = parser.parse_args()

    if args.weekly:
        return run_weekly()

    if args.date:
        try:
            target = date.fromisoformat(args.date)
        except ValueError:
            print(f"[错误] 日期格式不合法：{args.date}（应为 YYYY-MM-DD）")
            return 1
    else:
        target = date.today()
    return run_daily(target, wait_hours=args.wait_hours)


if __name__ == "__main__":
    sys.exit(main())
