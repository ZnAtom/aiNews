#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Skills 推荐板块（B23）：多源采集 → 本地缓存 → 去重 → 每日推荐 → 搜索。

采集源（4 路，全部走 api.github.com，raw.githubusercontent.com 兜底）：
  1. agentskillexchange/skills  —— skills.json 全量目录（3100+ 条，自带分类）
  2. anthropics/skills          —— skills/*/SKILL.md（官方技能）
  3. obra/superpowers           —— skills/*/SKILL.md（社区经典）
  4. GitHub topic:claude-skills —— 高 star 仓库（search API）

推荐（每天 5 条）：score = 0.45*star_norm + 0.25*fresh + 0.30*cat_pref，
每类当日 ≤2 条；skill_daily 冻结批次——昨天整批 0 交互则原批重推（说明没看），
部分交互则未交互的续推 + 补新；曝光 ≥3 且 0 交互 → 静默（muted）。

供 webapp.py（🧩 今日 Skills 板块 / 搜索 / 收藏）与 skills_sync.py（每日采集）共用。
"""

import json
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import httpx

RADAR_DIR = Path(__file__).resolve().parent
DB_PATH = RADAR_DIR / "data" / "feedback.db"

DAILY_PICKS = 5          # 每天推荐条数
PER_CATEGORY_CAP = 2     # 每类当日最多几条
MUTE_IMPRESSIONS = 3     # 曝光满 3 次仍 0 交互 → 静默
TOPIC_REPOS = 40         # topic 搜索取前 40 个高 star 仓库

_GH_API = "https://api.github.com"
_GH_TIMEOUT = 30

# agentskillexchange 英文分类 → 中文板块（与用户语境一致：写代码/做科研/写文章…）
CATEGORY_MAP = {
    "Developer Tools": "写代码",
    "Code Quality & Review": "写代码",
    "CI/CD Integrations": "写代码",
    "Library & API Reference": "写代码",
    "Templates & Workflows": "写代码",
    "Security & Verification": "写代码",
    "Data Extraction & Transformation": "数据自动化",
    "Monitoring & Alerts": "数据自动化",
    "Runbooks & Diagnostics": "数据自动化",
    "Integrations & Connectors": "数据自动化",
    "Browser Automation": "数据自动化",
    "Content Writing & SEO": "写作内容",
    "Media & Transcription": "写作内容",
    "Calendar, Email & Productivity": "写作内容",
    "Image & Creative Automation": "创意设计",
    "WordPress & CMS": "写作内容",
    "Research & Scraping": "科研",
}

_FRONT_RE = re.compile(r"\A---\s*\n(.*?)\n---", re.DOTALL)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_tables() -> None:
    """建 Skills 四表（webapp 启动时与 sync 前都会调用，幂等）。"""
    with _conn() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS skills (
                dedup_key TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                category TEXT DEFAULT '其他',
                origin TEXT DEFAULT '',
                source_url TEXT DEFAULT '',
                github_repo TEXT DEFAULT '',
                stars INTEGER DEFAULT 0,
                verified INTEGER DEFAULT 0,
                pushed_at TEXT DEFAULT '',
                first_seen TEXT,
                updated_at TEXT
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS skill_stats (
                dedup_key TEXT PRIMARY KEY,
                impressions INTEGER DEFAULT 0,
                clicks INTEGER DEFAULT 0,
                favorites INTEGER DEFAULT 0,
                votes_up INTEGER DEFAULT 0,
                votes_down INTEGER DEFAULT 0,
                last_shown_day TEXT DEFAULT '',
                updated_at TEXT
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS skill_favorites (
                dedup_key TEXT PRIMARY KEY,
                favorited_at TEXT
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS skill_daily (
                day TEXT NOT NULL,
                dedup_key TEXT NOT NULL,
                PRIMARY KEY (day, dedup_key)
            )"""
        )


def _norm_key(name: str, repo: str = "") -> str:
    """去重键：规范化名称（+ 仓库限定），同一技能多处出现只存一条。"""
    n = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", (name or "").lower())
    return (repo + "::" + n) if repo else n


def _upsert_skill(conn, dedup_key: str, name: str, description: str,
                  category: str, origin: str, source_url: str,
                  github_repo: str, stars: int, verified: bool,
                  pushed_at: str) -> bool:
    """写入/更新一条技能。新入库返回 True（用于统计本轮采集量）。"""
    now = datetime.now().isoformat(timespec="seconds")
    existing = conn.execute(
        "SELECT 1 FROM skills WHERE dedup_key = ?", (dedup_key,)
    ).fetchone()
    if existing:
        conn.execute(
            """UPDATE skills SET name=?, description=?, category=?, stars=?,
               verified=?, pushed_at=CASE WHEN ?<>'' THEN ? ELSE pushed_at END,
               updated_at=? WHERE dedup_key=?""",
            (name, description, category, stars, int(verified), pushed_at,
             pushed_at, now, dedup_key),
        )
        return False
    conn.execute(
        """INSERT INTO skills (dedup_key, name, description, category, origin,
           source_url, github_repo, stars, verified, pushed_at, first_seen, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (dedup_key, name, description, category, origin, source_url,
         github_repo, stars, int(verified), pushed_at, now, now),
    )
    return True


# ---------------- 采集（skills_sync.py 每日调用） ----------------

def _gh_get(url: str, params: dict = None) -> dict:
    resp = httpx.get(url, params=params or {}, timeout=_GH_TIMEOUT,
                     headers={"Accept": "application/vnd.github+json"},
                     follow_redirects=True)
    resp.raise_for_status()
    return resp.json()


def _gh_blob_text(repo: str, sha: str) -> str:
    """blob API 拉文件内容（base64 → utf-8）。raw 直连慢，统一走 api.github.com。"""
    d = _gh_get(f"{_GH_API}/repos/{repo}/git/blobs/{sha}")
    import base64
    return base64.b64decode(d.get("content") or "").decode("utf-8", "replace")


def _fetch_agentskillexchange(conn) -> int:
    """源 1：agentskillexchange/skills 的 skills.json 全量目录。"""
    tree = _gh_get(f"{_GH_API}/repos/agentskillexchange/skills/git/trees/main")
    sha = next((t["sha"] for t in tree.get("tree", [])
                if t.get("path") == "skills.json"), None)
    if not sha:
        print("[skills] agentskillexchange: 未找到 skills.json，跳过")
        return 0
    obj = json.loads(_gh_blob_text("agentskillexchange/skills", sha))
    added = 0
    for s in obj.get("skills", []):
        name = (s.get("name") or s.get("title") or s.get("slug") or "").strip()
        if not name:
            continue
        cat_en = s.get("category") or "其他"
        if isinstance(cat_en, list):
            cat_en = cat_en[0] if cat_en else "其他"
        cat = CATEGORY_MAP.get(str(cat_en).strip(), "其他")
        added += _upsert_skill(
            conn, _norm_key(name), name, (s.get("description") or "")[:500],
            cat, "agentskillexchange", s.get("source_url") or "",
            s.get("github_repo") or "", 0,
            s.get("verification") == "security_reviewed", "",
        )
    print(f"[skills] agentskillexchange: 全量 {obj.get('total', '?')} 条，新增 {added}")
    return added


def _parse_frontmatter(text: str) -> tuple:
    """解析 SKILL.md 的 YAML frontmatter（只取 name/description，不引 yaml 库）。"""
    m = _FRONT_RE.match(text or "")
    if not m:
        return "", ""
    name, desc = "", ""
    for line in m.group(1).splitlines():
        lm = re.match(r'^(name|description):\s*"?(.*?)"?\s*$', line)
        if lm:
            if lm.group(1) == "name":
                name = lm.group(2)
            else:
                desc = lm.group(2)
    return name, desc


def _fetch_skillmd_repo(conn, repo: str, origin: str) -> int:
    """源 2/3：anthropics/skills、obra/superpowers —— 仓库内任意 skills/*/SKILL.md。

    两个仓库的目录布局不同（anthropics 是 document-skills/docx/SKILL.md，
    superpowers 在 plugins/superpowers/skills/ 下），因此只按 "SKILL.md 结尾" 匹配。
    """
    try:
        tree = _gh_get(f"{_GH_API}/repos/{repo}/git/trees/main?recursive=1")
    except Exception as exc:
        print(f"[skills] {repo}: 目录树获取失败（{exc}），跳过")
        return 0
    paths = [t for t in tree.get("tree", [])
             if t.get("path", "").endswith("SKILL.md")]
    repo_pushed = ""
    try:
        repo_pushed = _gh_get(f"{_GH_API}/repos/{repo}").get("pushed_at") or ""
    except Exception:
        pass
    added = 0
    for t in paths:
        try:
            fm_name, desc = _parse_frontmatter(_gh_blob_text(repo, t["sha"]))
        except Exception:
            continue
        dirname = t["path"].split("/")[-2]  # 技能所在目录名（倒数第二段）
        name = fm_name or dirname
        if not name:
            continue
        added += _upsert_skill(
            conn, _norm_key(name, repo.split("/")[1]), name, desc[:500],
            _guess_skillmd_category(dirname, desc), origin,
            f"https://github.com/{repo}/blob/main/{t['path']}",
            repo, 0, False, repo_pushed,
        )
    print(f"[skills] {repo}: SKILL.md {len(paths)} 个，新增 {added}")
    return added


# 官方/社区仓库无自带分类，按目录名+描述关键词粗归 4 类
_SKILLMD_CATS = [
    (("frontend", "design", "canvas", "theme", "art", "brand", "slack-gif"), "创意设计"),
    (("docx", "pptx", "xlsx", "pdf", "comms", "doc-coauthor", "writing"), "写作内容"),
    (("webapp-testing", "web-artifacts", "mcp", "claude-api", "api"), "写代码"),
]


def _guess_skillmd_category(dirname: str, desc: str) -> str:
    text = (dirname + " " + desc).lower()
    for keys, cat in _SKILLMD_CATS:
        if any(k in text for k in keys):
            return cat
    return "写代码"


def _fetch_topic_repos(conn) -> int:
    """源 4：GitHub topic:claude-skills 高 star 仓库（⭐ GitHub 精选）。"""
    try:
        d = _gh_get(f"{_GH_API}/search/repositories",
                    {"q": "topic:claude-skills", "sort": "stars",
                     "order": "desc", "per_page": TOPIC_REPOS})
    except Exception as exc:
        print(f"[skills] topic:claude-skills: 搜索失败（{exc}），跳过")
        return 0
    added = 0
    for it in d.get("items", []):
        full = it.get("full_name") or ""
        name = (it.get("name") or full).replace("-", " ")
        desc = it.get("description") or ""
        added += _upsert_skill(
            conn, _norm_key(name, full), name, desc[:500], "GitHub 精选",
            "github-topic", it.get("html_url") or "", full,
            it.get("stargazers_count") or 0, False, it.get("pushed_at") or "",
        )
    print(f"[skills] topic:claude-skills: {d.get('total_count', '?')} 仓库取前 "
          f"{len(d.get('items', []))}，新增 {added}")
    return added


def sync_all() -> dict:
    """全量采集 4 路 + 去重入库（skills_sync.py 入口）。"""
    init_tables()
    total = 0
    with _conn() as conn:
        total += _fetch_agentskillexchange(conn)
        total += _fetch_skillmd_repo(conn, "anthropics/skills", "anthropics")
        total += _fetch_skillmd_repo(conn, "obra/superpowers", "superpowers")
        total += _fetch_topic_repos(conn)
        n = conn.execute("SELECT COUNT(*) AS c FROM skills").fetchone()["c"]
    print(f"[skills] 采集完成：本轮新增 {total}，全库 {n} 条")
    return {"added": total, "total": n}


# ---------------- 静默 / 交互 ----------------

def _muted_keys(conn) -> set:
    """曝光 ≥ MUTE_IMPRESSIONS 且 0 交互（点击/收藏/好评）→ 用户不感兴趣。"""
    rows = conn.execute(
        """SELECT dedup_key FROM skill_stats
           WHERE impressions >= ? AND clicks = 0 AND favorites = 0 AND votes_up = 0""",
        (MUTE_IMPRESSIONS,),
    ).fetchall()
    return {r["dedup_key"] for r in rows}


def record_impressions(conn, keys: list, day: str) -> None:
    """曝光埋点（skill_daily 入批时调用，每条每天只计一次）。"""
    now = datetime.now().isoformat(timespec="seconds")
    for k in keys:
        conn.execute(
            """INSERT INTO skill_stats (dedup_key, impressions, last_shown_day, updated_at)
               VALUES (?, 1, ?, ?)
               ON CONFLICT(dedup_key) DO UPDATE SET
                 impressions = impressions + 1,
                 last_shown_day = excluded.last_shown_day,
                 updated_at = excluded.updated_at""",
            (k, day, now),
        )


def _bump(conn, dedup_key: str, field: str, delta: int = 1) -> None:
    assert field in ("clicks", "favorites", "votes_up", "votes_down")
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        f"""INSERT INTO skill_stats (dedup_key, {field}, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(dedup_key) DO UPDATE SET
              {field} = {field} + ?, updated_at = excluded.updated_at""",
        (dedup_key, delta, now, delta),
    )


# ---------------- 每日推荐 ----------------

def _score_rows(conn, candidates: list) -> dict:
    """score = 0.45*star_norm + 0.25*fresh + 0.30*cat_pref（+0.05 安全审核加成）。

    star_norm：有 star 的在候选内 min-max 归一；0 star 一律 0.25 基线。
    fresh：GitHub 仓库按 pushed_at 距今指数衰减；无日期的官方/目录源给固定中性值。
    cat_pref：该分类收藏数 / 最大分类收藏数（没有收藏时全 0）。
    """
    stars = [c["stars"] for c in candidates]
    smax = max(stars) if stars else 0
    fav_rows = conn.execute(
        """SELECT s.category AS cat, COUNT(*) AS c
           FROM skill_favorites f JOIN skills s ON s.dedup_key = f.dedup_key
           GROUP BY s.category"""
    ).fetchall()
    fav_by_cat = {r["cat"]: r["c"] for r in fav_rows}
    fav_max = max(fav_by_cat.values()) if fav_by_cat else 0
    now = datetime.now()
    scores = {}
    for c in candidates:
        star_norm = (c["stars"] / smax) if smax else 0.0
        if not c["stars"]:
            star_norm = 0.25
        if c["pushed_at"]:
            try:
                days = max((now - datetime.fromisoformat(
                    c["pushed_at"].replace("Z", "+00:00")).replace(tzinfo=None)).days, 0)
                fresh = pow(0.5, days / 30.0)  # 半衰期 30 天
            except ValueError:
                fresh = 0.5
        elif c["origin"] in ("anthropics", "superpowers"):
            fresh = 0.8
        else:
            fresh = 0.5
        cat_pref = (fav_by_cat.get(c["category"], 0) / fav_max) if fav_max else 0.0
        scores[c["dedup_key"]] = (0.45 * star_norm + 0.25 * fresh +
                                  0.30 * cat_pref + (0.05 if c["verified"] else 0.0))
    return scores


def today_picks(day: str = None, k: int = DAILY_PICKS) -> list:
    """今日 5 条：skill_daily 冻结批次（昨日 0 交互原批重推）+ 补新 + 曝光埋点。"""
    init_tables()
    day = day or datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=1)
                 ).strftime("%Y-%m-%d")
    with _conn() as conn:
        existing = [r["dedup_key"] for r in conn.execute(
            "SELECT dedup_key FROM skill_daily WHERE day = ? ORDER BY rowid", (day,))]
        if existing:
            return _hydrate(conn, existing)

        # 冻结批次：昨日批次里 0 交互的续推（整天没看 → 原批重推）
        y_keys = [r["dedup_key"] for r in conn.execute(
            "SELECT dedup_key FROM skill_daily WHERE day = ?", (yesterday,))]
        keep = []
        if y_keys:
            ph = ",".join("?" * len(y_keys))
            stats = {r["dedup_key"]: r for r in conn.execute(
                f"SELECT * FROM skill_stats WHERE dedup_key IN ({ph})", y_keys)}
            for kkey in y_keys:
                st = stats.get(kkey)
                interacted = st and (st["clicks"] or st["favorites"] or st["votes_up"])
                if not interacted:
                    keep.append(kkey)
        picks = keep[:]

        # 补新：排除 muted / 已选 / 昨日已推（避免刚静默又出现）
        exclude = set(picks) | _muted_keys(conn) | set(y_keys)
        ph = ",".join("?" * len(exclude)) if exclude else "''"
        cands = [dict(r) for r in conn.execute(
            f"""SELECT * FROM skills
                WHERE dedup_key NOT IN ({ph})
                AND dedup_key NOT IN (SELECT dedup_key FROM skill_daily WHERE day = ?)""",
            list(exclude) + [day],
        )]
        scores = _score_rows(conn, cands)
        cands.sort(key=lambda c: scores.get(c["dedup_key"], 0), reverse=True)
        cat_count = {}
        for c in cands:
            if len(picks) >= k:
                break
            cat = c["category"]
            if cat_count.get(cat, 0) >= PER_CATEGORY_CAP:
                continue
            picks.append(c["dedup_key"])
            cat_count[cat] = cat_count.get(cat, 0) + 1

        for kkey in picks:
            conn.execute("INSERT OR IGNORE INTO skill_daily (day, dedup_key) VALUES (?, ?)",
                         (day, kkey))
        record_impressions(conn, picks, day)
        return _hydrate(conn, picks)


def _hydrate(conn, keys: list) -> list:
    """dedup_key 列表 → 完整条目（含统计/收藏标记），保持传入顺序。"""
    if not keys:
        return []
    ph = ",".join("?" * len(keys))
    rows = conn.execute(
        f"""SELECT s.*, COALESCE(st.impressions, 0) AS impressions, COALESCE(st.clicks, 0) AS clicks,
                   COALESCE(st.votes_up, 0) AS votes_up, COALESCE(st.votes_down, 0) AS votes_down,
                   CASE WHEN f.dedup_key IS NULL THEN 0 ELSE 1 END AS favorited
            FROM skills s
            LEFT JOIN skill_stats st ON st.dedup_key = s.dedup_key
            LEFT JOIN skill_favorites f ON f.dedup_key = s.dedup_key
            WHERE s.dedup_key IN ({ph})""",
        keys,
    ).fetchall()
    by_key = {r["dedup_key"]: dict(r) for r in rows}
    return [by_key[kk] for kk in keys if kk in by_key]


# ---------------- 搜索 ----------------

def search_skills(q: str = "", category: str = "", limit: int = 30) -> list:
    """全库搜索：收藏优先 → 净好评 → 相关性 → star。

    相关性：名称命中 ×3 + 描述命中 ×1（q 为空时按 star 排）。
    muted（曝光 ≥3 零交互）不出现。
    """
    init_tables()
    with _conn() as conn:
        muted = _muted_keys(conn)
        rows = [dict(r) for r in conn.execute(
            """SELECT s.*, COALESCE(st.impressions, 0) AS impressions, COALESCE(st.clicks, 0) AS clicks,
                      COALESCE(st.votes_up, 0) AS votes_up, COALESCE(st.votes_down, 0) AS votes_down,
                      CASE WHEN f.dedup_key IS NULL THEN 0 ELSE 1 END AS favorited
               FROM skills s
               LEFT JOIN skill_stats st ON st.dedup_key = s.dedup_key
               LEFT JOIN skill_favorites f ON f.dedup_key = s.dedup_key""")]
        rows = [r for r in rows if r["dedup_key"] not in muted]
        if category:
            rows = [r for r in rows if r["category"] == category]
        qn = (q or "").lower().strip()
        if qn:
            def rel(r):
                name = (r["name"] or "").lower()
                desc = (r["description"] or "").lower()
                return (3 if qn in name else 0) + (1 if qn in desc else 0)
            rows = [r for r in rows if rel(r) > 0] or rows  # 无命中时退回全量排序
            rows.sort(key=lambda r: (not r["favorited"],
                                     -(r["votes_up"] - r["votes_down"]),
                                     -rel(r), -r["stars"]))
        else:
            rows.sort(key=lambda r: (not r["favorited"],
                                     -(r["votes_up"] - r["votes_down"]), -r["stars"]))
        return rows[:limit]


def favorite(dedup_key: str) -> dict:
    """收藏 / 取消收藏（幂等切换）。"""
    init_tables()
    with _conn() as conn:
        row = conn.execute("SELECT 1 FROM skills WHERE dedup_key = ?",
                           (dedup_key,)).fetchone()
        if not row:
            return {"ok": False, "error": "未知技能"}
        cur = conn.execute("SELECT 1 FROM skill_favorites WHERE dedup_key = ?",
                           (dedup_key,)).fetchone()
        if cur:
            conn.execute("DELETE FROM skill_favorites WHERE dedup_key = ?", (dedup_key,))
            _bump(conn, dedup_key, "favorites", -1)
            return {"ok": True, "favorited": False}
        conn.execute("INSERT INTO skill_favorites (dedup_key, favorited_at) VALUES (?, ?)",
                     (dedup_key, datetime.now().isoformat(timespec="seconds")))
        _bump(conn, dedup_key, "favorites", 1)
        return {"ok": True, "favorited": True}


def record_click(dedup_key: str) -> None:
    init_tables()
    with _conn() as conn:
        _bump(conn, dedup_key, "clicks")


def record_vote(dedup_key: str, vote: int) -> dict:
    init_tables()
    with _conn() as conn:
        if not conn.execute("SELECT 1 FROM skills WHERE dedup_key = ?",
                            (dedup_key,)).fetchone():
            return {"ok": False, "error": "未知技能"}
        _bump(conn, dedup_key, "votes_up" if vote > 0 else "votes_down")
    return {"ok": True}


def favorites() -> list:
    """收藏夹：按收藏时间倒序。"""
    init_tables()
    with _conn() as conn:
        rows = conn.execute(
            """SELECT s.*, f.favorited_at,
                      COALESCE(st.votes_up, 0) AS votes_up, COALESCE(st.votes_down, 0) AS votes_down
               FROM skill_favorites f
               JOIN skills s ON s.dedup_key = f.dedup_key
               LEFT JOIN skill_stats st ON st.dedup_key = s.dedup_key
               ORDER BY f.favorited_at DESC""").fetchall()
        return [dict(r) for r in rows]


def categories() -> list:
    """分类清单（含各分类条数，供前端筛选）。"""
    init_tables()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT category, COUNT(*) AS n FROM skills GROUP BY category ORDER BY n DESC"
        ).fetchall()
        return [dict(r) for r in rows]
