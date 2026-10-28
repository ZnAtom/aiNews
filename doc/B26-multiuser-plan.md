# B26 多用户改造计划（已定稿 2026-10-28）

> 状态：第一期实施中（P0 已完成，P1 进行中）。本文档是完整调研结论与改造方案，
> 摘要版见 [todo.md](todo.md)「B. 明确缺口 / 维护债」第 2 条。

## 1. 背景与目标

系统原本单人使用（口令 `RADAR_ADMIN_KEY` 登录）。现在希望朋友也能用，但每人需求不同：
有人看论文、有人不看；有人看新闻、有人不看；口味权重各自独立。

目标：**同一台服务器、同一套内容池，多个账号各自配置互不干扰**。

已拍板的三个决策（2026-10-28）：

| 决策点 | 结论 |
|---|---|
| 好友登录方式 | **邀请码口令**——admin 在网页上创建用户、生成随机口令发给朋友，无需邮箱/注册流程 |
| 实施节奏 | **两期上线**——第一期网页端多用户；第二期个性化邮件分发 |
| 邮件推送 | 二期做**每人个性化日报**（复用全局日报解析，按各人规则渲染截取） |

## 2. 调研结论（决定方案的关键事实）

1. **内容层天然全局共享，多用户 LLM 成本 ≈ 零增长**（最重要的结论）
   - 抓取 → Horizon 打分 → 生成日报 → 解读/聚类，全部发生在登录之前的全局流程；
   - Horizon 打分 prompt 本就不含个人口味，口味加权全在 radar 层本地规则（读偏好表重排序）；
   - 因此 score_cache、解读缓存、日报正文、聚类结果 **一人生成、全员复用**；
   - 唯一按人消耗的 LLM：各自兴趣库的论文关键词提取（B17，每篇 1 次小调用）。
2. **真正的个人数据很薄**，只有 6 类小表：关注/屏蔽词、兴趣论文库、点击/投票记录、
   推送去重记录、技能收藏、每日配额。全是纯文本小行，SQLite 毫无压力。
3. **两个意外发现**：
   - `keyword_weights` 全库（radar + Horizon）**没有任何写入点**（僵尸表）→ 保持全局不动；
   - `source_weights` 是 admin 调教的全局参数 → 保持全局；member 投票只记自己的票、
     展示当前全局权重，不修改它（admin 投票才调权重）。

## 3. 目标架构：三层模型

```text
L1 全局内容池（一份，所有人共享，谁都不重复花钱）
   抓取 → Horizon 打分 → 日报生成 → 解读/聚类缓存
   skills 技能库 / skill_daily 每日推荐批次 / AI 推荐词 skill_briefs

L2 用户层（每人一份，全部挂 user_id）
   users            账号表（口令=身份；admin 发邀请码、重置、停用）
   user_keywords    关注词/屏蔽词
   interest_papers / interest_keywords   兴趣画像（每人自己的库）
   clicks / votes / pushed               行为与推送去重记录
   skill_favorites  技能收藏夹
   user_settings    个人设置（每日推送配额 quota_*）

L3 分发层：api_feed 按「当前登录的人」的关注词 + 屏蔽词 + 兴趣画像排序过滤
```

### 登录与身份

- cookie 存口令（radar_key），`user_by_passcode()` 查 users 表得到身份（30s 内存缓存）；
- **uid=1 永远是 admin（你）**，老库所有单用户数据一次性迁移归属 uid=1，迁移幂等；
- 权限矩阵：

| 能力 | admin | member | guest(未登录) |
|---|---|---|---|
| 看日报/搜索/skills/解读 | ✅ | ✅ | ✅（若开启 guest 直通） |
| 关注词/屏蔽词/兴趣库/收藏/配额 | ✅（自己的） | ✅（自己的） | ❌ |
| 投票 | 记票 + 调全局 source_weights | 只记自己的票 | 不记录兴趣强化 |
| 板块开关（cluster 等） | ✅ 全局 | ❌（传了返回 403） | ❌ |
| 用户管理（创建/重置/停用） | ✅ | ❌ | ❌ |

## 4. 数据库改动明细

### 4.1 挂 user_id 的表（个人数据）

| 表 | 约束变更 | 迁移方式 |
|---|---|---|
| votes | 加列 user_id（无约束变更） | ALTER TABLE ADD COLUMN，旧数据归 uid=1 |
| user_keywords | `UNIQUE(user_id, keyword)` | 重建表（rename→建新→拷贝→drop） |
| pushed | `PRIMARY KEY(user_id, day, anchor)` | 同上 |
| clicks | `UNIQUE(day, anchor, user_id, kind)` | 同上 |
| interest_papers | `UNIQUE(user_id, arxiv_id)` | 同上（interest.py 内联迁移） |
| interest_keywords | `UNIQUE(user_id, keyword)` | 同上 |
| skill_favorites | `UNIQUE(user_id, dedup_key)` | 同上（skills.py 内联迁移） |

> SQLite 无法原地改 PRIMARY KEY/UNIQUE 约束，只能重建表；webapp 侧封装了
> `_rebuild_for_uid()`，interest.py / skills.py 各自内联同款逻辑（避免循环导入）。

### 4.2 保持全局的表（内容池，不随人数增长）

score_cache、解读缓存、日报、聚类、source_weights、keyword_weights（僵尸表）、
skills、skill_stats、skill_daily、skill_briefs、app_settings（cluster_enabled 等全局开关）。

### 4.3 新增表

- `users`：id、name、passcode（唯一）、role（admin/member）、active、created_at、last_seen
- `user_settings`：`PRIMARY KEY(user_id, key)`；init_db 迁移把 app_settings 的
  `quota_*` 复制给 uid=1（幂等，以空表为触发条件）

## 5. 接口与逻辑改动清单

| 模块 | 改动 |
|---|---|
| 认证 | login/api_me/auth_gate 查 users 表；新增 admin 专属四接口 GET/POST /api/users、reset、toggle；`_gen_passcode() = token_urlsafe(9)` |
| /api/feed | 签名加 request；关注词/屏蔽词/兴趣词/配额/重推徽章全部按 current_uid |
| /api/click | 点击记录挂 uid；兴趣自动捕获（B17）从"仅 admin"开放给所有登录用户，进各自的库 |
| /api/feedback | 投票挂 uid；admin 调全局权重，member 只记票 |
| /api/prefs | 读写当前用户的关注/屏蔽词 |
| /api/interests | 四接口全部挂 uid |
| /api/quotas | 读/写 user_settings；cluster_enabled 仅 admin，member 传则 403 |
| /api/skills | today_picks/search/favorites/favorite 挂 uid；favorited 标记按人；skill_daily 批次全局共享（谁先打开谁触发生成，大家同一批） |
| seen.py 去重（P2） | 从「admin 点过才全局剔除」改为**「全员点过才全局剔除」**：`COUNT(DISTINCT user_id) >= 活跃用户数` 才剔除，任何一人没点过就还会推给他（保住省 LLM 的特性） |
| 前端 index.html | me-badge 显示身份；admin 用户管理卡；偏好面板 member 可见但风格卡（style）仅 admin；配额卡走 per-user API |
| 缓存分类 | cache_categories 的 protected 清单补 users / user_settings / skill_favorites |

## 6. 容量与承载估算（小笔记本服务器）

| 项 | 估算 | 依据 |
|---|---|---|
| 每用户一年 | **~20-30MB** | 每天 30-80 条行为记录（点击/去重/投票，每条 ~200B）+ 偏好词几十条 + 兴趣库/收藏，纯文本 |
| 共享内容池 | **~95MB/年** | score_cache 每天数百条打分 JSON + 解论缓存 + 日报 markdown + skills 库 |
| 10 人 × 3 年 | **< 1.1GB** | 上面两项相加 |
| 舒适承载 | **10-30 人** | 网页查询全走索引毫秒级；瓶颈不在数据库 |
| 硬上限 | **~50-100 人** | 天花板是每天定时任务（抓取+打分+日报）的 LLM 配额与内存，与用户数基本无关 |

结论：这台笔记本跑 10-30 人绰绰有余，数据量三年不过 1GB 量级。

## 7. 实施步骤与进度

- [x] b1 计划落档（todo.md + 本文档）
- [x] b2/P0 users 表 + 迁移 + 认证改造 + 用户管理四接口
- [x] b3/P0 前端用户名显示 + admin 用户管理卡
- [ ] b4/P1 个人表挂 user_id（webapp 侧 4 表已完成；interest.py / skills.py 收尾中）
- [ ] b5/P1 全部读写接口改 current_uid（api_feed/click/feedback/skills 已改；prefs/interests/quotas 收尾中）
- [ ] b6/P1 前端偏好面板 member 可见性（风格卡 admin only）
- [ ] b7/P2 seen.py「全员点过才全局剔除」口径
- [ ] b8 编译 + 重启 radar-web + 真库双用户验证（登录/偏好隔离/点击隔离/配额隔离/去重）
- [ ] b9 CHANGELOG B26 条目 + todo.md 同步 + commit + push develop

## 8. 第二期规划（本次不做）

- **P3** 每人个性化日报邮件：复用全局 parse_report，按各人关注词/配额渲染自己的 top-N；
  /rss?k=个人口令 输出个人 RSS
- **P4** admin 用量面板（每用户点击量/存储占用）+ 备份策略调整（WAL 补齐、备份压缩）

## 9. 风险与回滚

- 所有迁移幂等（以"缺 user_id 列 / 空表"为触发条件），重复启动不会重复迁移；
- 迁移只把旧数据归给 uid=1，不删除任何数据；出问题可直接回退代码 + 恢复 feedback.db 备份；
- skill_daily 每日批次全局共享是**有意设计**（L1 内容池），个性化体现在收藏标记与分类偏好。
