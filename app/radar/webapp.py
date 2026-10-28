#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 资讯雷达 Web 应用：日报阅读 + SoS 展示 + 👍/👎 反馈调教（FastAPI + SQLite）。

启动：uv run uvicorn webapp:app --host 127.0.0.1 --port 8080
"""

import json
import os
import random
import re
import secrets
import shutil
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from email.utils import formatdate
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from xml.sax.saxutils import escape

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from classic import pick_classics
from digeststyle import list_styles, set_style
from event_cluster import (
    apply_clusters,
    build_payload,
    init_table as init_cluster_table,
    load_cached as cluster_load_cached,
    save_cached as cluster_save_cached,
)
import explorer
from ghrepo import pick_ghrepos
from skill_brief import generate_brief, generate_detail
from ghrepo_brief import (
    GHREPOS_DIR,
    analyze_ghrepo,
    fetch_gh_readme,
    gh_cache_meta,
    gh_cache_path,
    gh_load_cached,
    gh_save_cached,
)
from hotlist import HOT_SOURCES, clear_cache as clear_hot_cache, load_custom_sources, pick_hot
import skills as skills_mod
from interest import (
    extract_arxiv_id,
    get_paper,
    init_tables as init_interest_tables,
    list_keywords,
    list_papers,
    remove_paper,
    search_arxiv_by_title,
    spawn_analysis,
    upsert_paper,
)
from paper_brief import (
    PAPERS_DIR,
    analyze_paper,
    cache_path,
    is_paper,
    load_cached,
    migrate_old_cache,
    save_cached,
)
from sos_daily import ENV_PATH, load_env

# 路径常量
RADAR_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = RADAR_DIR.parents[1]
SUMMARIES_DIR = PROJECT_ROOT / "app" / "Horizon" / "data" / "summaries"
SOS_DIR = PROJECT_ROOT / "out" / "sos"
DB_PATH = RADAR_DIR / "data" / "feedback.db"
STATIC_DIR = RADAR_DIR / "static"
HORIZON_CONFIG = PROJECT_ROOT / "app" / "Horizon" / "data" / "config.json"

# 访问口令（B8 双口令 → B26 多用户）：口令即身份，users 表存全部账号。
# ADMIN_KEY/GUEST_KEY 是 uid=1(admin)/guest 虚拟用户的 passcode（init_db 迁移落库），
# 好友账号由 admin 经 /api/users 创建（各自 passcode），旧 cookie 天然兼容。
ADMIN_KEY = os.environ.get("RADAR_ADMIN_KEY", "0000")
GUEST_KEY = os.environ.get("RADAR_GUEST_KEY", "")
SESSION_COOKIE = "radar_key"

# B26 口令→用户缓存（30s）：免每请求查库；停用用户最多 30s 后生效
_USER_CACHE: dict[str, tuple[float, dict | None]] = {}
_USER_CACHE_TTL = 30.0
_USER_CACHE_LOCK = threading.Lock()

# B9 登录防爆破：同一 IP 5 次失败锁 15 分钟
LOGIN_MAX_FAILS, LOGIN_LOCK_SECS = 5, 900
_LOGIN_FAILS: dict[str, list[float]] = {}
_LOGIN_LOCK = threading.Lock()

# 权重规则：👍 +0.5，👎 -1.0，clamp 到 [-3, +3]
CLAMP_MIN, CLAMP_MAX = -3.0, 3.0
UP_DELTA, DOWN_DELTA = 0.5, -1.0
# 关注词每命中一个 +8；屏蔽词命中直接 -1000 沉底
FOCUS_BONUS = 8.0
BLOCK_PENALTY = 1000.0
# 研究兴趣词每命中一个 +4（B18 调整：少量强化即可——过高会因一次双击就把口味精
# 化锁死、推荐同质化；探索优先，见 EXPLORE_SPREAD）
INTEREST_BONUS = 4.0
# B18：探索扰动幅度（ε-greedy 思路）——排序时每条未屏蔽条目加 0~4 分按
# 「日期+anchor」种子确定的随机扰动：同一天内顺序稳定，跨天自然轮换，
# 给中低权重条目露脸机会，防止高权重条目长期霸榜
EXPLORE_SPREAD = 4.0
# B20：探索配额——每板块 ≥4 条时保底 25%（至少 1 条）"无口味信号"条目强制曝光，
# 这是多臂老虎机 ε 保底：扰动（EXPLORE_SPREAD）只影响排序，配额才保证探索位存在
EXPLORE_QUOTA = 0.25

# 论文解读按需生成：进行中的 arxiv_id 集合 + 最近一次生成错误（arxiv_id -> 消息）
# B3：缓存与并发 key 均为 arxiv_id（全局唯一），同一篇论文跨日/跨板块只解读一次
PAPER_INFLIGHT: set[str] = set()
PAPER_ERRORS: dict[str, str] = {}
PAPER_LOCK = threading.Lock()
PAPER_SEM = threading.Semaphore(3)  # 预生成并发上限，防 LLM 限流

# B12 GitHub 项目解读：与论文解读同一套模式（key 为 full_name，跨日只解读一次）
GH_INFLIGHT: set[str] = set()
GH_ERRORS: dict[str, str] = {}
GH_LOCK = threading.Lock()

# B24 Skill AI 推荐词：同一套模式（key 为 dedup_key，跨日只生成一次），与论文/项目解读共用 PAPER_SEM
SKILL_BRIEF_INFLIGHT: set[str] = set()
SKILL_BRIEF_ERRORS: dict[str, str] = {}
SKILL_BRIEF_LOCK = threading.Lock()

# B25 Skill 深度解读：同上，key 为 dedup_key（点开 /skill 详情页才懒生成，永久缓存）
SKILL_DETAIL_INFLIGHT: set[str] = set()
SKILL_DETAIL_ERRORS: dict[str, str] = {}
SKILL_DETAIL_LOCK = threading.Lock()

# B13 事件聚类：后台构建防重入（day 集合，构建完从集合移除）
CLUSTER_INFLIGHT: set[str] = set()
CLUSTER_LOCK = threading.Lock()

# 启动时把旧的 out/papers/{date}/{anchor}.md 迁移到 arxiv_id 全局缓存（幂等）
migrate_old_cache()

app = FastAPI(title="AI 资讯雷达")


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    """口令门禁（B8 → B26 多用户）：users 表任一 active 账号的 cookie 放行，其余跳 /login。"""
    if request.url.path == "/login":
        return await call_next(request)
    # RSS 输出：阅读器无法保持 cookie，允许 ?key= 口令（任一 active 用户）直连
    rss_key = request.query_params.get("key", "")
    if request.url.path == "/rss" and rss_key and user_by_passcode(rss_key):
        return await call_next(request)
    # B15 探索频道中转 feed：本机 Horizon 每日抓取（radar-web 仅绑 127.0.0.1，
    # 输出只有公开资讯条目，无需口令）
    if request.url.path == "/explorer/feed.xml":
        return await call_next(request)
    if current_user(request):
        return await call_next(request)
    return RedirectResponse("/login", status_code=307)


def user_by_passcode(k: str) -> dict | None:
    """B26：口令 → active 用户（30s 内存缓存，避免每请求查库）。"""
    if not k:
        return None
    now = time.time()
    with _USER_CACHE_LOCK:
        hit = _USER_CACHE.get(k)
        if hit and now - hit[0] < _USER_CACHE_TTL:
            return hit[1]
    u: dict | None = None
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT id, name, email, passcode, role, active FROM users"
                " WHERE passcode = ? AND active = 1",
                (k,),
            ).fetchone()
            if row:
                u = dict(row)
    except sqlite3.OperationalError:
        u = None  # 库不可用时不放行（防爆破兜底）
    with _USER_CACHE_LOCK:
        _USER_CACHE[k] = (now, u)
    return u


def current_user(request: Request) -> dict | None:
    """B26：当前会话用户（cookie 口令查 users 表）。"""
    return user_by_passcode(request.cookies.get(SESSION_COOKIE, ""))


def current_uid(request: Request) -> int:
    """B26：当前登录用户 id（auth_gate 保证已登录；兜底 1=admin）。"""
    u = current_user(request)
    return int(u["id"]) if u else 1


def is_admin(request: Request) -> bool:
    """B8 → B26：管理员判定（当前用户 role=admin）。"""
    u = current_user(request)
    return bool(u) and u["role"] == "admin"


def guest_denied(request: Request):
    """B8：写接口校验；只读访客（role=guest）返回 403，admin/member 放行。

    member 的个人数据写权限由各接口以 current_user 归属实现（P1）；
    全局配置类接口仍应叠加 is_admin 校验。
    """
    u = current_user(request)
    if u and u["role"] == "guest":
        return JSONResponse(
            {"ok": False, "error": "访客只读：无权修改推荐"}, status_code=403
        )
    return None


def _login_locked(ip: str) -> bool:
    now = time.time()
    with _LOGIN_LOCK:
        fails = [t for t in _LOGIN_FAILS.get(ip, []) if now - t < LOGIN_LOCK_SECS]
        _LOGIN_FAILS[ip] = fails
        return len(fails) >= LOGIN_MAX_FAILS


def _login_record_fail(ip: str) -> None:
    with _LOGIN_LOCK:
        _LOGIN_FAILS.setdefault(ip, []).append(time.time())


def _login_clear(ip: str) -> None:
    with _LOGIN_LOCK:
        _LOGIN_FAILS.pop(ip, None)


def get_conn() -> sqlite3.Connection:
    """新建 SQLite 连接（每操作一连接，避免跨线程复用问题）。"""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _cols(conn: sqlite3.Connection, table: str) -> set:
    """表的全部列名（表不存在返回空集）。"""
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _rebuild_for_uid(conn: sqlite3.Connection, table: str, ddl: str, copy_cols: str) -> None:
    """B26：老表缺 user_id 列时重建并迁移（旧数据全部归属 uid=1 admin）。

    SQLite 无法原地改 PRIMARY KEY/UNIQUE 约束，只能 rename→建新表→拷贝→drop。
    """
    cols = _cols(conn, table)
    if not cols or "user_id" in cols:
        return
    conn.execute(f"ALTER TABLE {table} RENAME TO {table}__uid_old")
    conn.execute(ddl)
    conn.execute(
        f"INSERT INTO {table} (user_id, {copy_cols}) "
        f"SELECT 1, {copy_cols} FROM {table}__uid_old"
    )
    conn.execute(f"DROP TABLE {table}__uid_old")


# B26 挂 user_id 的表 DDL（建新库与老库迁移共用同一份）
USER_KEYWORDS_DDL = """CREATE TABLE IF NOT EXISTS user_keywords (
    user_id INTEGER NOT NULL DEFAULT 1,
    keyword TEXT NOT NULL,
    kind TEXT NOT NULL,
    created_at TEXT,
    UNIQUE (user_id, keyword)
)"""
PUSHED_DDL = """CREATE TABLE IF NOT EXISTS pushed (
    user_id INTEGER NOT NULL DEFAULT 1,
    day TEXT NOT NULL,
    anchor TEXT NOT NULL,
    url TEXT,
    title TEXT,
    source TEXT,
    kind TEXT,
    arxiv_id TEXT,
    url_norm TEXT,
    PRIMARY KEY (user_id, day, anchor)
)"""
CLICKS_DDL = """CREATE TABLE IF NOT EXISTS clicks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL DEFAULT 1,
    day TEXT,
    anchor TEXT,
    url TEXT,
    title TEXT,
    source TEXT,
    kind TEXT,
    role TEXT,
    url_norm TEXT,
    arxiv_id TEXT,
    clicked_at TEXT,
    UNIQUE (day, anchor, user_id, kind)
)"""


def init_db() -> None:
    """启动时建目录 + 建表。"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS votes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT,
                item_title TEXT,
                item_url TEXT,
                source TEXT,
                vote INTEGER,
                day TEXT,
                user_id INTEGER NOT NULL DEFAULT 1
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS source_weights (
                source TEXT PRIMARY KEY,
                weight REAL NOT NULL DEFAULT 0,
                updated_at TEXT
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS keyword_weights (
                keyword TEXT PRIMARY KEY,
                weight REAL NOT NULL DEFAULT 0,
                updated_at TEXT
            )"""
        )
        conn.execute(USER_KEYWORDS_DDL)
        # B26 多用户：口令即身份。admin 建号发口令，各账号偏好/行为互相隔离（P1 起挂 user_id）
        conn.execute(
            """CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT DEFAULT '',
                passcode TEXT NOT NULL UNIQUE,
                role TEXT NOT NULL DEFAULT 'member',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT
            )"""
        )
        # 迁移：users 空表时把 ADMIN_KEY/GUEST_KEY 落成内置账号（幂等）。
        # uid=1 永远是 admin（旧数据全部归属它）；GUEST_KEY 非空则注册 guest 虚拟用户。
        if conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] == 0:
            conn.execute(
                "INSERT INTO users (name, email, passcode, role, active, created_at) VALUES (?,?,?,?,1,?)",
                ("admin", "", ADMIN_KEY, "admin", datetime.now().isoformat(timespec="seconds")),
            )
            if GUEST_KEY:
                conn.execute(
                    "INSERT INTO users (name, email, passcode, role, active, created_at) VALUES (?,?,?,?,1,?)",
                    ("guest", "", GUEST_KEY, "guest", datetime.now().isoformat(timespec="seconds")),
                )
        # B2 点击追踪 + 跨日去重：pushed 记"每人每天看到过什么"（per-user 重推徽章
        # 与 Horizon 判重），clicks 记"谁点过什么"（per-user 埋点；role 列暂留兼容 seen.py）
        conn.execute(PUSHED_DDL)
        conn.execute(CLICKS_DDL)
        # B1 历史归档索引：每条历史条目一行，供 /api/search 检索
        # （本机 sqlite 未编译 FTS5，用 LIKE 子串匹配兜底，万条级毫秒足够）
        conn.execute(
            """CREATE TABLE IF NOT EXISTS archive_index (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day TEXT NOT NULL,
                type TEXT NOT NULL,
                title TEXT NOT NULL,
                url TEXT DEFAULT '',
                arxiv_id TEXT DEFAULT '',
                summary TEXT DEFAULT '',
                tags TEXT DEFAULT '',
                source TEXT DEFAULT '',
                created_at TEXT,
                UNIQUE (day, type, url)
            )"""
        )
        # B11 每日推送配额（全局配置，admin 管）：key-value 设置
        #（cluster_enabled / score_cache_ttl_days 等；quota_* 已迁到 user_settings per-user）
        conn.execute(
            """CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT
            )"""
        )
        # B26 个人设置：每人自己的每日推送配额（偏好面板可调）
        conn.execute(
            """CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                updated_at TEXT,
                PRIMARY KEY (user_id, key)
            )"""
        )
        # 迁移：老的单人配额（app_settings quota_*）落给 uid=1 admin（幂等）
        if conn.execute("SELECT COUNT(*) AS n FROM user_settings").fetchone()["n"] == 0:
            conn.execute(
                """INSERT OR IGNORE INTO user_settings (user_id, key, value, updated_at)
                   SELECT 1, key, value, updated_at FROM app_settings
                   WHERE key LIKE 'quota_%'"""
            )
        # B23 用户自定义热榜源（⚙ 设置面板可添加/删除；屏蔽走 votes 差评抑制）
        conn.execute(
            """CREATE TABLE IF NOT EXISTS hot_custom_sources (
                name TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                created_at TEXT
            )"""
        )
        # B26 多用户迁移：老库单用户数据全部归属 uid=1（admin），幂等。
        # source_weights/keyword_weights 保持全局（admin 管理/无写入点）；
        # votes 无约束变更直接补列，其余三表重建约束。
        if _cols(conn, "votes") and "user_id" not in _cols(conn, "votes"):
            conn.execute("ALTER TABLE votes ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1")
        _rebuild_for_uid(conn, "user_keywords", USER_KEYWORDS_DDL,
                         "keyword, kind, created_at")
        _rebuild_for_uid(conn, "pushed", PUSHED_DDL,
                         "day, anchor, url, title, source, kind, arxiv_id, url_norm")
        _rebuild_for_uid(conn, "clicks", CLICKS_DDL,
                         "day, anchor, url, title, source, kind, role, url_norm, "
                         "arxiv_id, clicked_at")
    init_interest_tables()
    init_cluster_table()
    # B15 探索频道：建表 + 同步源池
    explorer.init_tables()
    explorer.ensure_pool()
    # B23 Skills 推荐板块：建表（采集由 radar-skills-sync.timer 每日执行）
    skills_mod.init_tables()


# B11 每日推送数量配额（⚙ 偏好面板可调；B26 起存 user_settings per-user）
# papers=0 表示不限制；区间：ghrepo/classic 1~10，papers 0~30
QUOTA_DEFAULTS = {"ghrepo": 1, "classic": 1, "papers": 0}
QUOTA_LIMITS = {"ghrepo": (1, 10), "classic": (1, 10), "papers": (0, 30)}


def get_quotas(uid: int) -> dict:
    """读取某用户的三类每日推送配额（缺省/非法值回退默认，并钳制到合法区间）。"""
    quotas = dict(QUOTA_DEFAULTS)
    try:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT key, value FROM user_settings WHERE user_id = ?", (uid,)
            ).fetchall()
    except sqlite3.Error:
        return quotas
    for r in rows:
        name = (r["key"] or "").removeprefix("quota_")
        if name in quotas:
            try:
                quotas[name] = int(r["value"])
            except (TypeError, ValueError):
                pass
    for name, (lo, hi) in QUOTA_LIMITS.items():
        quotas[name] = max(lo, min(hi, quotas[name]))
    return quotas


def set_quotas(uid: int, values: dict) -> dict:
    """写入某用户配额（钳制到合法区间），返回生效后的全部配额。"""
    now = datetime.now().isoformat(timespec="seconds")
    with get_conn() as conn:
        for name, val in values.items():
            lo, hi = QUOTA_LIMITS[name]
            val = max(lo, min(hi, int(val)))
            conn.execute(
                """INSERT INTO user_settings (user_id, key, value, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value,
                                                          updated_at = excluded.updated_at""",
                (uid, f"quota_{name}", str(val), now),
            )
    return get_quotas(uid)


def get_cluster_enabled() -> bool:
    """读取 B13 事件聚类开关（app_settings 表；未配置默认开）。"""
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT value FROM app_settings WHERE key = 'cluster_enabled'"
            ).fetchone()
    except sqlite3.Error:
        return True
    if row is None:
        return True
    return (row["value"] or "1").strip().lower() not in ("0", "false", "off")


def set_cluster_enabled(enabled: bool) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO app_settings (key, value, updated_at)
               VALUES ('cluster_enabled', ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                               updated_at = excluded.updated_at""",
            ("1" if enabled else "0", now),
        )


def load_weights() -> tuple:
    """读取 source_weights 与 keyword_weights，返回 (dict, dict)。

    B26：两张表都是全局的（admin 管理；keyword_weights 无写入点仅占位），
    个性化排序靠 per-user 的 user_keywords / interest_keywords 叠加。
    """
    with get_conn() as conn:
        src_rows = conn.execute("SELECT source, weight FROM source_weights").fetchall()
        kw_rows = conn.execute("SELECT keyword, weight FROM keyword_weights").fetchall()
    src_weights = {r["source"]: float(r["weight"]) for r in src_rows}
    kw_weights = {r["keyword"]: float(r["weight"]) for r in kw_rows}
    return src_weights, kw_weights


def read_source_weight(source: str) -> float:
    """读取当前来源权重（member 投票时只展示不动权重）。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT weight FROM source_weights WHERE source = ?", (source,)
        ).fetchone()
    return float(row["weight"]) if row else 0.0


def update_source_weight(source: str, delta: float) -> float:
    """按规则更新来源权重并 upsert，返回新权重。"""
    now = datetime.now().isoformat(timespec="seconds")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT weight FROM source_weights WHERE source = ?", (source,)
        ).fetchone()
        old = float(row["weight"]) if row else 0.0
        new = max(CLAMP_MIN, min(CLAMP_MAX, old + delta))
        conn.execute(
            """INSERT INTO source_weights (source, weight, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(source) DO UPDATE SET weight = excluded.weight,
                                                updated_at = excluded.updated_at""",
            (source, new, now),
        )
    return new


# ---------------- 日报 Markdown 解析 ----------------

ITEM_RE = re.compile(
    r"^###\s+\[([^\]]+)\]\(([^)\s]+)\)\s*(?:⭐️?\s*([\d.]+)\s*/\s*\d+)?.*$"
)
TAG_RE = re.compile(r"#([^\s`,，、]+)")


def parse_report(md_text: str) -> list:
    """健壮解析日报 markdown，返回 [{name, items}]。

    约定结构：`## 板块` -> 可选 `<a id=...></a>` 锚点 -> `### [标题](url) ⭐️ 分`
    -> 摘要段 -> 来源行（` · ` 分隔，首段为 source）-> 可选 **标签** 行 -> `---`。
    """
    sections = []
    current = None
    pending_anchor = None
    lines = md_text.splitlines()
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()

        # 条目锚点行（如 <a id="item-papers-1"></a>）：挂到紧随其后的条目上
        m_anchor = re.match(r'<a id="([^"]+)"></a>$', stripped)
        if m_anchor:
            pending_anchor = m_anchor.group(1)
            i += 1
            continue

        # 二级标题：新板块（"### " 不会被 "## " 前缀误匹配）
        if stripped.startswith("## ") and not stripped.startswith("###"):
            current = {"name": stripped[3:].strip(), "items": []}
            sections.append(current)
            i += 1
            continue

        m = ITEM_RE.match(stripped)
        if m and current is not None:
            title = m.group(1).strip()
            url = m.group(2).strip()
            score = float(m.group(3)) if m.group(3) else None

            summary = ""
            source = ""
            tags = []
            # 条目块：直到下一个 ### / ## / --- 分隔线
            j = i + 1
            while j < n:
                inner = lines[j].strip()
                if inner.startswith("### ") or inner.startswith("## ") or inner == "---":
                    break
                if not inner or inner.startswith("<a id="):
                    j += 1
                    continue
                if inner.startswith("**标签**"):
                    tags = TAG_RE.findall(inner)
                    j += 1
                    continue
                if " · " in inner:
                    # 来源行特征：用 · 分隔的短行；若摘要未填且该行很长，则视为摘要
                    if not summary and len(inner) > 60:
                        summary = inner
                    elif not source:
                        # 来源行格式：source_type · (feed_name | author) · 日期。
                        # 仅 RSS 条目取第二段 feed_name 做归组/统计——此前只取
                        # 第一段导致 clicks/votes.source 全存成 "rss"（B15 修复）；
                        # 探索条目的 feed_name 来自中转 <source> 的真实源名。
                        # 非 RSS（hackernews/google_news/reddit…）第二段是
                        # 作者/版名，维持第一段 source_type 归组
                        parts = [p.strip() for p in inner.split(" · ") if p.strip()]
                        if parts and parts[0] == "rss" and len(parts) >= 2:
                            source = parts[1]
                        else:
                            source = parts[0] if parts else ""
                    j += 1
                    continue
                if not summary:
                    # 来源行之前的第一个非空段落即摘要
                    summary = inner
                j += 1

            current["items"].append(
                {
                    "title": title,
                    "url": url,
                    "score": score,
                    "summary": summary,
                    "source": source,
                    "tags": tags,
                    "anchor": pending_anchor,
                }
            )
            pending_anchor = None
            i = j
            continue

        i += 1
    return sections


def fallback_sections(md_text: str) -> list:
    """解析出 0 条时的兜底：单条目 = 首行标题 + 全文摘要。"""
    first_line = md_text.splitlines()[0].strip() if md_text else "Horizon 日报"
    return [
        {
            "name": "全部",
            "items": [
                {
                    "title": first_line,
                    "url": "",
                    "score": None,
                    "summary": md_text.strip(),
                    "source": "",
                    "tags": [],
                }
            ],
        }
    ]


def resolve_report(requested: str) -> tuple:
    """定位日报文件：指定日期不存在则回退到 summaries 中文件名最新的那份。

    返回 (使用的日期字符串, 日报文件 Path 或 None)。
    """
    exact = SUMMARIES_DIR / f"horizon-{requested}-zh.md"
    if exact.exists():
        return requested, exact
    candidates = sorted(SUMMARIES_DIR.glob("horizon-*-zh.md")) if SUMMARIES_DIR.exists() else []
    if not candidates:
        return requested, None
    latest = candidates[-1]
    m = re.match(r"horizon-(\d{4}-\d{2}-\d{2})-zh\.md$", latest.name)
    used_date = m.group(1) if m else requested
    return used_date, latest


# ---------------- B2：点击追踪 + 跨日去重 ----------------


def url_norm(url: str) -> str:
    """URL 归一化：scheme/host 小写 + path 去尾斜杠，丢弃 query/fragment。

    用于跨日去重时识别"同一篇新闻"（跟踪参数、锚点差异不计）。
    Horizon 侧 seen.py 有等价实现，两边保持一致。
    """
    url = (url or "").strip()
    try:
        parts = urlsplit(url)
        if not parts.netloc:
            return url
        return urlunsplit(
            (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", "")
        )
    except Exception:
        return url


def record_pushed(uid: int, day: str, sections: list) -> None:
    """B2：feed 被浏览时落推送记录（B26 per-user，INSERT OR IGNORE 幂等，同日刷新不重复计）。

    Horizon 入 digest 前查 clicks 判"看过没有"（B26 起全员点过才全局剔除）；
    pushed 仅用于 🔄 重推徽章与归档索引。
    """
    rows = []
    for sec in sections:
        for it in sec["items"]:
            url = it.get("url") or ""
            rows.append(
                (
                    uid,
                    day,
                    it.get("anchor") or "",
                    url,
                    it.get("title") or "",
                    it.get("source") or "",
                    "paper" if it.get("is_paper") else "link",
                    extract_arxiv_id(url) or "",
                    url_norm(url),
                )
            )
    if not rows:
        return
    with get_conn() as conn:
        conn.executemany(
            """INSERT OR IGNORE INTO pushed
               (user_id, day, anchor, url, title, source, kind, arxiv_id, url_norm)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )


def annotate_repush(uid: int, day: str, sections: list) -> None:
    """B2：给"以前推送过"的条目注入 🔄 重推徽章信息（B26 按当前用户自己的历史）。

    repush=True 且 last_pushed_day=最近一次推送日期（不含今天）。
    B22：已点击的也照常标注（条目不再被移除，看完还在）。
    """
    with get_conn() as conn:
        push_rows = conn.execute(
            "SELECT day, url_norm, arxiv_id FROM pushed WHERE user_id = ? AND day < ?",
            (uid, day),
        ).fetchall()
    last_by_norm, last_by_aid = {}, {}
    for r in push_rows:
        if r["url_norm"]:
            last_by_norm[r["url_norm"]] = max(last_by_norm.get(r["url_norm"], ""), r["day"])
        if r["arxiv_id"]:
            last_by_aid[r["arxiv_id"]] = max(last_by_aid.get(r["arxiv_id"], ""), r["day"])
    for sec in sections:
        for it in sec["items"]:
            url = it.get("url") or ""
            aid = extract_arxiv_id(url) or ""
            norm = url_norm(url)
            last = ""
            if aid and aid in last_by_aid:
                last = last_by_aid[aid]
            elif norm and norm in last_by_norm:
                last = last_by_norm[norm]
            if not last:
                continue
            it["repush"] = True
            it["last_pushed_day"] = last


# ---------------- B22：热榜来源投票（👍/👎 按源差评抑制） ----------------

# 热榜来源全集：aihot/36kr（RSS 源名）+ GitHub（项目置顶条目）。
# 与资讯源名分属不同命名空间，投票互不干扰。
HOT_VOTE_SOURCES = [name for name, _, _ in HOT_SOURCES] + ["GitHub"]


def _hot_vote_sources() -> list:
    """B23：内置源 + GitHub + 用户自定义源（hot_custom_sources 表）。"""
    return HOT_VOTE_SOURCES + [name for name, _, _ in load_custom_sources()]


def _hot_custom_names() -> set:
    return {name for name, _, _ in load_custom_sources()}


def _hot_vote_stats() -> dict:
    """聚合 votes 表按源统计 👍/👎，返回 {source: {up, down, muted}}。

    差评抑制规则：某源 downs >= 3 且 downs >= 2*ups → muted=True，
    后续不再进热榜（api_feed 过滤）。votes 表无 role 列，
    但 /api/feedback 有 guest 校验，全表即 admin 投票。
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT source, SUM(CASE WHEN vote = 1 THEN 1 ELSE 0 END) AS ups, "
            "SUM(CASE WHEN vote = -1 THEN 1 ELSE 0 END) AS downs "
            "FROM votes GROUP BY source"
        ).fetchall()
    stats = {}
    for r in rows:
        ups, downs = r["ups"] or 0, r["downs"] or 0
        stats[r["source"]] = {
            "up": ups,
            "down": downs,
            "muted": downs >= 3 and downs >= 2 * ups,
        }
    return stats


# ---------------- B1：历史归档索引 + 搜索 ----------------


def _upsert_archive(conn, day: str, type_: str, title: str, url: str,
                    summary: str, tags: str, source: str) -> None:
    """归档索引幂等写入（同日同类型同 URL 只入一次）。"""
    conn.execute(
        """INSERT OR IGNORE INTO archive_index
           (day, type, title, url, arxiv_id, summary, tags, source, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            day, type_, title, url or "", extract_arxiv_id(url or "") or "",
            (summary or "")[:300], tags or "", source or "",
            datetime.now().isoformat(timespec="seconds"),
        ),
    )


def archive_from_feed(day: str, sections: list, sos_text: str | None) -> None:
    """B1：feed 浏览时把当天条目 + SoS 增量入归档索引（幂等）。"""
    with get_conn() as conn:
        for sec in sections:
            for it in sec["items"]:
                _upsert_archive(
                    conn, day,
                    "paper" if it.get("is_paper") else "news",
                    it.get("title") or "", it.get("url") or "",
                    it.get("summary") or "",
                    ",".join(it.get("tags") or []),
                    it.get("source") or "",
                )
        if sos_text:
            body = re.sub(r"[#*`>\-]+", "", sos_text).strip()[:300]
            _upsert_archive(conn, day, "sos", f"SoS 综合速览 {day}", "", body, "", "")


def rebuild_archive() -> int:
    """B1：一次性回填历史归档（summaries 全部日报 + out/sos 全部 SoS）。

    INSERT OR IGNORE 幂等，重复执行无副作用；启动时调用。
    返回本次新增行数。
    """
    with get_conn() as conn:
        before = conn.execute("SELECT COUNT(*) FROM archive_index").fetchone()[0]
        reports = sorted(SUMMARIES_DIR.glob("horizon-*-zh.md")) if SUMMARIES_DIR.exists() else []
        for md in reports:
            m = re.match(r"horizon-(\d{4}-\d{2}-\d{2})-zh\.md$", md.name)
            if not m:
                continue
            day = m.group(1)
            for sec in parse_report(md.read_text(encoding="utf-8")):
                for it in sec["items"]:
                    _upsert_archive(
                        conn, day,
                        "paper" if is_paper(it.get("url") or "") else "news",
                        it.get("title") or "", it.get("url") or "",
                        it.get("summary") or "",
                        ",".join(it.get("tags") or []),
                        it.get("source") or "",
                    )
        sose_files = sorted(SOS_DIR.glob("sos-*.md")) if SOS_DIR.exists() else []
        for sos in sose_files:
            m = re.match(r"sos-(\d{4}-\d{2}-\d{2})\.md$", sos.name)
            if not m:
                continue
            day = m.group(1)
            body = re.sub(r"[#*`>\-]+", "", sos.read_text(encoding="utf-8")).strip()[:300]
            _upsert_archive(conn, day, "sos", f"SoS 综合速览 {day}", "", body, "", "")
        return conn.execute("SELECT COUNT(*) FROM archive_index").fetchone()[0] - before


def _like_kw(kw: str) -> str:
    """LIKE 通配符转义（% _ \\），包成 %...%。"""
    kw = kw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{kw}%"


def search_archive(q: str, limit: int = 50) -> list:
    """B1：归档搜索——标题/摘要/标签 LIKE 子串匹配，多关键词空格分隔 = AND。

    中文无需分词（子串即命中）；按日期倒序。
    """
    kws = [k for k in (q or "").split() if k][:5]
    if not kws:
        return []
    conds, args = [], []
    for _ in kws:
        conds.append(
            "(title LIKE ? ESCAPE '\\' OR summary LIKE ? ESCAPE '\\' OR tags LIKE ? ESCAPE '\\')"
        )
    for kw in kws:
        args += [_like_kw(kw)] * 3
    sql = (
        "SELECT day, type, title, url, arxiv_id, summary, tags, source "
        f"FROM archive_index WHERE {' AND '.join(conds)} "
        "ORDER BY day DESC, id DESC LIMIT ?"
    )
    args.append(limit)
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, args)]


# ---------------- API ----------------


# ---------------- B13 事件聚类（后台构建 + 注解） ----------------


def _start_cluster_build(used_date: str, report_file: Path, md_mtime: float) -> None:
    """后台线程构建当天事件聚类（规则预聚 + LLM 复核 → 存表），防重入。"""
    with CLUSTER_LOCK:
        if used_date in CLUSTER_INFLIGHT:
            return
        CLUSTER_INFLIGHT.add(used_date)

    def _worker() -> None:
        try:
            md = report_file.read_text(encoding="utf-8")
            secs = parse_report(md)
            if not secs:
                secs = fallback_sections(md)
            entries = [
                {
                    "section": s["name"],
                    "items": [
                        {
                            "anchor": it.get("anchor"),
                            "title": it.get("title") or "",
                            "source": it.get("source") or "",
                            "url": it.get("url") or "",
                        }
                        for it in s["items"]
                    ],
                }
                for s in secs
            ]
            payload = build_payload(entries)
            if payload is not None:
                cluster_save_cached(used_date, md_mtime, payload)
        except Exception:
            pass
        finally:
            with CLUSTER_LOCK:
                CLUSTER_INFLIGHT.discard(used_date)

    threading.Thread(target=_worker, daemon=True).start()


@app.get("/api/feed")
def api_feed(request: Request, date_param: str | None = None):
    """返回解析后的日报条目 + SoS + 权重排序结果（纯本地，不发外部请求）。

    B26：排序/屏蔽/兴趣/配额/重推徽章全部按当前用户视角（per-user）。
    """
    # B21：每日一次的缓存维护（按 score_cache_ttl_days 轮转打分缓存），幂等
    try:
        run_cache_maintenance()
    except Exception:
        pass
    requested = date_param or date.today().isoformat()
    # 校验日期格式
    try:
        date.fromisoformat(requested)
    except ValueError:
        return {"error": f"日期格式不合法：{requested}"}

    used_date, report_file = resolve_report(requested)
    if report_file is None:
        return {"error": "summaries 目录中没有任何日报文件"}

    md_text = report_file.read_text(encoding="utf-8")
    sections = parse_report(md_text)
    if not sections:
        sections = fallback_sections(md_text)

    # 同日 SoS（用回退后的日期）
    sos_path = SOS_DIR / f"sos-{used_date}.md"
    sos = sos_path.read_text(encoding="utf-8") if sos_path.exists() else None

    # 权重排序：source_weight*10 + 命中 keyword 权重和 + 关注词*8 + 兴趣词*4
    # − 屏蔽词沉底 + 每日探索扰动（B18：0~4 分，跨天轮换）
    # B26：关注词/屏蔽词/兴趣词读当前用户自己的
    uid = current_uid(request)
    src_weights, kw_weights = load_weights()
    focus_kws, block_kws = load_user_keywords(uid)
    interest_kws = list_keywords(uid)
    for sec in sections:
        for item in sec["items"]:
            sw = src_weights.get(item.get("source") or "", 0.0)
            text = (item["title"] + "\n" + (item.get("summary") or "")).lower()
            kw_sum = sum(w for kw, w in kw_weights.items() if kw.lower() in text)
            hits = [kw for kw in focus_kws if kw.lower() in text]
            blocked = any(kw.lower() in text for kw in block_kws)
            interest_hits = [k["keyword"] for k in interest_kws if k["keyword"] in text]
            item["focus_hits"] = hits
            item["blocked"] = blocked
            item["interest_hits"] = interest_hits
            weight = (
                sw * 10 + kw_sum + FOCUS_BONUS * len(hits)
                + INTEREST_BONUS * len(interest_hits)
            )
            if blocked:
                weight -= BLOCK_PENALTY
            seed = f"{used_date}:{item.get('anchor') or item.get('url') or item['title']}"
            weight += random.Random(seed).uniform(0, EXPLORE_SPREAD)
            item["weight"] = round(weight, 4)
        sec["items"].sort(key=lambda it: -it["weight"])

    # B11 论文板块配额：每板块 arXiv 论文条目最多 quota_papers 篇（0 = 不限制）；
    # 已按权重降序，从前往后保留前 n 篇，超额论文从尾部剔除（非论文与顺序不动，
    # 被裁条目也不进热榜剔除文本、不预生成解读）
    quotas = get_quotas(uid)
    qp = quotas["papers"]
    if qp > 0:
        for sec in sections:
            kept, paper_count = [], 0
            for it in sec["items"]:
                if is_paper(it.get("url") or ""):
                    paper_count += 1
                    if paper_count > qp:
                        continue
                kept.append(it)
            sec["items"] = kept

    # B20 探索配额：每板块 ≥4 条时，保底 25%（至少 1 条）"无口味信号"条目强制
    # 曝光——pool 只收未屏蔽、无关注词/兴趣词命中的非经典条目，按（日报质量分
    # 降序、权重降序）挑 k 条标 🎲 探索位，等距穿插回主列表。探索位不带任何
    # 口味加权（这正是探索的意义），质量分只是保证"探索的也是当天像样的条目"。
    for sec in sections:
        items = sec["items"]
        n = len(items)
        if n < 4:
            continue
        k = max(1, round(n * EXPLORE_QUOTA))
        pool = [
            it for it in items
            if not it.get("blocked")
            and not it.get("focus_hits")
            and not it.get("interest_hits")
            and not it.get("classic")
        ]
        if not pool:
            continue
        pool.sort(key=lambda it: (-(it.get("score") or 0), -it.get("weight", 0)))
        picks = pool[:k]
        pick_ids = {id(it) for it in picks}
        main = [it for it in items if id(it) not in pick_ids]
        for it in picks:
            it["explore_pick"] = True
        # 从后往前等距穿插，靠后的插入不影响靠前的目标位置
        for j in range(len(picks) - 1, -1, -1):
            pos = (j + 1) * len(main) // (len(picks) + 1)
            main.insert(pos, picks[j])
        sec["items"] = main

    # 🔥 热榜精选：aihot + 36kr，剔除日报已收录，取 1~3 条（拉取失败不影响主流程）
    hot = []
    try:
        digest_text = "\n".join(it["title"] for sec in sections for it in sec["items"])
        hot = pick_hot(digest_text, total=3)
    except Exception:
        hot = []
    # B22：按来源差评抑制——被持续 👎 的热榜源不再展示
    try:
        mute_stats = _hot_vote_stats()
        hot = [it for it in hot if not mute_stats.get(it["source"], {}).get("muted")]
    except sqlite3.Error:
        pass

    # 🧩 今日 Skills（B23）：5 条推荐（冻结批次；库未采集时返回 []，不影响主流程）
    try:
        skills_today = skills_mod.today_picks()
    except Exception as exc:
        print(f"[feed] skills 板块失败：{type(exc).__name__}: {exc}")
        skills_today = []
    # B24 预生成：今日 5 条的 AI 推荐词后台排队（已缓存的跳过，点开时多半已就绪）
    for sk in skills_today:
        try:
            _start_skill_brief(sk["dedup_key"], sk)
        except Exception as exc:
            print(f"[feed] skills 推荐词排队失败：{type(exc).__name__}: {exc}")

    # 论文条目标记：arXiv 链接 + 锚点齐备才启用"📄 解读"详情页入口；
    # B3 预生成：未缓存的后台排队生成（推送即保存，点开时多半已就绪）
    for sec in sections:
        for it in sec["items"]:
            if is_paper(it.get("url") or "") and it.get("anchor"):
                aid = extract_arxiv_id(it.get("url") or "") or ""
                it["is_paper"] = True
                it["paper_ready"] = bool(aid) and cache_path(aid).exists()
                it["paper_url"] = f"/paper?date={used_date}&anchor={it['anchor']}"
                if aid and not it["paper_ready"]:
                    _start_paper_generation(used_date, it["anchor"], aid, it)
            else:
                it["is_paper"] = False

    # B4：经典论文回看（配额 n 篇）——附在论文板块末尾（失败/无候选不影响主流程）
    try:
        classics = pick_classics(used_date, quotas["classic"])
    except Exception:
        classics = []
    if classics:
        target = next(
            (s for s in sections if any(it.get("is_paper") for it in s["items"])), None
        )
        for cl in classics:
            aid = cl["arxiv_id"]
            anchor = "classic-" + aid.replace(".", "-", 1)
            citem = {
                "anchor": anchor,
                "title": cl["title"],
                "url": cl["url"],
                "summary": (cl["abstract"] or "")[:300],
                "source": "经典回看",
                "tags": [],
                "score": None,
                "weight": 0,
                "focus_hits": [],
                "blocked": False,
                "interest_hits": [],
                "is_paper": True,
                "classic": True,
                "classic_reason": cl["reason"],
                "paper_ready": bool(cache_path(aid).exists()),
                "paper_url": f"/paper?date={used_date}&anchor={anchor}",
            }
            if target is not None:
                target["items"].append(citem)  # 排序已完成，固定落在板块末尾
                if not citem["paper_ready"]:
                    _start_paper_generation(used_date, anchor, aid, citem)

    # B5：每日 GitHub 热门项目（配额 n 个）——置顶热榜精选；入 B2 推送记录 / B1 归档索引
    # （失败不影响主流程）。anchor 用 full_name 派生（同日多个项目锚点各自唯一）
    try:
        ghs = pick_ghrepos(used_date, quotas["ghrepo"])
    except Exception:
        ghs = []
    gh_items = []
    for gh in ghs:
        anchor = _gh_anchor(gh["full_name"])
        gitem = {
            "anchor": anchor,
            "title": gh["full_name"],
            "url": gh["url"],
            "summary": (gh["description"] or "")[:300],
            "source": "GitHub 今日项目",
            "tags": [gh["language"]] if gh.get("language") else [],
            "score": None,
            "weight": 0,
            "focus_hits": [],
            "blocked": False,
            "interest_hits": [],
            "is_paper": False,
            "gh": True,
            "gh_reason": gh["reason"],
            # B12 项目解读入口：未生成则后台预生成（推送即保存，点开时多半已就绪）
            "gh_ready": gh_load_cached(gh["full_name"]) is not None,
            "gh_brief_url": f"/ghrepo?date={used_date}&anchor={anchor}",
        }
        if not gitem["gh_ready"]:
            _start_gh_generation(used_date, anchor, gh["full_name"], gh)
        gh_items.append((gitem, gh))
    if gh_items:
        gh_secs = [{"name": "今日项目", "items": [gi for gi, _ in gh_items]}]
        record_pushed(uid, used_date, gh_secs)
        annotate_repush(uid, used_date, gh_secs)
        try:
            archive_from_feed(used_date, gh_secs, None)
        except sqlite3.Error:
            pass
        for gi, gh in gh_items:  # 全部展示（B22：不再"已点即隐"）；首选保持热榜置顶
            hot.insert(
                0,
                {
                    "title": gh["full_name"],
                    "link": gh["url"],
                    "source": "GitHub",
                    "is_gh": True,
                    "description": gi["summary"],
                    "stars": gh["stars"],
                    "reason": gh["reason"],
                    "anchor": gi["anchor"],
                    "url": gh["url"],
                    "gh_ready": gi["gh_ready"],
                    "gh_brief_url": gi["gh_brief_url"],
                },
            )

    # B2：落推送记录 → 注入 🔄 重推徽章（B22：不再移除已点击条目，看完还在）
    record_pushed(uid, used_date, sections)
    annotate_repush(uid, used_date, sections)

    # B15 探索频道：试验中源的条目打 🧪 徽章（注解不删除，不影响 B2/B1）
    try:
        explorer.annotate_trial(sections)
    except Exception:
        pass

    # B13 事件聚类：同板块内“多家媒体报道同一件事”打标（注解不删除，不影响 B2/B1）。
    # 缓存未就绪时后台构建（当次不折叠，刷新后生效），失败不影响主流程。
    try:
        if get_cluster_enabled():
            md_mtime = report_file.stat().st_mtime
            payload = cluster_load_cached(used_date, md_mtime)
            if payload is None:
                _start_cluster_build(used_date, report_file, md_mtime)
            else:
                apply_clusters(sections, payload)
    except Exception:
        pass

    # B1：当天条目 + SoS 增量入历史归档索引（幂等）
    try:
        archive_from_feed(used_date, sections, sos)
    except sqlite3.Error:
        pass

    return {
        "date": used_date,
        "report_file": report_file.name,
        "sos": sos,
        "sections": sections,
        "hot": hot,
        "skills": skills_today,
    }


class ClickBody(BaseModel):
    day: str
    anchor: str = ""
    url: str
    title: str = ""
    source: str = ""
    kind: str = "link"  # "link" 标题原文 | "paper" 解读入口


def _autocapture_interest(arxiv_id: str, uid: int) -> None:
    """B17：用户对同一篇论文既点过解读(kind=paper)又点过原文链接(kind=link)
    视为强兴趣 → 自动写入【该用户自己的】研究兴趣画像并后台提取关键词
    （每篇仅 1 次 LLM）。之后命中这些关键词的条目排序 +15 并打 🎯
    （即"推荐类似关键词论文"）。已在画像中的（含手动提交/分析中/失败）不重复处理。"""
    with get_conn() as conn:
        kinds = {
            r["kind"]
            for r in conn.execute(
                "SELECT DISTINCT kind FROM clicks WHERE user_id=? AND arxiv_id=?",
                (uid, arxiv_id),
            )
        }
        if not {"paper", "link"} <= kinds:
            return  # 只点过一种 = 普通阅读，不做强化
        row = conn.execute(
            """SELECT title, url FROM clicks
               WHERE user_id=? AND arxiv_id=? AND title != ''
               ORDER BY clicked_at DESC LIMIT 1""",
            (uid, arxiv_id),
        ).fetchone()
    if get_paper(uid, arxiv_id) is not None:
        return
    title = ((row["title"] if row else "") or "").strip() or f"arXiv:{arxiv_id}"
    url = ((row["url"] if row else "") or "").strip() or f"https://arxiv.org/abs/{arxiv_id}"
    if upsert_paper(uid, arxiv_id, title, url):
        spawn_analysis(load_env(ENV_PATH), uid, arxiv_id)
        print(f"[Interest] B17 自动纳入双击论文：{arxiv_id}（{title[:40]}）")


@app.post("/api/click")
def api_click(request: Request, body: ClickBody):
    """B2：点击埋点（B26 按当前用户归属；访客也记录，不参与兴趣强化）。"""
    u = current_user(request) or {"id": 1, "role": "guest"}
    uid, role = int(u["id"]), u["role"]
    if body.kind not in ("link", "paper"):
        body.kind = "link"
    url = (body.url or "").strip()
    now = datetime.now().isoformat(timespec="seconds")
    try:
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO clicks
                   (day, anchor, url, title, source, kind, role, url_norm, arxiv_id, clicked_at, user_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(day, anchor, user_id, kind) DO UPDATE SET
                       clicked_at = excluded.clicked_at""",
                (
                    body.day,
                    body.anchor,
                    url,
                    body.title,
                    body.source,
                    body.kind,
                    role,
                    url_norm(url),
                    extract_arxiv_id(url) or "",
                    now,
                    uid,
                ),
            )
    except sqlite3.Error:
        return {"ok": False}
    # B17：双击（解读入口 + 原文链接）同一篇论文 → 自动纳入本人兴趣画像。
    # 任何失败都不影响埋点本身。
    aid = extract_arxiv_id(url) or ""
    if role != "guest" and aid:
        try:
            _autocapture_interest(aid, uid)
        except Exception:  # noqa: BLE001 - 兴趣强化为旁路功能
            pass
    return {"ok": True}


@app.get("/api/search")
def api_search(q: str = ""):
    """B1：历史归档搜索（标题/摘要/标签，多关键词 AND，按日期倒序）。"""
    return {"q": q, "results": search_archive(q)}


class FeedbackBody(BaseModel):
    title: str
    url: str
    source: str
    vote: str  # "up" | "down"


@app.post("/api/feedback")
def api_feedback(request: Request, body: FeedbackBody):
    """记录投票（B26 归属当前用户）并更新来源权重（全局，仅 admin 投票生效）。"""
    denied = guest_denied(request)
    if denied:
        return denied
    if body.vote == "up":
        delta, vote_val = UP_DELTA, 1
    elif body.vote == "down":
        delta, vote_val = DOWN_DELTA, -1
    else:
        return {"ok": False, "error": "vote 必须是 up 或 down"}

    now = datetime.now().isoformat(timespec="seconds")
    today = date.today().isoformat()
    uid = current_uid(request)
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO votes (created_at, item_title, item_url, source, vote, day, user_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (now, body.title, body.url, body.source, vote_val, today, uid),
        )
    # source_weights 是全局口味（admin 调教）；member 投票只留记录，不动全局权重
    if is_admin(request):
        new_weight = update_source_weight(body.source, delta)
    else:
        new_weight = read_source_weight(body.source)
    return {"ok": True, "source": body.source, "weight": new_weight}


@app.get("/api/hot/sources")
def api_hot_sources(request: Request):
    """B22/B23：热榜来源投票统计（👍/👎 计数 + 是否已抑制），仅管理员。

    B23：返回包含用户自定义源（custom=True 带 url），供 ⚙ 设置面板管理。
    """
    denied = guest_denied(request)
    if denied:
        return denied
    try:
        stats = _hot_vote_stats()
    except sqlite3.Error:
        stats = {}
    custom = _hot_custom_names()
    custom_urls = {name: url for name, url, _ in load_custom_sources()}
    sources = [
        {
            "source": name,
            "custom": name in custom,
            "url": custom_urls.get(name, ""),
            **stats.get(name, {"up": 0, "down": 0, "muted": False}),
        }
        for name in _hot_vote_sources()
    ]
    return {"sources": sources}


class HotResetBody(BaseModel):
    source: str


@app.post("/api/hot/reset")
def api_hot_reset(request: Request, body: HotResetBody):
    """B22：重置某热榜来源的全部投票（解除差评抑制/屏蔽），仅管理员。"""
    denied = guest_denied(request)
    if denied:
        return denied
    if body.source not in _hot_vote_sources():
        return {"ok": False, "error": "未知热榜来源"}
    with get_conn() as conn:
        conn.execute("DELETE FROM votes WHERE source = ?", (body.source,))
    return {"ok": True, "source": body.source}


@app.post("/api/hot/mute")
def api_hot_mute(request: Request, body: HotResetBody):
    """B23：屏蔽热榜来源 = 立即写 3 条差评（触发 downs>=3 抑制规则），仅管理员。

    恢复走既有 /api/hot/reset（清空该源全部投票）。
    """
    denied = guest_denied(request)
    if denied:
        return denied
    if body.source not in _hot_vote_sources():
        return {"ok": False, "error": "未知热榜来源"}
    today = datetime.now().strftime("%Y-%m-%d")
    now = datetime.now().isoformat(timespec="seconds")
    with get_conn() as conn:
        conn.execute("DELETE FROM votes WHERE source = ? AND item_title = '（屏蔽来源）'",
                     (body.source,))  # 幂等：重屏蔽前先清旧的屏蔽票，避免叠加
        for _ in range(3):
            conn.execute(
                """INSERT INTO votes (created_at, item_title, item_url, source, vote, day)
                   VALUES (?, '（屏蔽来源）', '', ?, -1, ?)""",
                (now, body.source, today),
            )
    return {"ok": True, "source": body.source, "muted": True}


class HotCustomBody(BaseModel):
    name: str
    url: str


@app.post("/api/hot/custom")
def api_hot_custom_add(request: Request, body: HotCustomBody):
    """B23：添加自定义热榜源（RSS 地址），仅管理员。添加后立即生效。"""
    denied = guest_denied(request)
    if denied:
        return denied
    name = (body.name or "").strip()
    url = (body.url or "").strip()
    if not name or not url:
        return {"ok": False, "error": "名称与 RSS 地址不能为空"}
    if len(name) > 30:
        return {"ok": False, "error": "名称过长（≤30 字符）"}
    if not url.lower().startswith(("http://", "https://")):
        return {"ok": False, "error": "地址需以 http:// 或 https:// 开头"}
    if name in HOT_VOTE_SOURCES or name == "GitHub":
        return {"ok": False, "error": "与内置热榜源重名，请换个名称"}
    try:
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO hot_custom_sources (name, url, created_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET url = excluded.url""",
                (name, url, datetime.now().isoformat(timespec="seconds")),
            )
    except sqlite3.Error as exc:
        return {"ok": False, "error": f"写入失败：{exc}"}
    clear_hot_cache()  # 下次抓取立即带上新源
    return {"ok": True, "name": name, "url": url}


@app.post("/api/hot/custom/remove")
def api_hot_custom_remove(request: Request, body: HotResetBody):
    """B23：删除自定义热榜源（连同其投票记录），仅管理员。"""
    denied = guest_denied(request)
    if denied:
        return denied
    if body.source not in _hot_custom_names():
        return {"ok": False, "error": "不是自定义热榜源（内置源不可删除，只能屏蔽）"}
    with get_conn() as conn:
        conn.execute("DELETE FROM hot_custom_sources WHERE name = ?", (body.source,))
        conn.execute("DELETE FROM votes WHERE source = ?", (body.source,))
    clear_hot_cache()
    return {"ok": True, "source": body.source}


# ---------------- B23：🧩 Skills 推荐板块（收藏 / 点击 / 搜索） ----------------

class SkillActionBody(BaseModel):
    dedup_key: str


class SkillVoteBody(BaseModel):
    dedup_key: str
    vote: int  # 1 = 好评，-1 = 差评


@app.get("/api/skills")
def api_skills_today(request: Request):
    """今日 5 条 Skills 推荐（skill_daily 冻结批次 + 曝光埋点；收藏标记按当前用户）。"""
    try:
        return {"skills": skills_mod.today_picks(current_uid(request))}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


@app.get("/api/skills/categories")
def api_skills_categories():
    """分类清单（含条数），供前端筛选。"""
    try:
        return {"categories": skills_mod.categories()}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


@app.get("/api/skills/search")
def api_skills_search(request: Request, q: str = "", category: str = "", limit: int = 30):
    """全库搜索：收藏优先 → 净好评 → 相关性 → star（收藏按当前用户）。"""
    try:
        return {
            "skills": skills_mod.search_skills(
                q, category, min(max(limit, 1), 100), uid=current_uid(request)
            )
        }
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


@app.get("/api/skills/favorites")
def api_skills_favorites(request: Request):
    """收藏夹（按收藏时间倒序，B26 按当前用户）。"""
    try:
        return {"skills": skills_mod.favorites(current_uid(request))}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


@app.post("/api/skills/favorite")
def api_skills_favorite(request: Request, body: SkillActionBody):
    """收藏 / 取消收藏（幂等切换；B26 写当前用户自己的收藏夹）。"""
    denied = guest_denied(request)
    if denied:
        return denied
    return skills_mod.favorite(current_uid(request), body.dedup_key)


@app.post("/api/skills/click")
def api_skills_click(request: Request, body: SkillActionBody):
    """点击埋点（推荐权重 + 交互判定用）。"""
    skills_mod.record_click(body.dedup_key)
    return {"ok": True}


@app.post("/api/skills/vote")
def api_skills_vote(request: Request, body: SkillVoteBody):
    """👍/👎（搜索排序里的"好评优先"）。"""
    return skills_mod.record_vote(body.dedup_key, body.vote)


@app.get("/api/skill/brief")
def api_skill_brief(request: Request, dedup_key: str):
    """B24：单条技能的 AI 推荐词。已缓存→ready；生成中→pending；都没有→懒起生成。

    error 表示本轮生成失败（前端回退显示原始描述），下次 feed 循环会重试。
    """
    try:
        cached = skills_mod.brief_get(dedup_key)
        if cached:
            return {"status": "ready", **cached}
        with SKILL_BRIEF_LOCK:
            if dedup_key in SKILL_BRIEF_INFLIGHT:
                return {"status": "pending"}
            err = SKILL_BRIEF_ERRORS.get(dedup_key)
        if err is not None:
            return {"status": "error", "error": err}
        sk = skills_mod.get_skill(dedup_key)
        if not sk:
            return {"status": "missing"}
        _start_skill_brief(dedup_key, sk)
        return {"status": "pending"}
    except Exception as exc:  # noqa: BLE001 - 任何异常都不打断前端轮询
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}


_SKILL_KEY_RE = re.compile(r"[A-Za-z0-9\u4e00-\u9fff/_.:\-]{1,150}")


@app.get("/api/skill/detail")
def api_skill_detail(request: Request, k: str, force: int = 0):
    """B25：单条技能的深度解读。缓存→ready；生成中→pending；都没有→懒起生成。

    解读只在用户点开 /skill 详情页时才生成（省 token），生成一次永久缓存；
    error 可用 force=1 清掉重试。
    """
    try:
        if not _SKILL_KEY_RE.fullmatch(k or ""):
            return {"status": "error", "message": "k 参数不合法"}
        cached = skills_mod.detail_get(k)
        if cached is not None:
            sk = skills_mod.get_skill(k) or {}
            return {"status": "ready", "content": cached, "meta": _skill_meta(sk)}
        with SKILL_DETAIL_LOCK:
            if k in SKILL_DETAIL_INFLIGHT:
                return {"status": "pending"}
            err = SKILL_DETAIL_ERRORS.get(k)
            if force and err:
                SKILL_DETAIL_ERRORS.pop(k, None)
                err = None
        if err is not None:
            return {"status": "error", "message": err}
        sk = skills_mod.get_skill(k)
        if not sk:
            return {"status": "missing", "message": "未找到该技能"}
        _start_skill_detail(k, sk, force=bool(force))
        return {"status": "pending", "message": "正在生成深度解读…"}
    except Exception as exc:  # noqa: BLE001 - 任何异常都不打断前端轮询
        return {"status": "error", "message": f"{type(exc).__name__}: {exc}"}


def _skill_meta(sk: dict) -> dict:
    """详情页头部 meta 字段（skill 表行 → 展示字典）。"""
    return {
        "name": sk.get("name") or "(未命名)",
        "url": sk.get("source_url") or "",
        "description": sk.get("description") or "",
        "category": sk.get("category") or "",
        "origin": sk.get("origin") or "",
        "stars": sk.get("stars", 0),
        "verified": bool(sk.get("verified")),
        "github_repo": sk.get("github_repo") or "",
    }


@app.get("/api/weights")
def api_weights():
    """返回当前来源权重与关键词权重。"""
    with get_conn() as conn:
        src_rows = conn.execute(
            "SELECT source, weight, updated_at FROM source_weights ORDER BY source"
        ).fetchall()
        kw_rows = conn.execute(
            "SELECT keyword, weight, updated_at FROM keyword_weights ORDER BY keyword"
        ).fetchall()
    return {
        "source_weights": [dict(r) for r in src_rows],
        "keyword_weights": [dict(r) for r in kw_rows],
    }


def load_user_keywords(uid: int = 1) -> tuple:
    """读取用户手动配置的关注词/屏蔽词，返回 (focus_list, block_list)。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT keyword, kind FROM user_keywords WHERE user_id = ?", (uid,)
        ).fetchall()
    focus = [r["keyword"] for r in rows if r["kind"] == "focus"]
    block = [r["keyword"] for r in rows if r["kind"] == "block"]
    return focus, block


class PrefBody(BaseModel):
    action: str  # "add" | "remove"
    kind: str  # "focus" | "block"
    keyword: str


@app.get("/api/prefs")
def api_prefs_get(request: Request):
    """返回当前用户的关注词/屏蔽词列表。"""
    focus, block = load_user_keywords(current_uid(request))
    return {"focus": focus, "block": block}


@app.post("/api/prefs")
def api_prefs_post(request: Request, body: PrefBody):
    """增删当前用户的关注词/屏蔽词。"""
    denied = guest_denied(request)
    if denied:
        return denied
    if body.kind not in ("focus", "block"):
        return {"ok": False, "error": "kind 必须是 focus 或 block"}
    if body.action not in ("add", "remove"):
        return {"ok": False, "error": "action 必须是 add 或 remove"}
    keyword = body.keyword.strip()
    if not keyword or len(keyword) > 50:
        return {"ok": False, "error": "关键词需为 1~50 个字符"}
    uid = current_uid(request)
    now = datetime.now().isoformat(timespec="seconds")
    with get_conn() as conn:
        if body.action == "add":
            conn.execute(
                """INSERT INTO user_keywords (user_id, keyword, kind, created_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(user_id, keyword) DO UPDATE SET kind = excluded.kind""",
                (uid, keyword, body.kind, now),
            )
        else:
            conn.execute(
                "DELETE FROM user_keywords WHERE user_id = ? AND keyword = ?",
                (uid, keyword),
            )
    focus, block = load_user_keywords(uid)
    return {"ok": True, "focus": focus, "block": block}


# ---------------- 研究兴趣画像（📚 Zotero 论文导入） ----------------


@app.get("/api/interests")
def api_interests_get(request: Request):
    """返回当前用户已添加的兴趣论文与聚合关键词。"""
    uid = current_uid(request)
    papers = list_papers(uid)
    keywords = list_keywords(uid)
    return {"papers": papers, "keywords": keywords}


class InterestBody(BaseModel):
    input: str  # arXiv 链接 / 裸 id / 论文标题


@app.post("/api/interests")
def api_interests_add(request: Request, body: InterestBody):
    """添加兴趣论文：链接/id 直接入库并后台分析；标题返回候选列表等用户点选。"""
    denied = guest_denied(request)
    if denied:
        return denied
    text = (body.input or "").strip()
    if not text:
        return {"ok": False, "error": "输入不能为空"}

    uid = current_uid(request)
    # 链接或裸 arXiv id：直接确认入库
    arxiv_id = extract_arxiv_id(text)
    if arxiv_id:
        existing = get_paper(uid, arxiv_id)
        if existing:
            return {"ok": True, "added": False, "paper": existing}
        url = f"https://arxiv.org/abs/{arxiv_id}"
        upsert_paper(uid, arxiv_id, title=text if re.fullmatch(r"[0-9]{4}\.[0-9]{4,5}", text) else text, url=url)
        env = load_env(ENV_PATH)
        spawn_analysis(env, uid, arxiv_id)
        return {"ok": True, "added": True, "paper": get_paper(uid, arxiv_id)}

    # 论文标题：arXiv 检索出候选，让用户点选
    if len(text) < 5:
        return {"ok": False, "error": "标题太短，建议粘贴 arXiv 链接或完整标题"}
    candidates = search_arxiv_by_title(text)
    if not candidates:
        return {"ok": False, "error": "arXiv 未检索到相似标题，请检查论文标题或改用链接"}
    return {"ok": True, "candidates": candidates}


class InterestSelectBody(BaseModel):
    arxiv_id: str
    title: str = ""
    url: str = ""


@app.post("/api/interests/select")
def api_interests_select(request: Request, body: InterestSelectBody):
    """用户从候选中点选确认后入库，触发后台关键词分析。"""
    denied = guest_denied(request)
    if denied:
        return denied
    arxiv_id = extract_arxiv_id(body.arxiv_id or "")
    if not arxiv_id:
        return {"ok": False, "error": "arxiv_id 不合法"}
    uid = current_uid(request)
    existing = get_paper(uid, arxiv_id)
    if existing:
        return {"ok": True, "added": False, "paper": existing}
    upsert_paper(uid, arxiv_id, title=(body.title or arxiv_id).strip()[:300], url=body.url or f"https://arxiv.org/abs/{arxiv_id}")
    env = load_env(ENV_PATH)
    spawn_analysis(env, uid, arxiv_id)
    return {"ok": True, "added": True, "paper": get_paper(uid, arxiv_id)}


class InterestRemoveBody(BaseModel):
    arxiv_id: str


@app.post("/api/interests/remove")
def api_interests_remove(request: Request, body: InterestRemoveBody):
    """移除一篇兴趣论文并重算关键词聚合。"""
    denied = guest_denied(request)
    if denied:
        return denied
    if not (body.arxiv_id or "").strip():
        return {"ok": False, "error": "arxiv_id 不能为空"}
    uid = current_uid(request)
    removed = remove_paper(uid, body.arxiv_id.strip())
    if not removed:
        return {"ok": False, "error": "论文不存在"}
    return {"ok": True, "papers": list_papers(uid), "keywords": list_keywords(uid)}


# ---------------- 日报风格（B7：切换 ai-news profile 的摘要文风 prompt） ----------------


@app.get("/api/style")
def api_style_get():
    """返回日报摘要风格预设与当前选中项（按文件内容实时匹配）。"""
    return list_styles()


class StyleBody(BaseModel):
    style: str


@app.post("/api/style")
def api_style_post(request: Request, body: StyleBody):
    """切换日报摘要风格：写 ai-news profile 的 enrichment.md，首次自动备份可逆。"""
    denied = guest_denied(request)
    if denied:
        return denied
    try:
        state = set_style(body.style)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, **state}


# ---------------- 每日推送配额（B11：⚙ 偏好面板可调） ----------------


@app.get("/api/quotas")
def api_quotas_get(request: Request):
    """返回当前用户三类每日推送配额当前值与合法区间。"""
    return {
        "quotas": get_quotas(current_uid(request)),
        "limits": QUOTA_LIMITS,
        "cluster_enabled": get_cluster_enabled(),
    }


class QuotaBody(BaseModel):
    ghrepo: int | None = None  # 🛠 GitHub 热门项目 每天推送个数（1~10）
    classic: int | None = None  # 📜 经典论文 每天推送篇数（1~10）
    papers: int | None = None  # 📄 论文板块 每天推送篇数（0~30，0 = 不限制）
    cluster_enabled: bool | None = None  # 🧩 事件聚类去重开关


@app.post("/api/quotas")
def api_quotas_post(request: Request, body: QuotaBody):
    """修改当前用户每日推送配额；聚类开关为全局设置仅管理员可改。超范围自动钳制。"""
    denied = guest_denied(request)
    if denied:
        return denied
    if body.cluster_enabled is not None and not is_admin(request):
        return JSONResponse(
            {"ok": False, "error": "板块开关仅管理员可修改"}, status_code=403
        )
    uid = current_uid(request)
    values = {}
    for name in ("ghrepo", "classic", "papers"):
        val = getattr(body, name)
        if val is not None:
            values[name] = val
    has_cluster = body.cluster_enabled is not None
    if not values and not has_cluster:
        return {"ok": False, "error": "没有提供任何要修改的配额"}
    quotas = set_quotas(uid, values) if values else get_quotas(uid)
    if has_cluster:
        set_cluster_enabled(body.cluster_enabled)
    return {"ok": True, "quotas": quotas, "cluster_enabled": get_cluster_enabled()}


# ---------------- B21 缓存管理（⚙ 偏好面板分类清理 + 打分缓存轮转） ----------------

HORIZON_DATA = HORIZON_CONFIG.parent
SCORE_CACHE_DB = HORIZON_DATA / "score_cache.db"
SUMMARIES_DIR = HORIZON_DATA / "summaries"

CACHE_TTL_KEY = "score_cache_ttl_days"
CACHE_TTL_DEFAULT = 0  # 0 = 永久保留
CACHE_TTL_CHOICES = (0, 30, 90, 180, 365)
_cache_maint_day: date | None = None


def get_cache_ttl() -> int:
    """读取打分缓存轮转天数（app_settings 表；0 = 永久保留）。"""
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT value FROM app_settings WHERE key = ?", (CACHE_TTL_KEY,)
            ).fetchone()
    except sqlite3.Error:
        return CACHE_TTL_DEFAULT
    try:
        days = int(row["value"]) if row else CACHE_TTL_DEFAULT
    except (TypeError, ValueError):
        return CACHE_TTL_DEFAULT
    return days if days in CACHE_TTL_CHOICES else CACHE_TTL_DEFAULT


def set_cache_ttl(days: int) -> int:
    """写入轮转天数（非法值回退永久），返回生效值。"""
    if days not in CACHE_TTL_CHOICES:
        days = CACHE_TTL_DEFAULT
    now = datetime.now().isoformat(timespec="seconds")
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                               updated_at = excluded.updated_at""",
            (CACHE_TTL_KEY, str(days), now),
        )
    return days


def run_cache_maintenance() -> int:
    """每日一次（幂等）：按 score_cache_ttl_days 轮转打分缓存，返回删除行数。

    0 = 永久保留（默认）。Horizon 只管写缓存，轮转策略由 radar 统一执行，
    删掉的条目重现时会自动重新打分，无一致性问题。"""
    global _cache_maint_day
    today = date.today()
    if _cache_maint_day == today:
        return 0
    _cache_maint_day = today
    ttl = get_cache_ttl()
    if ttl <= 0 or not SCORE_CACHE_DB.exists():
        return 0
    cutoff = (today - timedelta(days=ttl)).isoformat()
    try:
        conn = sqlite3.connect(SCORE_CACHE_DB, timeout=5.0)
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            with conn:
                cur = conn.execute("DELETE FROM scores WHERE analyzed_at < ?", (cutoff,))
                return max(0, cur.rowcount)
        finally:
            conn.close()
    except sqlite3.Error:
        return 0


def _clear_tables(*tables: str) -> None:
    """清空若干表；表尚不存在（功能未跑过）视为无数据可清，其余错误向上抛。"""
    with get_conn() as conn:
        for t in tables:
            try:
                conn.execute(f"DELETE FROM {t}")
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc).lower():
                    raise


def cache_clear_dir(d: Path) -> int:
    """删除目录下全部 *.md，返回删除个数（仅限该目录，不递归）。"""
    n = 0
    if d.exists():
        for f in d.glob("*.md"):
            try:
                f.unlink()
                n += 1
            except OSError:
                pass
    return n


def _dir_stats(d: Path) -> tuple:
    """目录内 *.md 的（文件数, 总字节）。"""
    try:
        files = [f for f in d.glob("*.md") if f.is_file()]
    except OSError:
        return 0, 0
    return len(files), sum(f.stat().st_size for f in files)


def _score_cache_stats() -> tuple:
    """打分缓存（行数, 字节；只读连接，行数查询失败不挡大小统计）。"""
    if not SCORE_CACHE_DB.exists():
        return 0, 0
    size = SCORE_CACHE_DB.stat().st_size
    try:
        with sqlite3.connect(f"file:{SCORE_CACHE_DB}?mode=ro", uri=True) as conn:
            row = conn.execute("SELECT COUNT(*) FROM scores").fetchone()
            return (row[0] if row else 0), size
    except sqlite3.Error:
        return 0, size


def _table_rows(*names: str) -> int:
    """feedback.db 若干表的行数合计（任一表缺失按 0 计）。"""
    total = 0
    try:
        with get_conn() as conn:
            for n in names:
                try:
                    total += conn.execute(f"SELECT COUNT(*) FROM {n}").fetchone()[0]
                except sqlite3.Error:
                    pass
    except sqlite3.Error:
        return 0
    return total


def _fmt_bytes(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


def cache_categories() -> list:
    """全部运行期存储数据的分类清单（clearable=False 为口味/防重复数据，永不清）。"""
    sc_rows, sc_bytes = _score_cache_stats()
    pb_count, pb_bytes = _dir_stats(PAPERS_DIR)
    gb_count, gb_bytes = _dir_stats(GHREPOS_DIR)
    sm_count, sm_bytes = _dir_stats(SUMMARIES_DIR)
    fb_bytes = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    return [
        {"id": "score_cache", "icon": "📊", "name": "论文质量打分缓存",
         "desc": "同一条目的 ⭐ 评分记录（永久去重打分），清空后条目重现会重新调 LLM（耗 API）",
         "rows": sc_rows, "bytes": sc_bytes, "clearable": True},
        {"id": "paper_briefs", "icon": "📄", "name": "论文深度解读",
         "desc": "点开「论文解读」生成的 markdown，清空后再次点开同一篇会重新生成",
         "rows": pb_count, "bytes": pb_bytes, "clearable": True},
        {"id": "ghrepo_briefs", "icon": "🛠", "name": "GitHub 项目解读",
         "desc": "项目解读 markdown，清空后再次点开同一个项目会重新生成",
         "rows": gb_count, "bytes": gb_bytes, "clearable": True},
        {"id": "summaries", "icon": "📰", "name": "历史日报归档",
         "desc": "每天的日报 markdown，清空后历史日期无法回看（之后照常生成）",
         "rows": sm_count, "bytes": sm_bytes, "clearable": True},
        {"id": "event_clusters", "icon": "🧩", "name": "事件聚类缓存",
         "desc": "同事件归组的中间结果，清空后下次生成日报时自动重建",
         "rows": _table_rows("event_clusters"), "bytes": 0, "clearable": True},
        {"id": "selections", "icon": "📆", "name": "经典/项目选型记录",
         "desc": "近期已选过的经典论文与 GitHub 项目，清空后可能重复推送近期选过的内容",
         "rows": _table_rows("classics", "ghrepos"), "bytes": 0, "clearable": True},
        {"id": "protected", "icon": "🔒", "name": "偏好与调教数据（不可清理）",
         "desc": "投票、来源/关键词权重、兴趣画像、Skill 收藏、点击记录、推送去重、探索频道状态、用户账号与个人设置——口味数据和防重复机制都在这里",
         "rows": _table_rows(
             "votes", "source_weights", "keyword_weights", "user_keywords",
             "interest_papers", "interest_keywords", "clicks", "pushed",
             "archive_index", "explorer_pool", "explorer_daily", "app_settings",
             "users", "user_settings", "skill_favorites",
         ), "bytes": fb_bytes, "clearable": False},
    ]


@app.get("/api/cache")
def api_cache_get(request: Request):
    """缓存管理面板数据：轮转配置 + 各类别量级（仅管理员）。"""
    denied = guest_denied(request)
    if denied:
        return denied
    cats = cache_categories()
    for c in cats:
        c["size_text"] = _fmt_bytes(c["bytes"])
    return {
        "ttl_days": get_cache_ttl(),
        "ttl_choices": list(CACHE_TTL_CHOICES),
        "categories": cats,
    }


class CacheTtlBody(BaseModel):
    days: int  # 0 = 永久；可选 30/90/180/365


@app.post("/api/cache/ttl")
def api_cache_ttl(request: Request, body: CacheTtlBody):
    """修改打分缓存轮转天数（仅管理员）。"""
    denied = guest_denied(request)
    if denied:
        return denied
    days = set_cache_ttl(body.days)
    return {"ok": True, "ttl_days": days}


class CacheClearBody(BaseModel):
    category: str


@app.post("/api/cache/clear")
def api_cache_clear(request: Request, body: CacheClearBody):
    """按类别清理缓存（仅管理员）；protected 类拒绝。"""
    denied = guest_denied(request)
    if denied:
        return denied
    cat = body.category
    try:
        if cat == "score_cache":
            if SCORE_CACHE_DB.exists():
                conn = sqlite3.connect(SCORE_CACHE_DB, timeout=5.0)
                conn.execute("PRAGMA busy_timeout=5000")
                try:
                    with conn:
                        conn.execute("DELETE FROM scores")
                finally:
                    conn.close()
            return {"ok": True, "msg": "打分缓存已清空，条目重现时将重新打分"}
        if cat == "paper_briefs":
            n = cache_clear_dir(PAPERS_DIR)
            return {"ok": True, "msg": f"已删除 {n} 个论文解读"}
        if cat == "ghrepo_briefs":
            n = cache_clear_dir(GHREPOS_DIR)
            return {"ok": True, "msg": f"已删除 {n} 个项目解读"}
        if cat == "summaries":
            n = cache_clear_dir(SUMMARIES_DIR)
            return {"ok": True, "msg": f"已删除 {n} 份历史日报"}
        if cat == "event_clusters":
            _clear_tables("event_clusters")
            return {"ok": True, "msg": "事件聚类缓存已清空，下次生成日报时自动重建"}
        if cat == "selections":
            _clear_tables("classics", "ghrepos")
            return {"ok": True, "msg": "选型记录已清空，经典/项目板块近期内容可能重新出现"}
    except (sqlite3.Error, OSError) as exc:
        return {"ok": False, "error": f"清理失败：{exc}"}
    return {"ok": False, "error": "未知类别"}


# ---------------- B15 探索频道（每日一个实验源，点击/投票裁决自动转正） ----------------


@app.get("/explorer/feed.xml")
def explorer_feed():
    """探索频道中转 feed：Horizon config 固定指向此地址，每日返回当日
    实验源的上游条目（带 <source>真实源名</source>）。"""
    xml = explorer.build_relay_feed()
    return Response(content=xml, media_type="application/rss+xml; charset=utf-8")


@app.get("/api/explorer")
def api_explorer_get():
    """探索频道状态（⚙ 面板）：开关、今日实验源、全池状态与试验统计。"""
    return explorer.pool_status()


class ExplorerBody(BaseModel):
    action: str  # "toggle" 开关 | "promote" 转正 | "cool" 冷却 | "reset" 重置
    source: str = ""


@app.post("/api/explorer")
def api_explorer_post(request: Request, body: ExplorerBody):
    """探索频道管理动作（仅管理员）。"""
    denied = guest_denied(request)
    if denied:
        return denied
    if body.action == "toggle":
        enabled = not explorer.get_explorer_enabled()
        explorer.set_explorer_enabled(enabled)
        return {"ok": True, "enabled": enabled}
    if body.action not in ("promote", "cool", "reset") or not body.source:
        return {"ok": False, "error": "action 须为 toggle/promote/cool/reset，且需带 source"}
    ok, msg = explorer.admin_action(body.action, body.source)
    return {"ok": ok, "message": msg}


# ---------------- 邮件推送配置（⚙ 偏好面板可改；写入 Horizon config.json，次日生效） ----------------

EMAIL_FIELDS = ("enabled", "email_address", "smtp_server", "smtp_port", "sender_name")


def _read_horizon_config() -> dict:
    """读取 Horizon data/config.json（前端邮件设置与日报推送共用同一份配置）。"""
    return json.loads(HORIZON_CONFIG.read_text(encoding="utf-8"))


@app.get("/api/email")
def api_email_get():
    """返回邮件推送配置的可编辑字段（SMTP 授权码在 .env，永不返回）。"""
    try:
        cfg = _read_horizon_config()
    except FileNotFoundError:
        return {"error": f"未找到 {HORIZON_CONFIG}"}
    except (json.JSONDecodeError, OSError) as exc:
        return {"error": f"配置读取失败：{exc}"}
    email = cfg.get("email", {})
    return {k: email.get(k) for k in EMAIL_FIELDS}


class EmailBody(BaseModel):
    enabled: bool | None = None
    email_address: str | None = None
    smtp_server: str | None = None
    smtp_port: int | None = None
    sender_name: str | None = None


@app.post("/api/email")
def api_email_post(request: Request, body: EmailBody):
    """修改邮件推送配置（仅管理员）：写 Horizon config.json 的 email 块，写前备份 .bak。"""
    denied = guest_denied(request)
    if denied:
        return denied
    updates = {k: getattr(body, k) for k in EMAIL_FIELDS if getattr(body, k) is not None}
    if not updates:
        return {"ok": False, "error": "没有提供任何要修改的字段"}
    if "email_address" in updates and "@" not in updates["email_address"]:
        return {"ok": False, "error": "邮箱格式不正确（需包含 @）"}
    if "smtp_port" in updates and not 1 <= updates["smtp_port"] <= 65535:
        return {"ok": False, "error": "端口需在 1~65535 之间"}
    try:
        cfg = _read_horizon_config()
    except FileNotFoundError:
        return {"ok": False, "error": f"未找到 {HORIZON_CONFIG}"}
    except (json.JSONDecodeError, OSError) as exc:
        return {"ok": False, "error": f"配置读取失败：{exc}"}
    email = cfg.setdefault("email", {})
    email.update(updates)
    try:
        shutil.copy2(HORIZON_CONFIG, HORIZON_CONFIG.parent / (HORIZON_CONFIG.name + ".bak"))
        HORIZON_CONFIG.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        return {"ok": False, "error": f"配置写入失败：{exc}"}
    return {"ok": True, "email": {k: email.get(k) for k in EMAIL_FIELDS},
            "note": "已保存，次日 08:00 推送生效"}


# ---------------- 论文解读（📄 详情页） ----------------


def _classic_item_from_db(anchor: str) -> dict | None:
    """B4：/api/paper 对 classic-* 锚点从 classics 表取条目（经典不在日报里）。"""
    aid = anchor[len("classic-"):].replace("-", ".", 1)
    try:
        with get_conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT title, url, abstract FROM classics WHERE arxiv_id = ?", (aid,)
            ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return {
        "title": row["title"],
        "url": row["url"],
        "summary": (row["abstract"] or "")[:300],
        "tags": [],
        "source": "经典回看",
        "anchor": anchor,
    }


@app.get("/api/paper")
def api_paper(date: str, anchor: str, force: int = 0):
    """论文解读：缓存命中直接返回；否则后台线程生成（每篇 LLM ≤3 次调用），前端轮询。"""
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return {"status": "error", "message": f"日期格式不合法：{date}"}
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", anchor or ""):
        return {"status": "error", "message": "anchor 参数不合法"}

    # 从日报定位条目（拿标题/摘要/标签/评分）；B4 经典回看条目从 classics 表定位
    used_date, report_file = resolve_report(date)
    item = None
    if anchor.startswith("classic-"):
        item = _classic_item_from_db(anchor)
    elif report_file is not None:
        for sec in parse_report(report_file.read_text(encoding="utf-8")):
            for it in sec["items"]:
                if it.get("anchor") == anchor:
                    item = it
                    break
            if item:
                break
    if item is None:
        return {"status": "error", "message": "未找到该条目（日报可能已更新）"}

    aid = extract_arxiv_id(item.get("url") or "")
    if not aid:
        return {"status": "error", "message": "该条目不是可解析的 arXiv 论文链接"}

    cached = load_cached(aid)
    if cached is not None:
        return {"status": "ready", "content": cached, "meta": _cache_meta(aid)}

    with PAPER_LOCK:
        err = PAPER_ERRORS.get(aid)
        if force and err:
            PAPER_ERRORS.pop(aid, None)
            err = None
        if aid not in PAPER_INFLIGHT and err is None:
            PAPER_INFLIGHT.add(aid)
            threading.Thread(
                target=_generate_paper_brief,
                args=(used_date, anchor, aid, item),
                daemon=True,
            ).start()

    if err:
        return {"status": "error", "message": err}
    return {"status": "pending"}


def _cache_meta(aid: str) -> dict:
    """解析缓存文件头部注释里的元信息（标题/链接/评分/作者/学科/摘要/生成时间）。"""
    try:
        head = cache_path(aid).read_text(encoding="utf-8")[:8000]
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


def _start_paper_generation(used_date: str, anchor: str, aid: str, item: dict) -> None:
    """预生成入口（api_feed 调用）：无错误且不在生成中才起后台线程；有错误等用户 force。"""
    with PAPER_LOCK:
        if PAPER_ERRORS.get(aid) is not None:
            return
        if aid in PAPER_INFLIGHT:
            return
        PAPER_INFLIGHT.add(aid)
    threading.Thread(
        target=_generate_paper_brief,
        args=(used_date, anchor, aid, item),
        daemon=True,
    ).start()


def _generate_paper_brief(used_date: str, anchor: str, aid: str, item: dict):
    """后台线程：调用 paper_brief skill 生成解读并落盘；无论成败都退出 inflight。"""
    try:
        with PAPER_SEM:
            env = load_env(ENV_PATH)
            content, meta = analyze_paper(
                env,
                item["title"],
                item["url"],
                report_summary=item.get("summary") or "",
                tags=item.get("tags") or [],
            )
            save_cached(aid, item["title"], item["url"], content, item.get("score"), meta=meta)
        with PAPER_LOCK:
            PAPER_ERRORS.pop(aid, None)
        print(f"[PaperBrief] {aid}（{used_date}/{anchor}）生成完成")
    except Exception as exc:  # noqa: BLE001 - 错误转给前端展示
        with PAPER_LOCK:
            PAPER_ERRORS[aid] = str(exc)
        print(f"[PaperBrief] {aid} 生成失败：{exc}")
    finally:
        with PAPER_LOCK:
            PAPER_INFLIGHT.discard(aid)


# ---------------- B12：GitHub 项目解读（🛠 详情页） ----------------


def _gh_anchor(full_name: str) -> str:
    """full_name → 详情页锚点：`NousResearch/hermes-agent` → `gh-nousresearch-hermes-agent`。"""
    return "gh-" + re.sub(r"[^0-9A-Za-z]+", "-", full_name).strip("-").lower()


def _start_gh_generation(used_date: str, anchor: str, full_name: str, gh: dict) -> None:
    """预生成入口（api_feed / api_ghrepo 调用）：无错误且不在生成中才起后台线程。"""
    with GH_LOCK:
        if GH_ERRORS.get(full_name) is not None:
            return
        if full_name in GH_INFLIGHT:
            return
        GH_INFLIGHT.add(full_name)
    threading.Thread(
        target=_generate_gh_brief,
        args=(used_date, anchor, full_name, gh),
        daemon=True,
    ).start()


def _generate_gh_brief(used_date: str, anchor: str, full_name: str, gh: dict):
    """后台线程：抓 README → LLM 生成项目解读并落盘；无论成败都退出 inflight。"""
    try:
        with PAPER_SEM:  # 与论文解读共用并发上限，防 LLM 限流
            env = load_env(ENV_PATH)
            readme = fetch_gh_readme(full_name)
            content = analyze_ghrepo(env, gh, readme)
            gh_save_cached(full_name, gh.get("url") or "", content, gh)
        with GH_LOCK:
            GH_ERRORS.pop(full_name, None)
        print(f"[GhBrief] {full_name}（{used_date}/{anchor}）生成完成")
    except Exception as exc:  # noqa: BLE001 - 错误转给前端展示
        with GH_LOCK:
            GH_ERRORS[full_name] = str(exc)
        print(f"[GhBrief] {full_name} 生成失败：{exc}")
    finally:
        with GH_LOCK:
            GH_INFLIGHT.discard(full_name)


# ---------------- B24：Skill AI 推荐词（🧩 卡片直显 + 📖 展开块） ----------------


def _start_skill_brief(dedup_key: str, sk: dict) -> None:
    """预生成入口（api_feed / api_skill_brief 调用）：已缓存/生成中/有错误均跳过。"""
    if skills_mod.brief_get(dedup_key):
        return
    with SKILL_BRIEF_LOCK:
        if SKILL_BRIEF_ERRORS.get(dedup_key) is not None:
            return
        if dedup_key in SKILL_BRIEF_INFLIGHT:
            return
        SKILL_BRIEF_INFLIGHT.add(dedup_key)
    threading.Thread(target=_generate_skill_brief, args=(dedup_key, sk),
                     daemon=True).start()


def _generate_skill_brief(dedup_key: str, sk: dict):
    """后台线程：LLM 生成评测式简介并入库；无论成败都退出 inflight。"""
    try:
        with PAPER_SEM:  # 与论文/项目解读共用并发上限，防 LLM 限流
            env = load_env(ENV_PATH)
            data = generate_brief(env, sk)
            skills_mod.brief_save(dedup_key, data["verdict"], data["when_use"],
                                  data["pros"], data["caveats"])
        with SKILL_BRIEF_LOCK:
            SKILL_BRIEF_ERRORS.pop(dedup_key, None)
        print(f"[SkillBrief] {sk.get('name')} 生成完成")
    except Exception as exc:  # noqa: BLE001 - 错误转给前端展示
        with SKILL_BRIEF_LOCK:
            SKILL_BRIEF_ERRORS[dedup_key] = str(exc)
        print(f"[SkillBrief] {sk.get('name')} 生成失败：{exc}")
    finally:
        with SKILL_BRIEF_LOCK:
            SKILL_BRIEF_INFLIGHT.discard(dedup_key)


def _start_skill_detail(dedup_key: str, sk: dict, force: bool = False) -> None:
    """深度解读懒生成入口（api_skill_detail 调用）：已缓存/生成中/有错误均跳过。"""
    if skills_mod.detail_get(dedup_key) is not None:
        return
    with SKILL_DETAIL_LOCK:
        if force:
            SKILL_DETAIL_ERRORS.pop(dedup_key, None)
        if SKILL_DETAIL_ERRORS.get(dedup_key) is not None:
            return
        if dedup_key in SKILL_DETAIL_INFLIGHT:
            return
        SKILL_DETAIL_INFLIGHT.add(dedup_key)
    threading.Thread(target=_generate_skill_detail, args=(dedup_key, sk),
                     daemon=True).start()


def _generate_skill_detail(dedup_key: str, sk: dict):
    """后台线程：抓 README → LLM 生成深度解读并入库；无论成败都退出 inflight。"""
    try:
        with PAPER_SEM:  # 与论文/项目/推荐词共用并发上限，防 LLM 限流
            env = load_env(ENV_PATH)
            content = generate_detail(env, sk)
            skills_mod.detail_save(dedup_key, content)
        with SKILL_DETAIL_LOCK:
            SKILL_DETAIL_ERRORS.pop(dedup_key, None)
        print(f"[SkillDetail] {sk.get('name')} 生成完成")
    except Exception as exc:  # noqa: BLE001 - 错误转给前端展示
        with SKILL_DETAIL_LOCK:
            SKILL_DETAIL_ERRORS[dedup_key] = str(exc)
        print(f"[SkillDetail] {sk.get('name')} 生成失败：{exc}")
    finally:
        with SKILL_DETAIL_LOCK:
            SKILL_DETAIL_INFLIGHT.discard(dedup_key)


# ---------------- 口令登录 ----------------

LOGIN_PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>访问验证 · AI 资讯雷达</title>
<style>
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:#0d1117; color:#e6edf3; min-height:100vh; display:flex;
         align-items:center; justify-content:center;
         font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; }
  .box { background:#161b22; border:1px solid #21262d; border-radius:14px;
         padding:34px 38px; width:320px; text-align:center; }
  h1 { font-size:18px; margin-bottom:6px; }
  p  { font-size:13px; color:#8b949e; margin-bottom:20px; }
  input { width:100%; padding:10px 12px; border-radius:8px; border:1px solid #30363d;
          background:#0d1117; color:#e6edf3; font-size:15px; text-align:center; letter-spacing:2px; }
  input:focus { outline:none; border-color:#1f6feb; }
  button { margin-top:12px; width:100%; padding:10px; border-radius:8px; border:none;
           background:#1f6feb; color:#fff; font-size:14px; cursor:pointer; }
  button:hover { background:#388bfd; }
  .err { color:#f85149; font-size:12px; margin-top:10px; min-height:14px; }
</style></head>
<body><div class="box">
  <h1>🛰 AI 资讯雷达</h1><p>私有面板，请输入访问口令</p>
  <p style="font-size:11px;margin:-14px 0 16px;">好友账号口令请联系管理员获取</p>
  <form method="get" action="/login">
    <input type="password" name="key" placeholder="访问口令" autofocus autocomplete="off">
    <button type="submit">进入</button>
  </form>
  <div class="err">__ERR__</div>
</div></body></html>"""


@app.get("/rss")
def rss_output(request: Request):
    """RSS 2.0 输出：当天日报条目供阅读器订阅（?key= 口令直连，只读不落库）。"""
    used_date, report_file = resolve_report(date.today().isoformat())
    if report_file is None:
        return Response("暂无日报文件", status_code=404, media_type="text/plain; charset=utf-8")
    sections = parse_report(report_file.read_text(encoding="utf-8"))
    base = str(request.base_url).rstrip("/")
    pub = formatdate(datetime.strptime(used_date, "%Y-%m-%d").timestamp(), localtime=True)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0">',
        "<channel>",
        "<title>AI 资讯雷达</title>",
        f"<link>{escape(base + '/')}</link>",
        f"<description>AI / 具身智能资讯雷达日报 · {used_date}</description>",
        "<language>zh-cn</language>",
        f"<lastBuildDate>{formatdate(report_file.stat().st_mtime, localtime=True)}</lastBuildDate>",
        f"<pubDate>{pub}</pubDate>",
    ]
    for sec in sections:
        for it in sec["items"]:
            link = it.get("url") or f"{base}/"
            desc = it.get("summary", "")
            tags = it.get("tags") or []
            if tags:
                desc += "\n标签：" + "、".join(tags)
            lines += [
                "<item>",
                f"<title>{escape(it['title'])}</title>",
                f"<link>{escape(link)}</link>",
                f"<guid>{escape(it.get('anchor') or link)}</guid>",
                f"<pubDate>{pub}</pubDate>",
                f"<category>{escape(sec['name'])}</category>",
                f"<description>{escape(desc)}</description>",
                "</item>",
            ]
    lines.append("</channel>")
    lines.append("</rss>")
    return Response("\n".join(lines), media_type="application/rss+xml; charset=utf-8")


@app.get("/login")
def login(request: Request, key: str | None = None):
    """口令校验（B8/B9 → B26 多用户）：users 表任一 active 口令正确则种 30 天 cookie。"""
    if key is not None:
        ip = request.client.host if request.client else "?"
        if _login_locked(ip):
            # B9：不区分"口令错/被锁"，避免给爆破者探测信息
            return HTMLResponse(LOGIN_PAGE.replace("__ERR__", "口令不正确，请重试"))
        u = user_by_passcode(key)
        if u:
            _login_clear(ip)
            resp = RedirectResponse("/", status_code=303)
            resp.set_cookie(
                SESSION_COOKIE, key,
                max_age=30 * 24 * 3600, httponly=True, samesite="lax",
            )
            return resp
        _login_record_fail(ip)
        return HTMLResponse(LOGIN_PAGE.replace("__ERR__", "口令不正确，请重试"))
    return HTMLResponse(LOGIN_PAGE.replace("__ERR__", ""))


@app.get("/api/me")
def api_me(request: Request):
    """当前会话身份（B26）：role（admin/member/guest）+ name（顶栏显示）。"""
    u = current_user(request)
    if not u:
        return {"role": "guest", "name": "", "uid": 0}
    return {"role": u["role"], "name": u["name"], "uid": u["id"]}


# ---------------- B26 用户管理（admin 专属） ----------------

class UserPayload(BaseModel):
    name: str
    email: str = ""
    role: str = "member"  # member=全功能好友 / guest=只读访客


def _gen_passcode() -> str:
    """B26：随机口令（12 字符 URL 安全），与现有口令冲突时重试。"""
    for _ in range(5):
        k = secrets.token_urlsafe(9)
        with get_conn() as conn:
            dup = conn.execute("SELECT 1 FROM users WHERE passcode = ?", (k,)).fetchone()
        if not dup:
            return k
    raise RuntimeError("口令生成冲突，请重试")


@app.get("/api/users")
def api_users_list(request: Request):
    """用户列表（admin）。口令不可逆查询（只可重置）。"""
    if (d := guest_denied(request)) or not is_admin(request):
        return d or JSONResponse({"ok": False, "error": "仅管理员"}, status_code=403)
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, email, role, active, created_at FROM users ORDER BY id"
        ).fetchall()
    return {"ok": True, "users": [dict(r) for r in rows]}


@app.post("/api/users")
def api_users_create(request: Request, body: UserPayload):
    """创建好友账号（admin）：随机口令，仅本次响应返回一次。"""
    if (d := guest_denied(request)) or not is_admin(request):
        return d or JSONResponse({"ok": False, "error": "仅管理员"}, status_code=403)
    name = (body.name or "").strip()[:30]
    if not name:
        return JSONResponse({"ok": False, "error": "名字不能为空"}, status_code=400)
    role = body.role if body.role in ("member", "guest") else "member"
    email = (body.email or "").strip()[:120]
    try:
        k = _gen_passcode()
    except RuntimeError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    with get_conn() as conn:
        dup = conn.execute("SELECT 1 FROM users WHERE name = ?", (name,)).fetchone()
        if dup:
            return JSONResponse({"ok": False, "error": "同名用户已存在"}, status_code=400)
        conn.execute(
            "INSERT INTO users (name, email, passcode, role, active, created_at) VALUES (?,?,?,?,1,?)",
            (name, email, k, role, datetime.now().isoformat(timespec="seconds")),
        )
    return {"ok": True, "passcode": k}  # 口令只显示这一次，前端提示保存


@app.post("/api/users/{uid}/reset")
def api_users_reset(request: Request, uid: int):
    """重置口令（admin）：旧 cookie 立即失效（缓存 30s 内收敛）。"""
    if (d := guest_denied(request)) or not is_admin(request):
        return d or JSONResponse({"ok": False, "error": "仅管理员"}, status_code=403)
    k = _gen_passcode()
    with get_conn() as conn:
        if not conn.execute("SELECT 1 FROM users WHERE id = ?", (uid,)).fetchone():
            return JSONResponse({"ok": False, "error": "用户不存在"}, status_code=404)
        conn.execute("UPDATE users SET passcode = ? WHERE id = ?", (k, uid))
    return {"ok": True, "passcode": k}


@app.post("/api/users/{uid}/toggle")
def api_users_toggle(request: Request, uid: int):
    """停用/启用（admin）：uid=1 与当前登录者不可停用。"""
    if (d := guest_denied(request)) or not is_admin(request):
        return d or JSONResponse({"ok": False, "error": "仅管理员"}, status_code=403)
    me = current_user(request)
    if uid == 1 or (me and uid == me["id"]):
        return JSONResponse({"ok": False, "error": "不能停用该账号"}, status_code=400)
    with get_conn() as conn:
        row = conn.execute("SELECT active FROM users WHERE id = ?", (uid,)).fetchone()
        if not row:
            return JSONResponse({"ok": False, "error": "用户不存在"}, status_code=404)
        conn.execute("UPDATE users SET active = ? WHERE id = ?", (0 if row["active"] else 1, uid))
        new_active = 0 if row["active"] else 1
    if not new_active:  # 停用立即踢出：清口令缓存
        with get_conn() as conn:
            row = conn.execute("SELECT passcode FROM users WHERE id = ?", (uid,)).fetchone()
        if row:
            with _USER_CACHE_LOCK:
                _USER_CACHE.pop(row["passcode"], None)
    return {"ok": True, "active": new_active}


# ---------------- 论文解读详情页 ----------------

PAPER_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>论文解读 · AI 资讯雷达</title>
<style>
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:#0d1117; color:#e6edf3; min-height:100vh; padding:28px 16px 60px;
         font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
         line-height:1.75; }
  .wrap { max-width:860px; margin:0 auto; }
  .back { display:inline-block; font-size:13px; color:#58a6ff; text-decoration:none; margin-bottom:18px; }
  .back:hover { text-decoration:underline; }
  #meta { background:#161b22; border:1px solid #21262d; border-radius:12px; padding:20px 24px; margin-bottom:22px; }
  #meta h1 { font-size:18px; line-height:1.5; margin-bottom:8px; }
  #meta h1 a { color:#e6edf3; text-decoration:none; }
  #meta h1 a:hover { color:#58a6ff; }
  #meta .sub { font-size:12px; color:#8b949e; margin-bottom:8px; }
  #meta .sub .score { color:#e3b341; font-weight:600; }
  #meta .authors { font-size:13px; color:#a8b3bf; margin-bottom:10px; line-height:1.6; }
  #meta .chips { margin-bottom:12px; }
  #meta .chips span { display:inline-block; font-size:11px; color:#79c0ff; background:#0c2d6b;
                      border:1px solid #1f3a5f; border-radius:999px; padding:1px 8px; margin:0 6px 4px 0; }
  #meta .abstract { font-size:13px; color:#8b949e; background:#0d1117; border:1px solid #21262d;
                    border-radius:8px; padding:10px 14px; margin-bottom:14px; line-height:1.7; }
  #meta .abstract b { color:#a8b3bf; }
  #meta .links { display:flex; gap:10px; }
  #meta .btn { display:inline-block; font-size:13px; color:#fff; background:#1f6feb;
               border-radius:8px; padding:7px 16px; text-decoration:none; }
  #meta .btn:hover { background:#388bfd; }
  #meta .btn.ghost { background:transparent; border:1px solid #30363d; color:#c9d1d9; }
  #meta .btn.ghost:hover { border-color:#58a6ff; color:#58a6ff; }
  #content { background:#161b22; border:1px solid #21262d; border-radius:12px; padding:22px 26px; }
  #content h3 { font-size:15px; color:#58a6ff; margin:18px 0 6px; }
  #content h3:first-child { margin-top:0; }
  #content p { font-size:14px; margin:6px 0; color:#c9d1d9; }
  #content ul { margin:6px 0; }
  #content li { font-size:14px; color:#c9d1d9; margin:3px 0 3px 18px; }
  #content code { background:#0d1117; border:1px solid #21262d; border-radius:4px;
                  padding:1px 5px; font-size:13px; color:#79c0ff; }
  #content a { color:#58a6ff; }
  .anno { display:inline-block; font-size:11px; border-radius:4px; padding:0 5px; margin:0 2px; }
  .anno.src { background:#1f6feb33; color:#79c0ff; }
  .anno.inf { background:#d2992233; color:#e3b341; }
  .anno.unk { background:#6e768166; color:#8b949e; }
  .loading, .errbox { text-align:center; padding:46px 0; font-size:14px; color:#8b949e; }
  .errbox button { margin-top:14px; padding:8px 22px; border-radius:8px; border:none;
                   background:#1f6feb; color:#fff; font-size:13px; cursor:pointer; }
</style></head>
<body><div class="wrap">
  <a class="back" href="/#__ANCHOR__" onclick="if (history.length > 1) { history.back(); return false; }">← 返回列表</a>
  <div id="meta" style="display:none">
    <h1><a id="ptitle" href="#" target="_blank" rel="noopener"></a></h1>
    <div class="sub"><span class="score" id="pscore"></span><span id="pgen"></span></div>
    <div class="authors" id="pauthors" style="display:none"></div>
    <div class="chips" id="psubjects"></div>
    <div class="abstract" id="pabstract" style="display:none"></div>
    <div class="links">
      <a id="plink" class="btn" href="#" target="_blank" rel="noopener">📄 arXiv 原文</a>
      <a id="ppdf" class="btn ghost" href="#" target="_blank" rel="noopener">📑 PDF 全文</a>
    </div>
  </div>
  <div id="content"><div class="loading">⏳ 正在加载…</div></div>
</div>
<script>
const DATE = '__DATE__', ANCHOR = '__ANCHOR__';
const box = document.getElementById('content');
let timer = null;

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
function inline(s) {
  let t = escHtml(s);
  t = t.replace(/`([^`]+)`/g, '<code>$1</code>');
  t = t.replace(/\[原文声明\]/g, '<span class="anno src">原文声明</span>');
  t = t.replace(/\[推断\]/g, '<span class="anno inf">推断</span>');
  t = t.replace(/\[未明确给出\]/g, '<span class="anno unk">未明确给出</span>');
  t = t.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  t = t.replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener">$1</a>');
  return t;
}
function renderMd(md) {
  let html = '', inList = false;
  const closeList = () => { if (inList) { html += '</ul>'; inList = false; } };
  for (const line of md.split('\n')) {
    if (/^#{2,3}\s+/.test(line)) {
      closeList(); html += '<h3>' + inline(line.replace(/^#{2,3}\s+/, '')) + '</h3>';
    } else if (/^[-*]\s+/.test(line)) {
      if (!inList) { html += '<ul>'; inList = true; }
      html += '<li>' + inline(line.replace(/^[-*]\s+/, '')) + '</li>';
    } else if (!line.trim()) {
      closeList();
    } else {
      closeList(); html += '<p>' + inline(line) + '</p>';
    }
  }
  closeList();
  return html;
}
async function load(force) {
  try {
    const r = await fetch('/api/paper?date=' + DATE + '&anchor=' + ANCHOR + (force ? '&force=1' : ''));
    const d = await r.json();
    if (d.status === 'ready') {
      const m = d.meta || {};
      if (m.title) {
        document.getElementById('meta').style.display = 'block';
        const t = document.getElementById('ptitle');
        t.textContent = m.title;
        if (m.url) t.href = m.url;
        document.getElementById('pscore').textContent = (m.score && m.score !== '-') ? '⭐ ' + m.score + ' / 10' : '';
        document.getElementById('pgen').textContent = m.generated_at ? ' · 解读生成于 ' + m.generated_at.replace('T', ' ') : '';
        const au = document.getElementById('pauthors');
        if (m.authors) { au.textContent = '👥 ' + m.authors; au.style.display = 'block'; }
        const sub = document.getElementById('psubjects');
        if (m.subjects) {
          sub.innerHTML = m.subjects.split(';').map(s => '<span>' + escHtml(s.trim()) + '</span>').join('');
        }
        const ab = document.getElementById('pabstract');
        if (m.abstract) {
          ab.innerHTML = '<b>官方摘要</b>　' + escHtml(m.abstract);
          ab.style.display = 'block';
        }
        const link = document.getElementById('plink');
        if (m.url) link.href = m.url;
        const pdf = document.getElementById('ppdf');
        if (m.url) pdf.href = m.url.replace('/abs/', '/pdf/');
        document.title = (m.title || '论文解读') + ' · AI 资讯雷达';
      }
      box.innerHTML = renderMd(d.content || '');
    } else if (d.status === 'pending') {
      box.innerHTML = '<div class="loading">⏳ 正在生成论文解读（首次约 10~30 秒）…</div>';
      if (!timer) timer = setTimeout(() => { timer = null; load(false); }, 2500);
    } else {
      box.innerHTML = '<div class="errbox">⚠ ' + escHtml(d.message || '生成失败') +
        '<br><button onclick="load(true)">重试</button></div>';
    }
  } catch (e) {
    box.innerHTML = '<div class="errbox">⚠ 请求失败：' + escHtml(String(e)) +
      '<br><button onclick="load(true)">重试</button></div>';
  }
}
load(false);
</script></body></html>"""


@app.get("/paper")
def paper_page(date: str, anchor: str):
    """论文解读详情页：静态壳，date/anchor 校验后注入，数据由 JS 轮询 /api/paper。"""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date or "") or not re.fullmatch(
        r"[A-Za-z0-9_.-]{1,80}", anchor or ""
    ):
        return HTMLResponse(
            '<p style="color:#f85149">参数不合法</p><p><a href="/">← 返回列表</a></p>',
            status_code=400,
        )
    html = PAPER_PAGE.replace("__DATE__", date).replace("__ANCHOR__", anchor)
    return HTMLResponse(html)


# ---------------- B12：GitHub 项目解读详情页 ----------------

GH_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>项目解读 · AI 资讯雷达</title>
<style>
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:#0d1117; color:#e6edf3; min-height:100vh; padding:28px 16px 60px;
         font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
         line-height:1.75; }
  .wrap { max-width:860px; margin:0 auto; }
  .back { display:inline-block; font-size:13px; color:#58a6ff; text-decoration:none; margin-bottom:18px; }
  .back:hover { text-decoration:underline; }
  #meta { background:#161b22; border:1px solid #21262d; border-radius:12px; padding:20px 24px; margin-bottom:22px; }
  #meta h1 { font-size:18px; line-height:1.5; margin-bottom:8px; }
  #meta h1 a { color:#e6edf3; text-decoration:none; }
  #meta h1 a:hover { color:#58a6ff; }
  #meta .sub { font-size:12px; color:#8b949e; margin-bottom:8px; }
  #meta .sub .stars { color:#e3b341; font-weight:600; }
  #meta .desc { font-size:13px; color:#a8b3bf; margin-bottom:10px; line-height:1.6; }
  #meta .chips { margin-bottom:12px; }
  #meta .chips span { display:inline-block; font-size:11px; color:#7ee787; background:#0f2417;
                      border:1px solid #1d4428; border-radius:999px; padding:1px 8px; margin:0 6px 4px 0; }
  #meta .links { display:flex; gap:10px; }
  #meta .btn { display:inline-block; font-size:13px; color:#fff; background:#1f6feb;
               border-radius:8px; padding:7px 16px; text-decoration:none; }
  #meta .btn:hover { background:#388bfd; }
  #content { background:#161b22; border:1px solid #21262d; border-radius:12px; padding:22px 26px; }
  #content h3 { font-size:15px; color:#7ee787; margin:18px 0 6px; }
  #content h3:first-child { margin-top:0; }
  #content p { font-size:14px; margin:6px 0; color:#c9d1d9; }
  #content ul { margin:6px 0; }
  #content li { font-size:14px; color:#c9d1d9; margin:3px 0 3px 18px; }
  #content code { background:#0d1117; border:1px solid #21262d; border-radius:4px;
                  padding:1px 5px; font-size:13px; color:#79c0ff; }
  #content a { color:#58a6ff; }
  .anno { display:inline-block; font-size:11px; border-radius:4px; padding:0 5px; margin:0 2px; }
  .anno.inf { background:#d2992233; color:#e3b341; }
  .anno.unk { background:#6e768166; color:#8b949e; }
  .loading, .errbox { text-align:center; padding:46px 0; font-size:14px; color:#8b949e; }
  .errbox button { margin-top:14px; padding:8px 22px; border-radius:8px; border:none;
                   background:#1f6feb; color:#fff; font-size:13px; cursor:pointer; }
</style></head>
<body><div class="wrap">
  <a class="back" href="/#__ANCHOR__" onclick="if (history.length > 1) { history.back(); return false; }">← 返回列表</a>
  <div id="meta" style="display:none">
    <h1><a id="gtitle" href="#" target="_blank" rel="noopener"></a></h1>
    <div class="sub"><span class="stars" id="gstars"></span><span id="ggen"></span></div>
    <div class="desc" id="gdesc" style="display:none"></div>
    <div class="chips" id="gchips"></div>
    <div class="links"><a id="glink" class="btn" href="#" target="_blank" rel="noopener">🛠 GitHub 仓库</a></div>
  </div>
  <div id="content"><div class="loading">⏳ 正在加载…</div></div>
</div>
<script>
const DATE = '__DATE__', ANCHOR = '__ANCHOR__';
const box = document.getElementById('content');
let timer = null;

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
function inline(s) {
  let t = escHtml(s);
  t = t.replace(/`([^`]+)`/g, '<code>$1</code>');
  t = t.replace(/\[推断\]/g, '<span class="anno inf">推断</span>');
  t = t.replace(/\[未明确给出\]/g, '<span class="anno unk">未明确给出</span>');
  t = t.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  t = t.replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener">$1</a>');
  return t;
}
function renderMd(md) {
  let html = '', inList = false;
  const closeList = () => { if (inList) { html += '</ul>'; inList = false; } };
  for (const line of md.split('\n')) {
    if (/^#{2,3}\s+/.test(line)) {
      closeList(); html += '<h3>' + inline(line.replace(/^#{2,3}\s+/, '')) + '</h3>';
    } else if (/^[-*]\s+/.test(line)) {
      if (!inList) { html += '<ul>'; inList = true; }
      html += '<li>' + inline(line.replace(/^[-*]\s+/, '')) + '</li>';
    } else if (!line.trim()) {
      closeList();
    } else {
      closeList(); html += '<p>' + inline(line) + '</p>';
    }
  }
  closeList();
  return html;
}
async function load(force) {
  try {
    const r = await fetch('/api/ghrepo?date=' + DATE + '&anchor=' + ANCHOR + (force ? '&force=1' : ''));
    const d = await r.json();
    if (d.status === 'ready') {
      const m = d.meta || {};
      if (m.full_name) {
        document.getElementById('meta').style.display = 'block';
        const t = document.getElementById('gtitle');
        t.textContent = m.full_name;
        if (m.url) t.href = m.url;
        document.getElementById('gstars').textContent =
          '⭐ ' + (parseInt(m.stars, 10) || 0).toLocaleString() + ' stars';
        document.getElementById('ggen').textContent =
          m.generated_at ? ' · 解读生成于 ' + m.generated_at.replace('T', ' ') : '';
        const de = document.getElementById('gdesc');
        if (m.description) { de.textContent = m.description; de.style.display = 'block'; }
        const chips = document.getElementById('gchips');
        if (m.language) chips.innerHTML = '<span>' + escHtml(m.language) + '</span>';
        const link = document.getElementById('glink');
        if (m.url) link.href = m.url;
        document.title = (m.full_name || '项目解读') + ' · AI 资讯雷达';
      }
      box.innerHTML = renderMd(d.content || '');
    } else if (d.status === 'pending') {
      box.innerHTML = '<div class="loading">⏳ 正在生成项目解读（首次约 10~30 秒）…</div>';
      if (!timer) timer = setTimeout(() => { timer = null; load(false); }, 2500);
    } else {
      box.innerHTML = '<div class="errbox">⚠ ' + escHtml(d.message || '生成失败') +
        '<br><button onclick="load(true)">重试</button></div>';
    }
  } catch (e) {
    box.innerHTML = '<div class="errbox">⚠ 请求失败：' + escHtml(String(e)) +
      '<br><button onclick="load(true)">重试</button></div>';
  }
}
load(false);
</script></body></html>"""


SKILL_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Skill 解读 · AI 资讯雷达</title>
<style>
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:#0d1117; color:#e6edf3; min-height:100vh; padding:28px 16px 60px;
         font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
         line-height:1.75; }
  .wrap { max-width:860px; margin:0 auto; }
  .back { display:inline-block; font-size:13px; color:#58a6ff; text-decoration:none; margin-bottom:18px; }
  .back:hover { text-decoration:underline; }
  #meta { background:#161b22; border:1px solid #21262d; border-radius:12px; padding:20px 24px; margin-bottom:22px; }
  #meta h1 { font-size:18px; line-height:1.5; margin-bottom:8px; }
  #meta h1 a { color:#e6edf3; text-decoration:none; }
  #meta h1 a:hover { color:#58a6ff; }
  #meta .sub { font-size:12px; color:#8b949e; margin-bottom:8px; }
  #meta .sub .stars { color:#e3b341; font-weight:600; }
  #meta .desc { font-size:13px; color:#a8b3bf; margin-bottom:10px; line-height:1.6; }
  #meta .chips { margin-bottom:12px; }
  #meta .chips span { display:inline-block; font-size:11px; color:#7ee787; background:#0f2417;
                      border:1px solid #1d4428; border-radius:999px; padding:1px 8px; margin:0 6px 4px 0; }
  #meta .chips span.official { color:#d2a8ff; background:#241a33; border-color:#3d2b56; }
  #meta .links { display:flex; gap:10px; }
  #meta .btn { display:inline-block; font-size:13px; color:#fff; background:#1f6feb;
               border-radius:8px; padding:7px 16px; text-decoration:none; }
  #meta .btn:hover { background:#388bfd; }
  #content { background:#161b22; border:1px solid #21262d; border-radius:12px; padding:22px 26px; }
  #content h3 { font-size:15px; color:#7ee787; margin:18px 0 6px; }
  #content h3:first-child { margin-top:0; }
  #content p { font-size:14px; margin:6px 0; color:#c9d1d9; }
  #content ul { margin:6px 0; }
  #content li { font-size:14px; color:#c9d1d9; margin:3px 0 3px 18px; }
  #content code { background:#0d1117; border:1px solid #21262d; border-radius:4px;
                  padding:1px 5px; font-size:13px; color:#79c0ff; }
  #content a { color:#58a6ff; }
  .anno { display:inline-block; font-size:11px; border-radius:4px; padding:0 5px; margin:0 2px; }
  .anno.inf { background:#d2992233; color:#e3b341; }
  .anno.unk { background:#6e768166; color:#8b949e; }
  .loading, .errbox { text-align:center; padding:46px 0; font-size:14px; color:#8b949e; }
  .errbox button { margin-top:14px; padding:8px 22px; border-radius:8px; border:none;
                   background:#1f6feb; color:#fff; font-size:13px; cursor:pointer; }
</style></head>
<body><div class="wrap">
  <a class="back" href="/" onclick="if (history.length > 1) { history.back(); return false; }">← 返回列表</a>
  <div id="meta" style="display:none">
    <h1><a id="ktitle" href="#" target="_blank" rel="noopener"></a></h1>
    <div class="sub"><span class="stars" id="kstars"></span><span id="ksub"></span></div>
    <div class="desc" id="kdesc" style="display:none"></div>
    <div class="chips" id="kchips"></div>
    <div class="links" id="klinks"></div>
  </div>
  <div id="content"><div class="loading">⏳ 正在加载…</div></div>
</div>
<script>
const KEY = '__KEY__';
const box = document.getElementById('content');
let timer = null;

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
function inline(s) {
  let t = escHtml(s);
  t = t.replace(/`([^`]+)`/g, '<code>$1</code>');
  t = t.replace(/\[推断\]/g, '<span class="anno inf">推断</span>');
  t = t.replace(/\[未明确给出\]/g, '<span class="anno unk">未明确给出</span>');
  t = t.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  t = t.replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener">$1</a>');
  return t;
}
function renderMd(md) {
  let html = '', inList = false;
  const closeList = () => { if (inList) { html += '</ul>'; inList = false; } };
  for (const line of md.split('\n')) {
    if (/^#{2,3}\s+/.test(line)) {
      closeList(); html += '<h3>' + inline(line.replace(/^#{2,3}\s+/, '')) + '</h3>';
    } else if (/^[-*]\s+/.test(line)) {
      if (!inList) { html += '<ul>'; inList = true; }
      html += '<li>' + inline(line.replace(/^[-*]\s+/, '')) + '</li>';
    } else if (!line.trim()) {
      closeList();
    } else {
      closeList(); html += '<p>' + inline(line) + '</p>';
    }
  }
  closeList();
  return html;
}
async function load(force) {
  try {
    const r = await fetch('/api/skill/detail?k=' + encodeURIComponent(KEY) + (force ? '&force=1' : ''));
    const d = await r.json();
    if (d.status === 'ready') {
      const m = d.meta || {};
      if (m.name) {
        document.getElementById('meta').style.display = 'block';
        const t = document.getElementById('ktitle');
        t.textContent = '🧩 ' + m.name;
        if (m.url) t.href = m.url;
        document.getElementById('kstars').textContent =
          (parseInt(m.stars, 10) || 0) > 0 ? '⭐ ' + (parseInt(m.stars, 10) || 0).toLocaleString() + ' stars' : '';
        document.getElementById('ksub').textContent =
          (m.category ? ' · ' + m.category : '') + (m.origin ? ' · 来自 ' + m.origin : '');
        const de = document.getElementById('kdesc');
        if (m.description) { de.textContent = m.description; de.style.display = 'block'; }
        const chips = [];
        if (m.category) chips.push('<span>' + escHtml(m.category) + '</span>');
        if (m.origin) chips.push('<span>' + escHtml(m.origin) + '</span>');
        if (m.verified) chips.push('<span class="official">✓ 官方</span>');
        document.getElementById('kchips').innerHTML = chips.join('');
        const links = document.getElementById('klinks');
        if (m.github_repo) {
          links.innerHTML = '<a class="btn" href="https://github.com/' + escHtml(m.github_repo) +
            '" target="_blank" rel="noopener">🛠 GitHub 仓库</a>';
        } else if (m.url) {
          links.innerHTML = '<a class="btn" href="' + escHtml(m.url) +
            '" target="_blank" rel="noopener">🔗 来源链接</a>';
        }
        document.title = (m.name || 'Skill 解读') + ' · AI 资讯雷达';
      }
      box.innerHTML = renderMd(d.content || '');
    } else if (d.status === 'pending') {
      box.innerHTML = '<div class="loading">⏳ 正在生成深度解读（首次约 10~60 秒）…</div>';
      if (!timer) timer = setTimeout(() => { timer = null; load(false); }, 2500);
    } else {
      box.innerHTML = '<div class="errbox">⚠ ' + escHtml(d.message || '生成失败') +
        '<br><button onclick="load(true)">重试</button></div>';
    }
  } catch (e) {
    box.innerHTML = '<div class="errbox">⚠ 请求失败：' + escHtml(String(e)) +
      '<br><button onclick="load(true)">重试</button></div>';
  }
}
load(false);
</script></body></html>"""


def _gh_item_from_db(date: str, anchor: str) -> dict | None:
    """B12：/api/ghrepo 按日期 + 锚点从 ghrepos 表定位项目（slug 反查不可逆，逐行匹配）。"""
    try:
        with get_conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM ghrepos WHERE day = ? ORDER BY rowid", (date,)
            ).fetchall()
            row = next((r for r in rows if _gh_anchor(r["full_name"]) == anchor), None)
            if row is None:  # 跨日回看兜底：全表找最近一次推送的同一项目
                rows = conn.execute("SELECT * FROM ghrepos ORDER BY day DESC, rowid").fetchall()
                row = next((r for r in rows if _gh_anchor(r["full_name"]) == anchor), None)
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return dict(row)


@app.get("/ghrepo")
def ghrepo_page(date: str, anchor: str):
    """GitHub 项目解读详情页：静态壳，date/anchor 校验后注入，数据由 JS 轮询 /api/ghrepo。"""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date or "") or not re.fullmatch(
        r"[A-Za-z0-9_.-]{1,80}", anchor or ""
    ):
        return HTMLResponse(
            '<p style="color:#f85149">参数不合法</p><p><a href="/">← 返回列表</a></p>',
            status_code=400,
        )
    html = GH_PAGE.replace("__DATE__", date).replace("__ANCHOR__", anchor)
    return HTMLResponse(html)


@app.get("/skill")
def skill_page(k: str):
    """Skill 深度解读详情页（B25）：静态壳，k 校验后注入，数据由 JS 轮询 /api/skill/detail。"""
    if not _SKILL_KEY_RE.fullmatch(k or ""):
        return HTMLResponse(
            '<p style="color:#f85149">参数不合法</p><p><a href="/">← 返回列表</a></p>',
            status_code=400,
        )
    return HTMLResponse(SKILL_PAGE.replace("__KEY__", k))


@app.get("/api/ghrepo")
def api_ghrepo(date: str, anchor: str, force: int = 0):
    """项目解读：缓存命中直接返回；否则后台线程生成（每个项目 LLM ≤3 次调用），前端轮询。"""
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return {"status": "error", "message": f"日期格式不合法：{date}"}
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", anchor or ""):
        return {"status": "error", "message": "anchor 参数不合法"}

    gh = _gh_item_from_db(date, anchor)
    if gh is None:
        return {"status": "error", "message": "未找到该项目（当天可能未推送）"}

    full_name = gh["full_name"]
    cached = gh_load_cached(full_name)
    if cached is not None:
        meta = gh_cache_meta(full_name)
        meta.setdefault("full_name", full_name)
        meta.setdefault("url", gh.get("url") or "")
        meta.setdefault("description", gh.get("description") or "")
        meta.setdefault("stars", gh.get("stars", 0))
        meta.setdefault("language", gh.get("language") or "")
        return {"status": "ready", "content": cached, "meta": meta}

    with GH_LOCK:
        err = GH_ERRORS.get(full_name)
        if force and err:
            GH_ERRORS.pop(full_name, None)
            err = None
        if full_name not in GH_INFLIGHT and err is None:
            GH_INFLIGHT.add(full_name)
            threading.Thread(
                target=_generate_gh_brief,
                args=(date, anchor, full_name, gh),
                daemon=True,
            ).start()
    return {"status": "pending", "message": err or "正在生成项目解读…"}


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# 启动时初始化数据库
init_db()

# B1：回填历史归档索引（幂等；已有数据则只补缺失的新文件）
try:
    _n = rebuild_archive()
    if _n:
        print(f"[archive] 回填历史归档索引 {_n} 条")
except Exception as _exc:
    print(f"[archive] 历史归档回填失败（不影响主流程）：{_exc}")

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8080)
