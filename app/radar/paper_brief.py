"""论文解读 skill：对日报"论文雷达"里挑出的 arXiv 论文生成中文深度解读。

参考社区 skill（flyer-Li/paper-analyst 的反幻觉标注、Ayanami0730/paper-read-skill
的按论文类型路由）裁剪而成，约束：每篇论文 LLM 调用 ≤3 次——

  1. 免费：抓 arXiv abs 页 <meta> 元数据（标题/作者/日期/官方摘要/分类），不耗 LLM；
  2. 第 1 次 LLM：非流式分析（材料齐备时通常这一次就够）；
  3. 第 2/3 次：仅在正文过短（推理 token 挤占）或异常时，切换流式更大 max_tokens 重试。

输出 markdown 落盘缓存到 out/papers/{arxiv_id}.md（全局唯一，跨日复用），webapp 详情页按需调用。
"""

import html as html_mod
import re
from datetime import datetime
from pathlib import Path

import httpx

from interest import extract_arxiv_id

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PAPERS_DIR = PROJECT_ROOT / "out" / "papers"

# ---------- 论文判定与 arXiv 元数据抓取（免费，不耗 LLM 预算） ----------

ARXIV_ABS_RE = re.compile(r"^https?://(www\.)?arxiv\.org/abs/", re.I)
_META_RE = re.compile(
    r'<meta\s+name="(citation_[a-z_]+)"\s+content="([^"]*)"', re.I
)
_SUBJECTS_RE = re.compile(
    r'<td class="tablecell subjects">(.*?)</td>', re.S
)
_TAG_RE = re.compile(r"<[^>]+>")


def is_paper(url: str) -> bool:
    """是否论文条目（当前仅识别 arXiv abs 链接）。"""
    return bool(url) and bool(ARXIV_ABS_RE.match(url.strip()))


def fetch_arxiv_meta(abs_url: str, timeout: float = 20.0) -> dict | None:
    """抓取 arXiv abs 页的 citation_* 元数据；失败返回 None（降级为仅用日报摘要）。"""
    last_err = None
    for use_proxy in (False, True):  # 先直连，失败走系统代理
        try:
            resp = httpx.get(
                abs_url,
                trust_env=use_proxy,
                timeout=timeout,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (radar-paper-brief)"},
            )
            resp.raise_for_status()
            page = resp.text

            meta: dict[str, str] = {}
            for name, content in _META_RE.findall(page):
                val = html_mod.unescape(content).strip()
                if name in ("citation_author",):  # 多作者合并
                    meta.setdefault(name, [])
                    if isinstance(meta[name], list):
                        meta[name].append(val)
                else:
                    meta[name] = val

            subjects = ""
            m = _SUBJECTS_RE.search(page)
            if m:
                subjects = _TAG_RE.sub(" ", m.group(1))
                subjects = re.sub(r"\s+", " ", html_mod.unescape(subjects)).strip()

            abstract = meta.get("citation_abstract", "")
            return {
                "title": meta.get("citation_title", ""),
                "authors": meta.get("citation_author", []),
                "date": meta.get("citation_date", "")
                or meta.get("citation_online_date", ""),
                "abstract": abstract[:3000],
                "subjects": subjects,
            }
        except Exception as exc:  # noqa: BLE001 - 逐个通道尝试
            last_err = exc
    print(f"[PaperBrief] arXiv 页抓取失败（{type(last_err).__name__}），降级为仅用日报摘要")
    return None


# ---------- Skill 提示词 ----------

PAPER_SYSTEM_PROMPT = """你是一名严谨的 AI/机器人领域论文解读助手，帮非论文专业读者快速看懂一篇论文。

【输出格式】中文 Markdown，固定包含以下小节（### 级别）：

### 一句话总结
一句大白话说清这篇论文做了什么、亮点在哪（不超过 60 字）。

### 这篇论文在做什么
2~4 句：研究背景与动机、要解决的核心问题、在领域中的定位（开创新范式 / 达到新 SOTA / 渐进式改进 / 提出新基准或数据集）。

### 用了什么方法
3~6 句：核心思路（Key Idea）与关键步骤/组件。若论文属于 benchmark / 数据集类，则改为描述：任务怎么定义、数据怎么收集与构建、规模多少、覆盖哪些场景。

### 用了什么技术
3~6 条要点列表：模型骨干与训练技术、数据规模与来源、仿真或真机平台、评估协议等。每条一行，简洁具体。

### 效果如何
2~4 句：关键实验结果与对比结论。**只能引用材料中明确出现的数字**；没有数字就定性描述，不得编造。

### 值得注意的点
2~3 条要点：亮点、适用场景或局限（材料中未提及的就写"材料未提及"，不要臆测）。

【反幻觉规则（必须遵守）】
1. 只基于"给定材料"（标题、官方摘要、日报摘要、元数据）分析，不使用外部知识补充细节；
2. 材料中没写的关键信息，写 `[未明确给出]`，不要编造方法细节或实验数字；
3. 由你推理归纳（而非材料原话）的结论，句尾标注 `[推断]`；
4. 术语首次出现时用一句括号给出通俗解释。"""

PAPER_TYPE_HINT = {
    "benchmark": "该论文疑似 benchmark/评测基准类，重点写清任务定义、数据构建流程、评估协议与指标。",
    "dataset": "该论文疑似数据集类论文，重点写清数据来源、规模、标注方式、构建流水线。",
    "survey": "该论文疑似综述类，重点写清分类框架、脉络梳理与未来方向。",
}


def build_user_prompt(
    title: str,
    url: str,
    report_summary: str,
    tags: list[str],
    meta: dict | None,
) -> str:
    """组装单次 LLM 调用的用户提示词。"""
    parts = [f"# 待解读论文\n\n- 标题：{title}\n- 原文链接：{url}"]
    if meta:
        if meta.get("authors"):
            parts.append(f"- 作者：{', '.join(meta['authors'][:8])}")
        if meta.get("date"):
            parts.append(f"- 发布日期：{meta['date']}")
        if meta.get("subjects"):
            parts.append(f"- arXiv 分类：{meta['subjects']}")
    if tags:
        parts.append(f"- 日报标签：{' '.join('#' + t for t in tags)}")
    parts.append(f"\n## 官方摘要（arXiv abs 页）\n\n{meta['abstract'] if meta and meta.get('abstract') else '[未抓取到，仅凭日报摘要分析，并在开头注明此局限]'}")
    parts.append(f"\n## 日报收录摘要（中文/英文，可能已浓缩）\n\n{report_summary or '[无]'}")
    low = title.lower()
    hints = [v for k, v in PAPER_TYPE_HINT.items() if k in low]
    if hints:
        parts.append("\n## 提示\n\n" + "\n".join(hints))
    parts.append("\n请按系统提示的固定小节输出中文 Markdown 解读。")
    return "\n".join(parts)


# ---------- 主分析入口（每篇 LLM 调用 ≤3 次） ----------


def analyze_paper(
    env: dict,
    title: str,
    url: str,
    report_summary: str = "",
    tags: list[str] | None = None,
) -> tuple[str, dict | None]:
    """生成一篇论文的解读 markdown。LLM 调用预算：最多 3 次。

    第 1 次：非流式（材料齐备时通常一次成功）；
    第 2/3 次：正文过短（推理 token 挤占）或异常时，切换流式 + 更大 max_tokens。
    返回 (解读正文, arXiv 元数据或 None)，元数据供详情页信息卡展示。
    """
    from sos_daily import call_llm, call_llm_stream  # 复用同一套 OpenAI 兼容调用

    tags = tags or []
    meta = fetch_arxiv_meta(url)
    user_prompt = build_user_prompt(title, url, report_summary, tags, meta)

    max_tokens_seq = [4000, 12000, 16000]
    last_err: Exception | None = None
    for i, max_tokens in enumerate(max_tokens_seq):
        try:
            if i == 0:
                content, _usage, _model = call_llm(
                    env, PAPER_SYSTEM_PROMPT, user_prompt, max_tokens=max_tokens
                )
            else:
                content, _usage, _model = call_llm_stream(
                    env, PAPER_SYSTEM_PROMPT, user_prompt, max_tokens=max_tokens
                )
            if len(content.strip()) >= 30:
                return content.strip(), meta
            last_err = RuntimeError(f"第 {i + 1} 次调用正文过短（{len(content.strip())} 字符）")
            print(f"[PaperBrief] {last_err}，重试…")
        except Exception as exc:  # noqa: BLE001 - 预算内重试
            last_err = exc
            print(f"[PaperBrief] 第 {i + 1} 次调用失败（{type(exc).__name__}），重试…")
    raise RuntimeError(f"论文解读生成失败（已用满 3 次调用预算）：{last_err}")


# ---------- 缓存（B3：按 arxiv_id 全局唯一，同一篇论文跨日/跨板块只解读一次） ----------


def cache_path(arxiv_id: str) -> Path:
    return PAPERS_DIR / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', arxiv_id)}.md"


def load_cached(arxiv_id: str) -> str | None:
    p = cache_path(arxiv_id)
    if p.exists():
        try:
            text = p.read_text(encoding="utf-8")
            # 头部注释（<!-- ... --> 元信息）之后的正文
            m = re.match(r"\s*<!--.*?-->\s*", text, re.S)
            return text[m.end():] if m else text
        except Exception:
            return None
    return None


def save_cached(
    arxiv_id: str,
    title: str,
    url: str,
    content: str,
    score: float | None = None,
    meta: dict | None = None,
) -> Path:
    p = cache_path(arxiv_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    m = meta or {}
    authors = "; ".join(m.get("authors") or [])
    subjects = (m.get("subjects") or "").replace("\n", " ")
    abstract = (m.get("abstract") or "").replace("\n", " ")
    head = (
        f"<!--\ntitle: {title}\nurl: {url}\nscore: {score if score is not None else '-'}\n"
        f"generated_at: {datetime.now().isoformat(timespec='seconds')}\n"
        f"authors: {authors}\nsubjects: {subjects}\nabstract: {abstract}\n-->\n\n"
    )
    p.write_text(head + content, encoding="utf-8")
    return p


def migrate_old_cache() -> int:
    """一次性迁移：旧缓存 out/papers/{date}/{anchor}.md（头部含 url）挪到 arxiv_id 全局缓存。幂等。"""
    moved = 0
    if not PAPERS_DIR.exists():
        return 0
    for old in sorted(PAPERS_DIR.glob("*/item-*.md")):
        try:
            head = old.read_text(encoding="utf-8")[:4000]
            m = re.search(r"^url: (\S+)", head, re.M)
            aid = extract_arxiv_id(m.group(1)) if m else None
            if not aid:
                continue
            dst = cache_path(aid)
            if dst.exists():
                continue
            dst.write_text(old.read_text(encoding="utf-8"), encoding="utf-8")
            moved += 1
        except Exception as exc:  # noqa: BLE001 - 迁移失败不影响服务
            print(f"[PaperBrief] 旧缓存迁移失败 {old.name}: {exc}")
    if moved:
        print(f"[PaperBrief] 旧缓存迁移完成：{moved} 篇 → out/papers/{{arxiv_id}}.md")
    return moved
