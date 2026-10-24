# AI 资讯雷达 · 项目手册（现状 · 数据流 · 待办）

> 目标一句话：**只属于自己的 AI/具身智能资讯雷达**——多源采集 → 每条 1~2 句中文摘要+原文链接 → SoS 独家综合（标注共识/分歧）→ 网页 + 邮件推送 → 👍👎 调教。零订阅费（R9），全部走个人 API。
>
> 上游需求规格：[report.md](report.md) ｜ **变更/执行流水已拆分至 [CHANGELOG.md](CHANGELOG.md)**（2026-10-23 整理，只追加不修改；本文档只保留"当前状态 + 待办"快照，历史细节可查 CHANGELOG 或 git log）

---

## 一、当前状态一览

**端到端链路已全部跑通**（2026-10-23 补跑验收）：早 08:00 定时采集 → 日报产出并自动发邮件到订阅邮箱 → SoS 综合同步生成 → 网页可看、可👍👎、可搜索、有事件聚类。任一定时任务失败会**自动发告警邮件**（B14）。

运行中的服务（systemd 用户级，均已 enable）：

| 服务 | 方式 | 作用 |
|---|---|---|
| horizon-daily.timer → service | 每日 08:00 | 采集+LLM 处理+日报+邮件（全流程约 2~5 小时，`TimeoutStartSec=infinity`） |
| sos-daily.timer → service | 每日 08:40（`--wait-hours 4` 等日报就绪） | SoS 今日综合 |
| sos-weekly.timer → service | 每周一 09:00 | SoS 周报 |
| radar-distill.timer → service | 每周日 21:00 | 偏好蒸馏（水位后新增投票 ≥5 才跑，见⑦） |
| radar-backup.timer → service | 每日 07:30 | feedback.db 在线快照备份（落 `~/Myfile/backups/`，保留 7 份） |
| radar-web.service | 常驻 :8080 | 自研前端 + 调教 API（口令门禁，管理员/访客双口令见 systemd unit 环境变量与 Horizon .env，不入 git） |
| we-mp-rss.service | 常驻 :8001 | 微信公众号 → RSS 转换（机器之心/新智元） |
| RSSHub（Docker 容器，host 网络） | 常驻 :1200 | AIbase 日报 / The Batch / HF Daily Papers 三 route |

每日时间线：

```
07:30  feedback.db 快照备份到 ~/Myfile/backups/（保留 7 份）
08:00  Horizon 全量采集+逐条 enrich（约 2~5h，glm-5.3-flash 思考模型慢）→ 日报 md → 自动发邮件
08:40  SoS 服务启动，轮询等日报文件（最长 4h）→ 日报一出现即生成今日综合
周一 09:00  汇总近 7 天日报+SoS → 周报
周日 21:00  偏好蒸馏：水位后新增投票 ≥5 → LLM 蒸馏关键词权重
任意时间  浏览器开 http://127.0.0.1:8080（输入口令）看资讯流、点 👍👎
```

---

## 二、系统现在连接了哪些信息（信息源全景）

共 **11 个信息源 + 1 条 LLM 通道**，分四层：

### L1 自托管转换层（本机常驻服务，免订阅费的关键）

| 源 | 采集内容 | 通道 | 类别 |
|---|---|---|---|
| 机器之心 | 公众号文章（标题/链接/摘要） | 本机 we-mp-rss `http://127.0.0.1:8001/feed/MP_WXS_3073282833.rss?limit=30` | ai-news |
| 新智元 | 公众号文章 | 本机 we-mp-rss `:8001/feed/MP_WXS_3271041950.rss?limit=30` | ai-news |
| AIbase AI 日报 | 中文 AI 日报条目 | 本机 RSSHub `http://127.0.0.1:1200/aibase/daily` | ai-news |
| The Batch（DeepLearning.AI） | 英文 AI 周刊 | 本机 RSSHub `:1200/deeplearning/the-batch` | ai-news |
| HF Daily Papers | HuggingFace 每日热门论文 | 本机 RSSHub `:1200/huggingface/daily-papers` | papers |

- we-mp-rss：微信扫码授权一次（2026-10-21），自动抓订阅公众号文章转 RSS；受微信限频影响，条目采集量可能阶段性为 0，服务本身稳定。**增强抓取**（2026-10-21）：appmsgpublish 接口遇 200013 风控时按 120s/240s/360s/480s 递增退避重试 4 次，仍失败自动降级 free_publish 备用通道（再遇 200013 做 60s/120s 短退避），`core/wx/model/{web,app,api,free_publish}.py` 四文件已改。
- RSSHub：Docker 容器（`diygod/rsshub`，host 网络 + `PROXY_URI=http://127.0.0.1:7897` 走 clash 分流）。

### L2 外部 RSS/Atom（Horizon 直接抓，走 clash 代理）

| 源 | 采集内容 | 类别 |
|---|---|---|
| TLDR AI | 英文 AI 日报（`tldr.tech/api/rss/ai`） | ai-news |
| arXiv cs.RO | 机器人方向论文（`rss.arxiv.org/rss/cs.RO`） | papers |
| arXiv 关键词 API | 按 "humanoid robot / VLA / embodied AI" 检索最新 30 篇 | papers |
| Embodied-AI-Guide | 具身智能资源库的 GitHub commits.atom（垂直源） | papers |

### L3 Horizon 内置非 RSS 源

| 源 | 采集内容 | 参数 |
|---|---|---|
| Hacker News | AI 相关热帖 | top 30，`min_score` 50（点赞门槛） |
| Google News | "artificial intelligence OR embodied AI OR humanoid robot" | 英文/US，max 50 |

### L4 LLM 通道（所有"理解与生成"都走它）

- 火山方舟中转站（`OPENAI_BASE_URL`）+ 模型 `glm-5.3-flash`，密钥在 `app/Horizon/.env`。
- **该模型是强制思考模型**：每条 enrich 约 60 秒（并发 8 后全流程约 1 小时）；radar 侧已做流式+max_tokens=16000 兜底。
- 用途六类：② 条目评分与中文摘要、④ SoS 综合生成、⑤ 论文解读（paper_brief，每篇 ≤3 次调用）、🛠 GitHub 项目解读（ghrepo_brief）、B13 事件聚类复核、⑦ 调教蒸馏。

### 各源的去向（谁消费它）

- **进 Horizon 日报**：全部 11 源。
- **进 SoS 共识比对**（外部日报源，5 个）：AIbase、The Batch、机器之心、新智元、TLDR AI + 当日日报本身。HF Papers/arXiv/HN/Google News 通过日报间接参与。

---

## 三、这些信息是怎么被处理的（数据流全貌）

```
          ┌────────────── 每日 08:00 horizon-daily.service（约 2~5h）──────────────┐
11 个源 ─▶ ① 采集 ─▶ ② LLM 逐条 enrich ─▶ ③ digest 分组选编 ─▶ 日报 md ─┬─▶ ⑥ 邮件推送
          （24h 窗口）   match 评分≥6.0 才留      news≤14 / papers≤12      │   （全文 HTML）
                        →1~2 句中文摘要+标签+⭐  other≤2，总帽 30        │
          └──────────────────────────────────────────────────────────────┘
日报 md ──④ SoS 综合（sos-daily，真实 LLM）─────────────────────▶ out/sos/sos-YYYY-MM-DD.md
日报 md + SoS ──⑤ webapp :8080 解析 + 权重重排 ──────────────────▶ 网页资讯流
👍/👎 ──⑦ votes ─▶ 来源权重（即时影响⑤）；votes≥5 ─▶ LLM 蒸馏 ─▶ 关键词权重（影响⑤）
```

### ① 采集（Horizon fetcher）

- 每源拉取原始条目（标题、链接、摘要、发布时间），只留 **24 小时时间窗** 内的。
- 已知噪音处理：Hacker News 有 `min_score=50` 热度门槛；Google News 给的是 news.google.com 重定向链（可接受，点击会跳原文）。
- **跨日去重主闸**（B2）：入 digest 前查 radar `clicks` 表——管理员已点击的条目（论文按 arxiv_id、新闻按 URL）永不再推；未点击的可以一直重推。任何异常全放行，不阻断推送。

### ② LLM 逐条 enrich（Horizon enricher，最耗时的一步）

- 对每条候选做三件事（prompt 按 profile 分两套：`config/profiles/ai-news/` 与 `config/profiles/papers/`；摘要文风由 enrichment.md 决定，可在网页 ⚙偏好里切预设）：
  1. **match 评分**：与"AI/具身智能"相关性 0~10 分，**< 6.0 直接丢弃**（阈值在 `config.json → processing`，两 profile 均为 6.0）；
  2. **analysis/enrichment**：生成 **1~2 句中文摘要** + 标签 + ⭐️ 重要性分数；全中文输出（`ai.languages=["zh"]`）。
- 并发 8（实测零限流），约 1 小时跑完。
- 偶发 "Invalid enrichment artifact"（思考型输出不合 schema），Horizon 自带容错跳过。

### ③ digest 分组选编（Horizon digest）

- 防止某类源刷屏的**配额制**（`config.json → digest.category_groups`）：
  - AI 资讯组（ai-news）**≤ 14 条**、论文组（papers）**≤ 12 条**、其他兜底 ≤ 2 条，**总量帽 30**；
  - topic 去重（`topic_dedup: true`）。
- 产物：`app/Horizon/data/summaries/horizon-YYYY-MM-DD-zh.md`。每条固定格式：`<a id="item-xxx-N"></a>` 锚点 + `### [标题](原文链接) ⭐️ 分/10` + 中文摘要 + 来源行 + 标签——这个格式是下游 SoS 和前端解析的契约。

### ④ SoS 综合生成（app/radar/sos_daily.py，每日 08:40）

- **输入**：当日日报 md + 5 个外部日报源 RSS 原始条目（AIbase/The Batch/机器之心/新智元/TLDR）。
- **输出**：`out/sos/sos-YYYY-MM-DD.md`，固定四段式：
  - **🔗 共识要点**：≥2 个源同提的事件，必须注明源名（如 "The Batch、TLDR AI"）；
  - **🔍 独家观察**：单一来源但值得跟的线索，标注出处（含"日报未收录的条目"提示）；
  - **🧨 破圈推荐**：从全网热榜（aihot 热榜 + 36kr 快讯，已剔除日报收录）挑 1~2 条日报外的信号，各配一句话理由；
  - **🧭 我的综合判断**：跨源交叉后的趋势判断，明确区分"已确认事实/传闻/官方预测"。
- **硬规则**（report.md L349）：只基于当天入库内容做综合，**不引入模型自身知识**——这是"独有综合总结"（R11）的可信度基础。
- 每周版：`--weekly` 汇总近 7 天日报+SoS，周一 09:00 生成 `sos-week-YYYY-Www.md`。日/周报生成成功后自动单发一封 SoS 邮件（mail.py，仅"目标为今天且文件本次新建"时发）。

### ⑤ 前端展示与重排序（app/radar/webapp.py，:8080）

- **口令门禁**：@app.middleware 全站拦截，管理员/访客双口令（环境变量 `RADAR_ADMIN_KEY`/`RADAR_GUEST_KEY`），正确后种 30 天 cookie；访客只读（写 API 服务端 403）。
- `GET /api/feed?date=`：解析日报 md + 同日 SoS md → 板块 JSON（无当日日报自动回退最新一期）；另附 `hot` 字段：全网热榜 1~3 条（aihot 热榜 + 36kr 快讯，剔除日报已收录，进程内 30 分钟缓存，hotlist.py 共享模块）+ **🛠 GitHub 热门项目置顶**（B5/B11 按配额多条）。
- **重排序公式**（每个板块内）：

  ```
  条目权重 = source_weight(该条来源) × 10 + Σ(标题/摘要命中的 keyword_weights)
           + 8 × 命中关注词数 + 15 × 命中研究兴趣词数（🎯 徽章）− 1000 × 屏蔽词命中（沉底）
  按权重稳定降序；无任何调教数据时保持日报原序
  ```

- **展示链路**（api_feed 顺序）：落 pushed 记录 → 🔄 重推徽章注入 → 已点条目兜底过滤（论文/经典豁免——深度内容可反复看，新闻"已点即隐"）→ **🧩 事件聚类注解**（B13：同板块内多家媒体报道同一事件折叠聚合，LLM 复核缓存，失败不折叠自动重试）→ B1 归档入库。论文/经典配额截断（B11）在此前完成。
- `GET/POST /api/prefs`：关注词/屏蔽词增删查（user_keywords 表）；`/api/style` 日报风格、`/api/quotas` 推送配额 + 聚类开关、`/api/email` 邮件配置、`/api/interests*` 研究兴趣、`/api/search` 历史搜索、`/rss` RSS 订阅输出（?key= 直连）。
- **📄 论文解读 / 🛠 项目解读详情页**（paper_brief.py + ghrepo_brief.py）：日报条目带"📄/🛠 解读"入口 → **本页跳转**详情页，按需生成中文解读（论文六小节 / 项目七小节，带 `[原文声明]/[推断]/[未明确给出]` 反幻觉标注）；推送时后台预生成（共用 LLM 并发闸），缓存全局唯一（论文 `out/papers/{arxiv_id}.md`、项目 `out/ghrepos/{full_name}.md`）跨日复用；「← 返回列表」history.back() 回到列表原位置。

### ⑥ 邮件推送（Horizon email 服务 + B14 告警）

- horizon-daily 产报后自动把**日报全文**（Markdown→HTML，含样式）发给 `app/Horizon/data/subscribers.json` 里所有订阅者；邮件顶部附网页访问横幅（动态局域网 IP + 访客口令）。
- SMTP：smtp.qq.com:465（SSL），中文头 RFC2047 编码；SMTP 授权码在 Horizon .env，其余配置可在网页 ⚙偏好「📧 邮件推送」卡片改（写回 config.json，次日生效）。
- 订阅管理：用户回复邮件 **SUBSCRIBE/UNSUBSCRIBE**，Horizon 跑 pipeline 时经 IMAP 检查收件箱自动增删订阅者。
- **失败告警**（B14，2026-10-23）：horizon-daily / sos-daily / sos-weekly / radar-distill / radar-backup 任一失败 → systemd OnFailure → alert-mail@.service → alert_mail.py 抓 journalctl + 应用日志尾部发给订阅者，正文附手动补跑命令。测试：`./.venv/bin/python alert_mail.py test-alert.service`。

### ⑦ 调教闭环（👍👎 → 排序 → 蒸馏）

- **即时层**：点 👍 → 该来源 source_weight **+0.5**；👎 → **−1.0**；clamp 在 ±3。写入 `app/radar/data/feedback.db`（SQLite：votes / source_weights / keyword_weights / user_keywords / pushed / clicks / archive_index / interest_* / classics / ghrepos / app_settings / event_clusters），**下一次刷新页面即生效**。
- **蒸馏层**（`app/radar/distill.py` + 周日 21:00 timer）：首次 votes 总数 ≥5 触发；之后按增量水位（`data/distill_state.json` 记 `last_vote_id`）只需水位后新增投票 ≥5 即再蒸馏，蒸馏始终用全部历史投票。LLM 把点赞记录蒸馏成关键词权重（连赞"世界模型" → 该词权重升）+ 屏蔽关键词（写负权重），影响⑤排序。手动跑：`.venv/bin/python distill.py`。

### ⑧ 研究兴趣画像（Zotero 论文喂给雷达，2026-10-21 上线）

- **入口**：网页 ⚙偏好 tab「📚 研究兴趣」；粘贴 arXiv 链接（或裸 id）直接入库；输入论文标题则调 arXiv export API `ti:"标题"` 精确检索，返回 ≤5 篇候选供点选"就是这篇"确认。
- **分析**：入库后后台线程抓 arXiv 元数据（export API），每篇 1 次 LLM 提取 3~6 个英文小写研究方向关键词（如 world model / loco-manipulation / cross-embodiment），状态 pending → ready / error。
- **生效**：⑤排序每命中一个兴趣词 **+15**（高于关注词 8——来自真实读过的论文），命中条目显示 🎯 徽章（悬停看命中词）；实测 2026-10-21：喂入 Ropedia W0 一篇即让 7 篇相关论文加权，Ropedia W0 以 75 分居论文板块首位。

---

## 四、待办（未完成，优先级从上到下）

### A. 观察项（自动发生，无需动手，看到异常再处理）

1. **B13 事件聚类真实效果**：LLM key 已恢复（10-23 补跑成功），网页后台会自动构建当日聚类——日常观察 🧩 折叠是否误聚/漏聚，反馈给我调阈值或 prompt。
2. **首份周报**：周一 2026-10-26 09:00 自动生成，看质量是否达标。
3. **连续稳定产出**：连续多天日报/SoS 不空跑（A4/A5 验收尾巴）。
4. **机器之心/新智元采集量**：受微信限频阶段性为 0，恢复后自动入报（增强重试已就位）。
5. **调教真实效果**：日常点 👍👎 积累 votes（≥5 触发蒸馏），次日排序看变化。

### B. 明确缺口 / 维护债（要动手的）

1. **B6 📖 每日 AI 概念讲堂——【暂缓：推送哪些概念用户还没想好】**
   方案轮廓：预设学习路径（RAG → Tool Use → Embedding → Agent → MCP → Multi-Agent → RLHF…）每日推进 1 个，`concept_progress` 表记进度；仿 paper_brief 模式自研 `concept_brief.py`（LLM 生成：是什么/为什么重要/怎么用/例子/延伸阅读，反幻觉标注沿用）；前端 📖 今日概念板块 + 详情页 + 入 B1 搜索索引。想好概念清单即可开工。

### C. 可选增强（暂缓清单，做不做等指令）

- feedback.db 异地备份（当前备份在同盘 `~/Myfile/backups/`，防误删/库损坏，不防整机盘损）
- Docker 化收尾（Horizon/radar 容器化部署）
- newsnow 自托管前端（降级原则已定：自研前端即验收形态）
- 摘要风格 Profile 调优实验（三预设之外的自定义文风）

### D. 环境依赖提醒

- **app/Horizon/.env 含全部密钥（OPENAI_API_KEY / OPENAI_BASE_URL / HORIZON_MODEL / EMAIL_PASSWORD）**：任一为空都会导致日报失败（10-23 故障根因）。被误清空时用权威副本恢复：`cp config/.env app/Horizon/.env && chmod 600 app/Horizon/.env`；编辑器不要长期开着 .env。
- 代理 clash（7897）停用 → TLDR/arXiv/Google News 等外部源自动空跑，本机源（:8001/:1200）与 HN 不受影响。
- 微信授权长期有效但可能失效 → we-mp-rss 网页（:8001）重新扫码。
- doc/rss-sources.md 有 30+ 实测可用源，**不要一次全接**——现有 11 源跑顺后按痛点渐进添加。

---

## 五、已完成（✅ 详情查 [CHANGELOG.md](CHANGELOG.md) 对应日期条目）

### 建设阶段

| 阶段 | 内容 | 完成日 |
|---|---|---|
| 0 环境 | uv + Horizon venv + Docker + 代理摸底（clash 7897） | 10-21 |
| 1 MVP | Horizon 全流程、中文化、每日 08:00 timer、首份中文日报 | 10-21 |
| 2 源建设 | arXiv 论文板块 + we-mp-rss 公众号双号 + 自建 RSSHub 三源 + 配额修复 | 10-21 |
| 3 SoS 与调教 | app/radar 五件套（SoS/前端/调权/蒸馏）+ systemd 部署 | 10-21 |
| 4 邮件 | QQ SMTP + RFC2047 + 自动送达 + Embodied-AI-Guide 垂直源 | 10-21 |
| 5 体验升级 | 口令门禁/热榜/四段式 SoS/偏好面板/详情页/兴趣画像 | 10-21 |

### B 系列功能（全部完成，B6 暂缓）

| 项 | 内容 | 完成日 |
|---|---|---|
| B1 | 🔍 历史归档 archive_index + 全库搜索 /api/search（LIKE 兜底） | 10-21 |
| B2 | 👁 点击追踪 + ♻ 跨日去重（10-22 简化为"只看管理员点没点"） | 10-21 |
| B3 | 🧠 解读全局缓存（arxiv_id 唯一）+ 推送时预生成 | 10-21 |
| B4 | 🏆 经典论文回看（OpenAlex 高引 + LLM 理由） | 10-21 |
| B5 | 🛠 每日 GitHub 热门 AI 项目（GitHub Search API） | 10-21 |
| B7 | 📧 SoS 单发邮件 + 🗞 日报风格三预设切换 | 10-22 |
| B8 | 👥 访客只读模式（双口令分级） | 10-21 |
| B9 | 🛡 安全加固（登录防爆破 + 强口令） | 10-21 |
| B10 | 📧 邮件附网页访问地址（动态 IP + 访客口令） | 10-21 |
| B11 | 📦 每日推送数量配额（⚙ 面板可调，当天补选） | 10-22 |
| B12 | 🛠 GitHub 项目深度解读（七小节，README 为源） | 10-22 |
| B13 | 🧩 事件聚类去重（规则预聚 + LLM 复核 + 纯注解） | 10-22 |
| B14 | 🚨 定时任务失败告警邮件（systemd OnFailure） | 10-23 |
| B6 | 📖 每日 AI 概念讲堂——**暂缓**（见待办 B.1） | — |

### 其他

| 内容 | 完成日 |
|---|---|
| 开源化：LICENSE（MIT）+ GitHub Actions CI + README 截图 + `GET /rss` 订阅输出 | 10-22 |
| 📬 邮件推送前端配置（网页改邮箱写回 config.json） | 10-22 |
| 📚 doc/rss-sources.md 实测源清单（39 候选 → 30+ 可用，两轮扩容） | 10-22 |
| bug 修复：经典/论文看完消失 ×2、详情页返回定位 ×3（hash → 本页跳转演进）、首屏空白、中文 From 550、TimeoutStartSec、同名 import 覆盖等 | 10-21~23 |
| 10-23 故障恢复：.env 空 key 致日报失败 → 补跑日报 + SoS 全链路恢复 + 建 B14 告警 | 10-23 |
| 🚨 维护债清理：deploy/ 与实际安装侧对齐（4 service 补 OnFailure、radar-web 双口令模板化、alert-mail@ + radar-backup unit 入库）+ sos-weekly 补告警 | 10-24 |
| 💾 feedback.db 每日备份（backup_feedback.py 在线快照 + radar-backup timer，保留 7 份） | 10-24 |

---

## 六、仓库文件地图

| 路径 | 是什么 |
|---|---|
| `app/Horizon/` | 主壳：采集/enrich/digest/邮件（上游开源项目 + 本项目配置） |
| `app/Horizon/data/config.json` | **信息源清单与配额都在这**（改源、改阈值、改配额；邮件配置可从网页写回） |
| `app/Horizon/data/summaries/` | 每日日报产物（horizon-YYYY-MM-DD-zh.md） |
| `app/Horizon/data/subscribers.json` | 邮件订阅者列表 |
| `app/Horizon/.env` | API 密钥/中转站/邮箱授权码（**含敏感信息，勿入库勿外发**；权威副本 `config/.env`） |
| `app/radar/` | 自研模块：sos_daily.py（SoS 综合）/ webapp.py（前端+API）/ mail.py / distill.py / hotlist.py / paper_brief.py / ghrepo_brief.py / classic.py / ghrepo.py / interest.py / event_cluster.py / digeststyle.py / alert_mail.py / backup_feedback.py / static/index.html |
| `app/radar/data/feedback.db` | 调教与业务数据（votes / 权重 / 关键词 / pushed / clicks / archive_index / interest_* / classics / ghrepos / app_settings / event_clusters）——**不可再生；每日 07:30 自动备份至 `~/Myfile/backups/`（保留 7 份）** |
| `app/we-mp-rss/` | 公众号转 RSS 自托管服务 |
| `config/profiles/` | ai-news / papers 两套 LLM prompt（改摘要风格在这） |
| `config/.env` | Horizon .env 的权威副本（恢复用） |
| `deploy/` | systemd unit 源文件（git 版本化：全部 service + timer + alert-mail@.service，口令用占位符不入 git；实际安装于 ~/.config/systemd/user/，**改动需两侧同步**） |
| `out/` | 运行日志（horizon-daily.log 等）+ SoS 产物（out/sos/）+ 论文解读缓存（out/papers/）+ 项目解读缓存（out/ghrepos/） |
| `doc/` | report.md（需求规格）+ 本文档 + CHANGELOG.md（变更流水）+ rss-sources.md（源清单）+ img/ |

---

## 七、常用命令

```bash
# 手动跑一次日报（长任务，--no-block 必加）
systemctl --user start --no-block horizon-daily.service

# 手动生成今日 SoS / 周报（日报已存在时不等待）
cd ~/Myfile/AINews/app/radar && .venv/bin/python sos_daily.py --wait-hours 0
cd ~/Myfile/AINews/app/radar && .venv/bin/python sos_daily.py --weekly

# 手动跑蒸馏（votes≥5 才有产出）
cd ~/Myfile/AINews/app/radar && .venv/bin/python distill.py

# 测试失败告警邮件链路（会给订阅者发一封测试邮件）
cd ~/Myfile/AINews/app/radar && .venv/bin/python alert_mail.py test-alert.service

# 手动备份一次 feedback.db（落 ~/Myfile/backups/，每日 07:30 自动）
systemctl --user start radar-backup.service

# .env 被清空后的恢复（密钥权威副本）
cp config/.env app/Horizon/.env && chmod 600 app/Horizon/.env

# 状态与日志
systemctl --user list-timers
systemctl --user status radar-web.service
tail -n 50 ~/Myfile/AINews/out/horizon-daily.log

# 重启前端（改 webapp.py / index.html 后）
systemctl --user restart radar-web.service
```

---

## 八、已知问题与对策（滚动更新）

| 问题 | 对策 |
|---|---|
| glm-5.3-flash 强制思考模型：早期串行 enrich 60s/条远超 2h 被 `TimeoutStartSec=2h` SIGTERM | `TimeoutStartSec=infinity` + enrich 并发 1→8；radar 侧 `call_llm_robust` 兜底思考模型三坑（max_tokens 被推理占满 / 非流式 120s 超时 / thinking disabled 400 → 流式+16000） |
| QQ SMTP 拒收中文 From（550 "From header is missing or invalid"） | `src/services/email.py` 用 `Header` + `formataddr` 做 RFC2047 编码（From/Subject/退订回信三处），已实测通过 |
| app/Horizon/.env 两次被"未填写模板"覆盖 + 一次换 key 清空（10-23 日报失败根因） | 权威副本存于 `config/.env`，恢复：`cp config/.env app/Horizon/.env && chmod 600 app/Horizon/.env`；编辑器不要长期开着 .env；失败已有 B14 告警兜底 |
| 定时任务失败无感知（10-23 故障暴露） | ✅ 已建 B14 OnFailure 告警邮件（5 个定时任务全覆盖；deploy/ 已同步） |
| feedback.db 无备份（唯一不可再生数据） | ✅ 已建 radar-backup 每日在线快照（保留 7 份）；当前同盘，防误删不防整机盘损（异地备份列入可选增强） |
| webapp.py 同名 import 相互覆盖（paper_brief 与 event_cluster 的 load_cached） | 后导入者胜出会静默覆盖——新增模块导入一律检查同名冲突；B13 已用别名修复 |
| 新装 systemd timer `Persistent=true` 首次不补跑 | 无历史触发记录属正常；手动 `systemctl --user start sos-daily.service` 补当日 |
| 本机 127.0.0.1 源被 clash 劫持（httpx trust_env） | 所有涉及本机服务的 unit/脚本注入 `NO_PROXY=localhost,127.0.0.1`；radar 直连时 `trust_env=False` |
| rsshub.app 公共实例 403 | 自建 RSSHub（Docker，host 网络 + PROXY_URI 走 clash） |
| RSSHub HF daily-papers 503（DNS 污染 + Node 不读 HTTP_PROXY） | 容器加 `-e PROXY_URI=http://127.0.0.1:7897` |
| we-mp-rss 授权二维码无限加载 | playwright webkit 二进制缺失 → `playwright install webkit` |
| we-mp-rss 采集遇微信 200013 频率控制 | 增强抓取（2026-10-21）：主接口递增退避 120/240/360/480s 重试 4 次，耗尽自动降级 free_publish 通道（短退避 60/120s）；阶段性 0 条属微信侧现状 |
| we-mp-rss 重复点授权提示"请勿重复运行" | 授权会话持有 `data/lock.lock` → 重启服务+删锁文件 |
| fish shell 不支持 heredoc/反引号 | 给用户的命令一律单行 |
| X 源依赖 Apify 付费 | 已弃用（enabled=false） |
| Google News 返回重定向链接（非原文直链） | 可接受，点击跳原文 |
| docker pull 直连超时 | 用全限定域名 `docker.m.daocloud.io/<image>` |
