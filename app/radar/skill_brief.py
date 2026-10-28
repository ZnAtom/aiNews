"""Skill AI 推荐词 skill：对推送/搜索命中的 Claude Code Skill 生成中文评测式简介。

与 paper_brief / ghrepo_brief 同一套模式：免费抓材料（描述太短时抓仓库 README
首段，复用 ghrepo_brief 的抓取与清理）→ 单次 LLM 调用 → 结构化 JSON（verdict /
when_use / pros / caveats）由 webapp 落 feedback.db skill_briefs 表，全局复用。

评测式口吻（B24 用户定稿）：客观冷静、开头给明确结论（推荐/值得试/一般/不推荐），
不夹带吹捧；材料不足就下调置信，严禁编造功能与数字。
"""

import json
import re

from ghrepo_brief import fetch_gh_readme  # 复用 README 抓取（含清理与代理回退）

# 简介只需少量材料，README 截断比项目解读短得多
README_SNIPPET_CHARS = 1200
# 原始描述短于该长度时才补抓 README（节省 GitHub API 配额，多数条目描述够用）
DESC_MIN_CHARS = 40

SKILL_SYSTEM_PROMPT = """你是一名客观严谨的 AI 编程技能评测员，为用户评测 Claude Code Skill（可安装的技能包，安装后扩展 AI 助手在某类任务上的能力）。

【输出】只输出一个 JSON 对象，不要 markdown 代码块、不要任何多余文字：
{
  "verdict": "结论句：以「推荐」「值得试」「一般」「不推荐」四档之一开头，加一句话定位（总共不超过 45 字）",
  "when_use": "什么时候用：1~2 个具体使用场景，用「当你需要…时」句式（30~60 字）",
  "pros": "好在哪：1~2 点落到实处的能力描述，不写空话（20~50 字）",
  "caveats": "注意事项：前置条件或坑（如需装依赖、要 API key、仅限某平台）；材料看不出就写空字符串"
}

【评测规则（必须遵守）】
1. 客观冷静，不夸大不吹捧；结论必须基于给定材料，材料单薄时主动下调置信（用「值得试」而非「推荐」）；
2. 全部用中文，专有名词（工具名、协议名）保留英文；
3. 严禁编造材料中没有的功能、数字与背书；star 数仅代表热度，不代表质量结论；
4. verdict 是给卡片直接展示的推荐词，必须独立成句、信息完整，让人不点开就知道这是什么、值不值得装。"""


def build_user_prompt(sk: dict, readme: str | None) -> str:
    """组装单次 LLM 调用的用户提示词。sk 为 skills 表行（dict）。"""
    desc = (sk.get("description") or "").strip()
    parts = [
        "# 待评测技能",
        f"- 名称：{sk.get('name') or '(未命名)'}",
        f"- 来源：{sk.get('origin') or '未知'}",
        f"- 链接：{sk.get('source_url') or '(无)'}",
        f"- 分类：{sk.get('category') or '未分类'}",
        f"- GitHub Star：{sk.get('stars', 0)}",
        f"- 是否官方出品：{'是' if sk.get('verified') else '否'}",
        f"- 原始描述：{desc[:500] if desc else '（无）'}",
    ]
    parts.append(
        "\n## README 摘要（仓库抓取，可能缺失）\n\n"
        + (readme or "[未抓取到，仅凭描述评测；材料不足请下调结论]")
    )
    parts.append("\n请只输出 JSON。")
    return "\n".join(parts)


def _extract_json(text: str) -> dict:
    """从 LLM 输出中提取 JSON 对象（容忍代码围栏与前后杂文字）。"""
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"输出中找不到 JSON 对象：{text[:120]}")
    return json.loads(m.group(0))


def generate_brief(env: dict, sk: dict) -> dict:
    """生成一条技能的评测式简介。LLM 调用预算 ≤2 次（非流式 → 流式重试）。

    预算与 ghrepo_brief 对齐（4000/12000）：思考型模型会在推理上耗掉大量 token，
    max_tokens 给小了会返回空 content，解析不出 JSON。

    返回 {"verdict", "when_use", "pros", "caveats"}；描述太短时自动补抓 README。
    失败抛异常（调用方 webapp 记内存错误，前端回退显示原始描述）。
    """
    from sos_daily import call_llm, call_llm_stream  # 复用同一套 OpenAI 兼容调用

    desc = (sk.get("description") or "").strip()
    readme = None
    if len(desc) < DESC_MIN_CHARS and sk.get("github_repo"):
        raw = fetch_gh_readme(sk["github_repo"])
        readme = raw[:README_SNIPPET_CHARS] if raw else None

    user_prompt = build_user_prompt(sk, readme)
    last_err: Exception | None = None
    for i, (max_tokens, stream) in enumerate(((4000, False), (12000, True))):
        try:
            if stream:
                content, _usage, _model = call_llm_stream(
                    env, SKILL_SYSTEM_PROMPT, user_prompt, max_tokens=max_tokens
                )
            else:
                content, _usage, _model = call_llm(
                    env, SKILL_SYSTEM_PROMPT, user_prompt, max_tokens=max_tokens
                )
            data = _extract_json(content)
            verdict = str(data.get("verdict") or "").strip()
            when_use = str(data.get("when_use") or "").strip()
            pros = str(data.get("pros") or "").strip()
            caveats = str(data.get("caveats") or "").strip()
            if not verdict or not when_use:
                raise ValueError(f"关键字段缺失：verdict={verdict[:40]!r} when={when_use[:40]!r}")
            return {"verdict": verdict, "when_use": when_use,
                    "pros": pros, "caveats": caveats}
        except Exception as exc:  # noqa: BLE001 - 预算内重试
            last_err = exc
            print(f"[SkillBrief] {sk.get('name')}: 第 {i + 1} 次调用失败"
                  f"（{type(exc).__name__}），重试…")
    raise RuntimeError(f"推荐词生成失败（已用满 2 次调用预算）：{last_err}")


# ---------------- B25 深度解读（/skill 详情页，模式对齐 ghrepo_brief） ----------------

DETAIL_README_CHARS = 6000  # 解读材料更足，README 截断对齐 ghrepo_brief

SKILL_DETAIL_SYSTEM_PROMPT = """你是一名客观严谨的 AI 编程技能评测员，为 Claude Code Skill（可安装的技能包，安装后扩展 AI 助手在某类任务上的能力）写一篇深度解读页，帮读者决定要不要装、装了怎么用。

【输出】只输出 markdown 正文，不要代码块围栏、不要开场白。固定用以下小节（## 二级标题）：
## 这是什么
2~3 句：一句话定位 + 解决什么问题 + 背后的项目/作者（材料有才写）。
## 什么时候用
2~4 条要点列表：典型使用场景与人群（README 中提到的优先）。
## 怎么安装
材料中写明安装方式就如实列出（用列表，命令用反引号包住）；材料没写安装步骤时，写「官方材料未写明安装步骤，请以仓库 README 为准」，可用 Claude Code 生态通识补充推荐途径，句尾标注 [推断]。
## 怎么用
2~4 条要点：如何触发（命令/对话说法/自动生效）、上手示例；材料没写就如实说 [未明确给出] 并给出最可能的触发方式（标 [推断]）。
## 为什么好
2~4 条要点：设计亮点、省事在哪、与同类相比的差异化（依据材料归纳，不夸大）。
## 注意什么
2~3 条要点：前置条件、平台限制、维护活跃度等；材料看不出就写「材料中未见明显注意事项」。
## 没有它用什么
1~3 条要点：同类替代方案或该技能出现前的常见做法；材料没提的可用生态通识补充，推理得出的句尾标注 [推断]。

【写作规则（必须遵守）】
1. 主要依据「给定材料」（README、官方简介、star/官方数据）；
2. 开源领域的公认可识（同类项目、替代方案、技术背景）可以使用，但由你归纳推理的结论句尾标注 [推断]；
3. 具体数字（star 数、版本号、性能数据）只能来自材料，材料中没有就写 [未明确给出]，不得编造；
4. 全部用中文，专有名词（工具名、命令、路径）保留英文；客观冷静不吹捧，材料单薄就明说材料单薄。"""


def build_detail_user_prompt(sk: dict, readme: str | None) -> str:
    """组装深度解读的用户提示词。sk 为 skills 表行（dict）。"""
    desc = (sk.get("description") or "").strip()
    parts = [
        "# 待解读技能",
        f"- 名称：{sk.get('name') or '(未命名)'}",
        f"- 来源：{sk.get('origin') or '未知'}",
        f"- 链接：{sk.get('source_url') or '(无)'}",
        f"- 分类：{sk.get('category') or '未分类'}",
        f"- GitHub Star：{sk.get('stars', 0)}",
        f"- 是否官方出品：{'是' if sk.get('verified') else '否'}",
        f"- 原始描述：{desc[:800] if desc else '（无）'}",
    ]
    parts.append(
        "\n## README（GitHub 仓库页抓取，可能已截断或缺失）\n\n"
        + (readme or "[未抓取到，仅凭描述解读；材料不足请如实说明]")
    )
    parts.append("\n请只输出 markdown 正文。")
    return "\n".join(parts)


def generate_detail(env: dict, sk: dict) -> str:
    """生成一条技能的深度解读（markdown）。LLM 调用预算 ≤3 次，阶梯对齐 ghrepo_brief。

    README 抓取策略与推荐词不同：只要仓库存在就抓（解读需要足够材料，且结果
    永久缓存，一次成本换长期复用）。
    """
    from sos_daily import call_llm, call_llm_stream

    readme = None
    if sk.get("github_repo"):
        raw = fetch_gh_readme(sk["github_repo"])
        readme = raw[:DETAIL_README_CHARS] if raw else None

    user_prompt = build_detail_user_prompt(sk, readme)
    last_err: Exception | None = None
    for i, (max_tokens, stream) in enumerate(
        ((4000, False), (12000, True), (16000, True))
    ):
        try:
            if stream:
                content, _usage, _model = call_llm_stream(
                    env, SKILL_DETAIL_SYSTEM_PROMPT, user_prompt, max_tokens=max_tokens
                )
            else:
                content, _usage, _model = call_llm(
                    env, SKILL_DETAIL_SYSTEM_PROMPT, user_prompt, max_tokens=max_tokens
                )
            text = (content or "").strip()
            if len(text) < 200 or "## " not in text:
                raise ValueError(f"输出不像解读正文（{len(text)} 字符），疑似空转")
            return text
        except Exception as exc:  # noqa: BLE001 - 预算内重试
            last_err = exc
            print(f"[SkillDetail] {sk.get('name')}: 第 {i + 1} 次调用失败"
                  f"（{type(exc).__name__}），重试…")
    raise RuntimeError(f"深度解读生成失败（已用满 3 次调用预算）：{last_err}")
