#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B7：日报摘要风格切换——切换 ai-news profile 的 enrichment.md（逐条摘要的 LLM 文风 prompt）。

安全设计：
- 只动 enrichment.md（摘要文风），评分 rubric analysis.md 与 profile.json 一律不碰；
- 首次切换前自动把当前文件备份为 enrichment.base.md，"我的调优版"随时可切回；
- 当前风格按文件内容实时匹配，不存状态文件——手动改过文件也能正确显示；
- Horizon 管道每次运行重新加载 profile，切换后下一次日报（次日 08:00）即生效。
"""

import shutil
import threading
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
# profiles_dir 以 Horizon config.json 的 processing.profiles_dir 为准，读不到再用默认
_HORIZON_CONFIG = PROJECT_ROOT / "app" / "Horizon" / "data" / "config.json"
_DEFAULT_PROFILES_DIR = PROJECT_ROOT / "config" / "profiles"
PROFILE_ID = "ai-news"
ENRICH_NAME = "enrichment.md"
BASE_NAME = "enrichment.base.md"

_STYLE_CONCISE = """# Role

你是中文科技编辑，帮助读者高效、准确地了解 AI 与具身智能领域每天发生了什么。

# Blocks

- `summary`：用**简体中文**写 1~2 个完整句子（不超过 80 字），说清"谁发布了/发现了什么，对谁有什么用"。保留决定性细节：具体名称、版本、数字、可用性、兼容性或条件。区分"官方宣称"与"已实测结果"；不要为素材稀少的条目编造细节或堆砌空话。不得使用英文整句，专有名词/模型名可保留英文。

# Profile writing rules

- 标题用简体中文，不超过 20 个字，不用标题党；保留产品/项目/模型名（可英文）与关键变化。
- 每个输出块都必须是完整句子，具体、不重叠。
- 禁用套话，如"标志着重大里程碑""将重塑行业"，除非证据明确支持具体影响。"""

_STYLE_DEEP = """# Role

你是深度科技专栏编辑，帮助读者不仅知道"发生了什么"，还理解"为什么重要"。

# Blocks

- `summary`：用**简体中文**写 3~4 个完整句子（不超过 160 字），按"事实 → 为什么重要 → 背景影响"展开：先说清谁发布了/发现了什么（保留具体名称、版本、数字、可用性等决定性细节），再点出它对行业或读者的实际意义，最后补充必要的前作对比或背景。区分"官方宣称"与"已实测结果"；不确定的不写，不得编造细节。不得使用英文整句，专有名词/模型名可保留英文。

# Profile writing rules

- 标题用简体中文，不超过 20 个字，不用标题党；保留产品/项目/模型名（可英文）与关键变化。
- 每个输出块都必须是完整句子，句子之间要有信息增量，不要换一种说法重复同一件事。
- 禁用套话，如"标志着重大里程碑""将重塑行业"，除非证据明确支持具体影响。"""

_STYLE_CASUAL = """# Role

你是懂技术的朋友，在聊天里给读者转述今天 AI 圈发生了什么，轻松但准确。

# Blocks

- `summary`：用**简体中文**写 1~2 个完整句子（不超过 80 字），像朋友转发消息一样口语化，但必须保留关键事实：谁、发布了什么、数字或版本。不堆感叹号，不编细节，拿不准的不说。不得使用英文整句，专有名词/模型名可保留英文。

# Profile writing rules

- 标题用简体中文，不超过 20 个字，不用标题党；保留产品/项目/模型名（可英文）与关键变化。
- 每个输出块都必须是完整句子，具体、不重叠。
- 口语化不等于失真：数字、名称、结论必须与素材一致。"""

STYLES = [
    {
        "id": "concise",
        "name": "简洁快讯",
        "desc": "每条 1~2 句话，只留决定性细节（默认）",
        "prompt": _STYLE_CONCISE,
    },
    {
        "id": "deep",
        "name": "深度解读",
        "desc": "每条 3~4 句：事实 + 为什么重要 + 背景影响",
        "prompt": _STYLE_DEEP,
    },
    {
        "id": "casual",
        "name": "轻松口语",
        "desc": "朋友转消息的口吻，轻松一点，事实不缩水",
        "prompt": _STYLE_CASUAL,
    },
]

_LOCK = threading.Lock()


def _profiles_dir() -> Path:
    try:
        import json

        cfg = json.loads(_HORIZON_CONFIG.read_text(encoding="utf-8"))
        d = (cfg.get("processing") or {}).get("profiles_dir")
        if d:
            return Path(d)
    except Exception:
        pass
    return _DEFAULT_PROFILES_DIR


def _enrich_path() -> Path:
    return _profiles_dir() / PROFILE_ID / ENRICH_NAME


def _base_path() -> Path:
    return _profiles_dir() / PROFILE_ID / BASE_NAME


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def list_styles() -> dict:
    """返回风格预设列表、当前选中项（按文件内容实时匹配）与备份状态。"""
    current_file = _read(_enrich_path())
    base_content = _read(_base_path())
    has_backup = bool(base_content)
    # "我的调优版"仅当备份内容与任何预设都不同（否则和默认重复，不展示）
    base_is_unique = has_backup and not any(
        s["prompt"].strip() == base_content for s in STYLES
    )
    current = next(
        (s["id"] for s in STYLES if s["prompt"].strip() == current_file), None
    )
    if current is None:
        current = "custom"  # 内容不匹配任何预设：视为手调版

    styles = [{"id": s["id"], "name": s["name"], "desc": s["desc"]} for s in STYLES]
    if base_is_unique:
        styles.append(
            {
                "id": "custom",
                "name": "我的调优版",
                "desc": "切换前自动备份的你的手调版本",
            }
        )
    return {"current": current, "styles": styles, "has_backup": has_backup}


def set_style(style_id: str) -> dict:
    """切换风格：首次切换先把当前内容备份为 base，再写入目标预设。"""
    target = next((s for s in STYLES if s["id"] == style_id), None)
    base_content = _read(_base_path())
    if target is None:
        if style_id != "custom":
            raise ValueError(f"未知风格：{style_id}")
        if not base_content:
            raise ValueError("没有可用的调优版备份")
        prompt = base_content
    else:
        prompt = target["prompt"]

    with _LOCK:
        enrich = _enrich_path()
        base = _base_path()
        # 备份策略：base 不存在才备份当前内容（首个切换动作），已有备份永不覆盖
        if not base.exists() and enrich.exists():
            shutil.copyfile(enrich, base)
        enrich.parent.mkdir(parents=True, exist_ok=True)
        enrich.write_text(prompt.rstrip("\n") + "\n", encoding="utf-8")
    return list_styles()
