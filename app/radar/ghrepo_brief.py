"""GitHub 项目解读 skill：对 B5 每日 GitHub 热门项目生成中文深度解读。

与 paper_brief（论文解读 skill）同一套模式，约束：每个项目 LLM 调用 ≤3 次——

  1. 免费：抓 GitHub API 的仓库 README 原文（无需 key），不耗 LLM；
  2. 第 1 次 LLM：非流式分析（材料齐备时通常这一次就够）；
  3. 第 2/3 次：仅在正文过短（推理 token 挤占）或异常时，切换流式更大 max_tokens 重试。

输出 markdown 落盘缓存到 out/ghrepos/{full_name}.md（全局唯一，跨日复用），
webapp 详情页 /ghrepo 按需调用。与论文解读不同：项目类解读允许使用开源生态
公认可识（替代方案、同类工具），但具体数字仍只能来自材料，防编造。
"""

import re
from datetime import datetime
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[2]
GHREPOS_DIR = PROJECT_ROOT / "out" / "ghrepos"

README_API = "https://api.github.com/repos/{full_name}/readme"
GH_HEADERS = {"Accept": "application/vnd.github.raw+json", "User-Agent": "ai-news-radar"}
README_MAX_CHARS = 6000

# ---------- README 抓取（免费，不耗 LLM 预算） ----------

_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")  # markdown 图片（README 头部徽章噪音）
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_URL_PAREN_RE = re.compile(r"\]\((https?:[^)\s]+)\)")  # 链接只留文字


def _clean_readme(text: str) -> str:
    """轻量清理：去徽章图片/HTML 标签/链接括号，压连续空行，截取前段。"""
    text = _IMG_RE.sub(" ", text)
    text = _URL_PAREN_RE.sub("]", text)
    text = _HTML_TAG_RE.sub(" ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()[:README_MAX_CHARS]


def fetch_gh_readme(full_name: str, timeout: float = 20.0) -> str | None:
    """抓取仓库 README 原文；失败返回 None（降级为仅用官方简介分析）。"""
    last_err = None
    for use_proxy in (False, True):  # 先直连，失败走系统代理
        try:
            resp = httpx.get(
                README_API.format(full_name=full_name),
                headers=GH_HEADERS,
                trust_env=use_proxy,
                timeout=timeout,
                follow_redirects=True,
            )
            resp.raise_for_status()
            cleaned = _clean_readme(resp.text or "")
            if cleaned:
                return cleaned
            return None
        except Exception as exc:  # noqa: BLE001 - 逐个通道尝试
            last_err = exc
    print(f"[GhBrief] {full_name} README 抓取失败（{type(last_err).__name__}），降级为仅用简介")
    return None


# ---------- Skill 提示词 ----------

GH_SYSTEM_PROMPT = """你是一名严谨的开源项目解读助手，帮非专业读者快速看懂一个 GitHub 热门项目。

【输出格式】中文 Markdown，固定包含以下小节（### 级别）：

### 一句话总结
一句大白话说清这个项目是做什么的、亮点在哪（不超过 60 字）。

### 这个项目是做什么的
2~4 句：项目定位、核心功能、在同类工具中的位置。

### 人们通常用它做什么
2~4 条要点列表：典型使用场景与人群（README 中提到的优先）。

### 为什么好用
2~4 句：设计亮点、性能/易用性/生态优势（依据 README 描述归纳，不夸大）。

### 没有它的话，人们通常用什么
2~4 条要点：同类替代方案或该类需求此项目出现前的常见做法；README 没提的可用开源生态通识补充，推理得出的句尾标注 [推断]。

### 它解决了什么问题
2~4 句：痛点与动机、相比替代方案的本质差异。

### 值得注意的点
2~3 条要点：适用场景、局限或上手提醒（材料未提及的就写"材料未提及"，不要臆测）。

【写作规则（必须遵守）】
1. 主要依据"给定材料"（README、官方简介、star/语言数据）分析；
2. 开源领域的公认可识（同类项目、替代方案、技术背景）可以使用，但由你归纳推理的结论句尾标注 [推断]；
3. 具体数字（star 数、版本号、性能数据、benchmark）只能来自材料，材料中没有就写 [未明确给出]，不得编造；
4. 术语首次出现时用一句括号给出通俗解释。"""


def build_user_prompt(gh: dict, readme: str | None) -> str:
    """组装单次 LLM 调用的用户提示词。gh 为 ghrepos 表行（dict）。"""
    parts = [
        f"# 待解读项目\n\n- 仓库：{gh['full_name']}\n- 链接：{gh.get('url') or ('https://github.com/' + gh['full_name'])}",
        f"- Star 数：{gh.get('stars', 0)}",
        f"- 主语言：{gh.get('language') or '未知'}",
        f"- 官方简介：{(gh.get('description') or '（无）')[:500]}",
    ]
    parts.append(
        "\n## README（GitHub 仓库页抓取，可能已截断）\n\n"
        + (readme or "[未抓取到，仅凭官方简介分析，并在开头注明此局限]")
    )
    parts.append("\n请按系统提示的固定小节输出中文 Markdown 解读。")
    return "\n".join(parts)


# ---------- 主分析入口（每个项目 LLM 调用 ≤3 次） ----------


def analyze_ghrepo(env: dict, gh: dict, readme: str | None) -> str:
    """生成一个 GitHub 项目的解读 markdown。LLM 调用预算：最多 3 次。

    第 1 次：非流式（材料齐备时通常一次成功）；
    第 2/3 次：正文过短（推理 token 挤占）或异常时，切换流式 + 更大 max_tokens。
    """
    from sos_daily import call_llm, call_llm_stream  # 复用同一套 OpenAI 兼容调用

    user_prompt = build_user_prompt(gh, readme)
    max_tokens_seq = [4000, 12000, 16000]
    last_err: Exception | None = None
    for i, max_tokens in enumerate(max_tokens_seq):
        try:
            if i == 0:
                content, _usage, _model = call_llm(
                    env, GH_SYSTEM_PROMPT, user_prompt, max_tokens=max_tokens
                )
            else:
                content, _usage, _model = call_llm_stream(
                    env, GH_SYSTEM_PROMPT, user_prompt, max_tokens=max_tokens
                )
            if len(content.strip()) >= 30:
                return content.strip()
            last_err = RuntimeError(f"第 {i + 1} 次调用正文过短（{len(content.strip())} 字符）")
            print(f"[GhBrief] {last_err}，重试…")
        except Exception as exc:  # noqa: BLE001 - 预算内重试
            last_err = exc
            print(f"[GhBrief] 第 {i + 1} 次调用失败（{type(exc).__name__}），重试…")
    raise RuntimeError(f"项目解读生成失败（已用满 3 次调用预算）：{last_err}")


# ---------- 缓存（按 full_name 全局唯一，同一项目跨日只解读一次） ----------


def gh_cache_path(full_name: str) -> Path:
    return GHREPOS_DIR / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', full_name)}.md"


def gh_load_cached(full_name: str) -> str | None:
    p = gh_cache_path(full_name)
    if p.exists():
        try:
            text = p.read_text(encoding="utf-8")
            # 头部注释（<!-- ... --> 元信息）之后的正文
            m = re.match(r"\s*<!--.*?-->\s*", text, re.S)
            return text[m.end():] if m else text
        except Exception:
            return None
    return None


def gh_save_cached(full_name: str, url: str, content: str, gh: dict | None = None) -> Path:
    p = gh_cache_path(full_name)
    p.parent.mkdir(parents=True, exist_ok=True)
    g = gh or {}
    head = (
        f"<!--\nfull_name: {full_name}\nurl: {url}\n"
        f"stars: {g.get('stars', '-')}\nlanguage: {g.get('language') or '-'}\n"
        f"description: {(g.get('description') or '').replace(chr(10), ' ')}\n"
        f"generated_at: {datetime.now().isoformat(timespec='seconds')}\n-->\n\n"
    )
    p.write_text(head + content, encoding="utf-8")
    return p


def gh_cache_meta(full_name: str) -> dict:
    """解析缓存文件头部注释里的元信息（仓库/链接/star/语言/简介/生成时间）。"""
    try:
        head = gh_cache_path(full_name).read_text(encoding="utf-8")[:4000]
        m = re.search(r"<!--(.*?)-->", head, re.S)
        if m:
            info: dict = {}
            for line in m.group(1).splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    info[k.strip()] = v.strip()
            return info
    except Exception:
        pass
    return {}
