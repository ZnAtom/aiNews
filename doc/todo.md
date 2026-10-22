# AI 资讯雷达 · 项目手册（现状 · 数据流 · 待办）

> 目标一句话：**只属于自己的 AI/具身智能资讯雷达**——多源采集 → 每条 1~2 句中文摘要+原文链接 → SoS 独家综合（标注共识/分歧）→ 网页 + 邮件推送 → 👍👎 调教。零订阅费（R9），全部走个人 API。
>
> 上游需求规格：[report.md](report.md) ｜ 历史执行流水与变更记录见本文档末尾及 git log（整理于 2026-10-21，此前按阶段的详细执行细节已压缩，原文可查 git 历史）

---

## 一、当前状态一览

**端到端链路已全部跑通**（2026-10-21 实测）：早 08:00 定时采集 → 13:23 日报产出并自动发邮件到订阅邮箱 → SoS 综合同步生成 → 网页可看、可👍👎。

运行中的服务（systemd 用户级，均已 enable）：

| 服务 | 方式 | 作用 |
|---|---|---|
| horizon-daily.timer → service | 每日 08:00 | 采集+LLM 处理+日报+邮件（全流程约 2~5 小时，`TimeoutStartSec=infinity`） |
| sos-daily.timer → service | 每日 08:40（`--wait-hours 4` 等日报就绪） | SoS 今日综合 |
| sos-weekly.timer → service | 每周一 09:00 | SoS 周报 |
| radar-distill.timer → service | 每周日 21:00 | 偏好蒸馏（新增投票 ≥5 才跑，见⑦） |
| radar-web.service | 常驻 :8080 | 自研前端 + 调教 API（口令门禁，初始 0000；`--host 0.0.0.0` 局域网可访问 http://<局域网IP>:8080） |
| we-mp-rss.service | 常驻 :8001 | 微信公众号 → RSS 转换（机器之心/新智元） |
| RSSHub（Docker 容器，host 网络） | 常驻 :1200 | AIbase 日报 / The Batch / HF Daily Papers 三 route |

每日时间线：

```
08:00  Horizon 全量采集+逐条 enrich（约 2~5h，glm-5.3-flash 思考模型慢）→ 日报 md → 自动发邮件
08:40  SoS 服务启动，轮询等日报文件（最长 4h）→ 日报一出现即生成今日综合
周一 09:00  汇总近 7 天日报+SoS → 周报
周日 21:00  偏好蒸馏：水位后新增投票 ≥5 → LLM 蒸馏关键词权重
任意时间  浏览器开 http://127.0.0.1:8080（输入口令 0000）看资讯流、点 👍👎
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
- **该模型是强制思考模型**：每条 enrich 约 60 秒（这是全流程 2~5 小时的根本原因）；radar 侧已做流式+max_tokens=16000 兜底。
- 用途五类：② 条目评分与中文摘要、④ SoS 综合生成、⑤ 论文解读（paper_brief，每篇 ≤3 次调用）、⑦ 调教蒸馏、（未用）风格 Profile。

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

### ② LLM 逐条 enrich（Horizon enricher，最耗时的一步）

- 对每条候选做三件事（prompt 按 profile 分两套：`config/profiles/ai-news/` 与 `config/profiles/papers/`）：
  1. **match 评分**：与"AI/具身智能"相关性 0~10 分，**< 6.0 直接丢弃**（阈值在 `config.json → processing`，两 profile 均为 6.0）；
  2. **analysis/enrichment**：生成 **1~2 句中文摘要** + 标签 + ⭐️ 重要性分数；全中文输出（`ai.languages=["zh"]`）。
- 串行（并发 1，思考模型限制），约 60 秒/条 × 300 条上下 ⇒ 2~5 小时。
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
- 每周版：`--weekly` 汇总近 7 天日报+SoS，周一 09:00 生成 `sos-week-YYYY-Www.md`。

### ⑤ 前端展示与重排序（app/radar/webapp.py，:8080）

- **口令门禁**：@app.middleware 全站拦截，口令来自 `RADAR_ACCESS_KEY` 环境变量（deploy unit 设 0000），正确后种 30 天 cookie（`radar_key`）。
- `GET /api/feed?date=`：解析日报 md + 同日 SoS md → 板块 JSON（无当日日报自动回退最新一期）；另附 `hot` 字段：全网热榜 1~3 条（aihot 热榜 + 36kr 快讯，剔除日报已收录，进程内 30 分钟缓存，hotlist.py 共享模块）。
- **重排序公式**（每个板块内）：

  ```
  条目权重 = source_weight(该条来源) × 10 + Σ(标题/摘要命中的 keyword_weights)
           + 8 × 命中关注词数 + 15 × 命中研究兴趣词数（🎯 徽章）− 1000 × 屏蔽词命中（沉底）
  按权重稳定降序；无任何调教数据时保持日报原序
  ```

- `GET /` 返回深色单文件前端（newsnow 风格：SoS tab + 🔥热榜精选 tab + 动态板块 tab + ⚙偏好 tab + 每条 👍👎，移动端单列）；关注词命中标题高亮，屏蔽词条目半透明沉底。
- `GET/POST /api/prefs`：关注词/屏蔽词增删查（user_keywords 表）。
- **📄 论文解读详情页**（paper_brief.py + `GET /paper` + `GET /api/paper`）：日报论文板块的 arXiv 条目带"📄 解读"入口 → 深色详情页按需生成六小节解读（一句话总结 / 在做什么 / 什么方法 / 什么技术 / 效果如何 / 值得注意的点，带 `[原文声明]/[推断]/[未明确给出]` 反幻觉标注）。**详情页顶部为完整信息卡**（2026-10-21）：标题 / 作者 / arXiv 学科分类 / 官方英文摘要 / 原文链接 + PDF 直链；免费抓 arXiv abs 页 citation_* meta + 每篇 LLM ≤3 次（非流式 4000 失败切流式 12000/16000）；结果缓存 `out/papers/{date}/{anchor}.md`（头部注释含 authors/subjects/abstract），首次生成约 10~30 秒（前端 2.5 秒轮询），之后秒开；失败可 force=1 重试。

### ⑥ 邮件推送（Horizon email 服务）

- horizon-daily 产报后自动把**日报全文**（Markdown→HTML，含样式）发给 `app/Horizon/data/subscribers.json` 里所有订阅者（订阅邮箱列表，部署时自行添加）。
- SMTP：smtp.qq.com:465（SSL）；发件人 "AI 资讯雷达 <你的发件邮箱>"（中文头已做 RFC2047 编码，2026-10-21 修复）。
- 订阅管理：用户回复邮件 **SUBSCRIBE/UNSUBSCRIBE**，Horizon 跑 pipeline 时经 IMAP 检查收件箱自动增删订阅者。

### ⑦ 调教闭环（👍👎 → 排序 → 蒸馏）

- **即时层**：点 👍 → 该来源 source_weight **+0.5**；👎 → **−1.0**；clamp 在 ±3。写入 `app/radar/data/feedback.db`（SQLite 三表：votes / source_weights / keyword_weights），**下一次刷新页面即生效**。
- **蒸馏层**（`app/radar/distill.py` + 周日 21:00 timer）：首次 votes 总数 ≥5 触发；之后按增量水位（`data/distill_state.json` 记 `last_vote_id`）只需水位后新增投票 ≥5 即再蒸馏，蒸馏始终用全部历史投票。LLM 把点赞记录蒸馏成关键词权重（连赞"世界模型" → 该词权重升）+ 屏蔽关键词（写负权重），影响⑤排序。手动跑：`.venv/bin/python distill.py`。

### ⑧ 研究兴趣画像（Zotero 论文喂给雷达，2026-10-21 上线）

- **入口**：网页 ⚙偏好 tab 第三个区块「📚 研究兴趣」；粘贴 arXiv 链接（或裸 id）直接入库；输入论文标题则调 arXiv export API `ti:"标题"` 精确检索，返回 ≤5 篇候选供点选"就是这篇"确认。
- **分析**：入库后后台线程抓 arXiv 元数据（export API，必须 https + follow_redirects），每篇 1 次 LLM（glm-5.3-flash，max_tokens=4000 防推理挤占）提取 3~6 个英文小写研究方向关键词（如 world model / loco-manipulation / cross-embodiment），状态 pending → ready / error。
- **落库**：`app/radar/data/feedback.db` 新增两表——`interest_papers`（arxiv_id / title / url / status / keywords JSON / error）+ `interest_keywords`（keyword 主键 / hits 聚合，删论文自动重算）。
- **生效**：⑤排序每命中一个兴趣词 **+15**（高于关注词 8——来自真实读过的论文），命中条目显示 🎯 徽章（悬停看命中词）；实测 2026-10-21：喂入 Ropedia W0 一篇即让 7 篇相关论文加权，Ropedia W0 以 75 分居论文板块首位。
- **API**：`GET /api/interests`（论文+关键词）、`POST /api/interests`（添加，链接直入/标题返回候选）、`POST /api/interests/select`（候选确认）、`POST /api/interests/remove`（移除）。

---

## 四、已完成（简要）

| 阶段 | 内容 | 状态 |
|---|---|---|
| 0 环境 | uv + Horizon 独立 venv + Docker + 网络代理摸底（clash 7897） | ✅ |
| 1 MVP | Horizon 跑通全流程、中文化、每日 08:00 timer、首份中文日报 | ✅ |
| 2 中文与论文源 | arXiv 论文板块（双源）+ we-mp-rss 公众号双号 + 自建 RSSHub 三源 + digest 配额修复 | ✅ |
| 3 SoS 与调教 | app/radar 五件套（SoS 日/周综合、webapp、调权、蒸馏、深色前端）+ systemd 三 unit 部署 | ✅ |
| 4 邮件 | QQ SMTP 接入 + RFC2047 修复 + 测试邮件送达 + Embodied-AI-Guide 垂直源 | ✅ |

---

## 五、还没做的（重点）

### A. 观察项（自动发生，无需动手）

1. **首次全自动链路验收**：明早 08:00 / 08:40 两个 timer 的产物（今天 13:23 那次含手动重启触发成分）。
2. **A4/A5 连续 2 天稳定产出**（阶段 2 验收尾巴）：连续两天日报不空跑。
3. **机器之心/新智元采集量**：受微信限频，feed 目前可能 0 条，待其恢复后自动入报。
4. **调教真实效果**：需要日常使用中点 👍👎 积累 votes，次日排序才会看到变化。
5. **首份周报**：周一 2026-10-26 09:00。

### B. 明确缺口（2026-10-21 用户集中提出 + 当日追加，实施中）

> 实施顺序（用户拍板）：~~B3 → B9/B8/B10 → B2 → B1 → B4 → B5 → B7~~（✅ 2026-10-21 完成，见变更记录）；~~B11 每日推送数量配额~~（✅ 2026-10-22 完成）；~~B12 GitHub 项目深度解读~~（✅ 2026-10-22 完成）；**B6 暂缓**（推送哪些概念用户还没想好）。

**B1. 🔍 历史归档与全文搜索——✅ 2026-10-21 完成**（用户问：过期周报怎么存？怎么搜？搜标题/简介/全文？）

- **存储方案**：日报/SoS/周报/论文解读维持现状按日期文件归档（`summaries/`、`out/sos/`、`out/papers/`），**不删除不过期**（日增 ~30 条，一年 <100MB）；周报按 ISO 周编号（`sos-week-YYYY-Www.md`）自然成序；远期量大可按年 gzip 归档冷数据。
- **统一索引**：feedback.db 新增 `archive_index` 表——每条历史条目一行（date / type[news|paper|sos|weekly|concept] / title / url / arxiv_id / summary / tags / source），新条目入库时写入，同时一次性回填历史文件。
- **搜索引擎选型（建议）**：**SQLite FTS5** —— 数据量万条级，毫秒响应，零额外服务（无需 Elasticsearch/MeiliSearch）；中文用 jieba 预分词后入 FTS5（unicode61 对中文分词无效），兜底方案 LIKE 模糊查询亦够快。
- **搜索范围（建议）**：默认 **标题 + 中文摘要（简介） + 标签**（正是浏览时用于决策的信息），论文解读正文与 SoS 全文作为第二阶段可选扩展；不建议盲搜全文（噪音多、命中率低）。
- **前端**：顶栏 🔍 搜索框 → 结果按日期倒序卡片展示（含 🎯/⭐ 徽章），论文条目可直达解读详情页，新闻直达原文。
- 落地：
  - 索引：`archive_index` 表（UNIQUE day+type+url 幂等）；`archive_from_feed`（每次 /api/feed 增量入库条目 + SoS 整篇，type=sos、summary 取正文前 300 字）+ `rebuild_archive`（启动幂等回填 summaries/*.md + out/sos/*.md，首跑 44 条：paper 37 / news 5 / sos 2）；论文顺带提取 arxiv_id 备用。
  - 搜索：本机 SQLite **未编译 FTS5 且无 jieba** → 按计划兜底方案用 **LIKE 子串匹配**（中文子串天然命中无需分词；多关键词空格分隔 = AND；`ESCAPE '\'` 转义 % _ \ 防注入；按 day DESC 限 50 条）；`GET /api/search?q=`（登录即可用，访客也能搜——B8 只读场景）。
  - 前端：顶栏 🔍 搜索框（Enter 触发）→ 动态「🔍 搜索 (N)」tab（插在 SoS 之后，load() 重建 tab 栏后自动补回）+ 搜索结果面板（复用卡片样式：type 图标 📄/📰/🧭 + 📅 日期 + source chip + 标签 chip + 标题命中词 `<mark>` 高亮，带 url 条目直达原文/解读）。
  - 实测：中文子串命中、AND 多词过滤、'SoS' 命中 2 条、通配符注入（`%_'`）安全无 crash；页面关键词与接口 curl 验证通过。

**B2. 👁 点击追踪 + ♻ 跨日去重（两项合并实现）——✅ 2026-10-21 完成**

- `clicks` 表记录点击（标题/原文/解读入口，date/anchor/url/title/clicked_at），前端埋点 POST `/api/click`。
- **去重规则（按用户口径，2026-10-22 简化）**：唯一标准是**管理员点没点过**——已点击 → 永不再推；推送过但未点击 → **一直可以重推**（带 🔄「上次推送于 X 日」徽章），不设次数上限、不设时间窗，直到点了为止。论文条目按 arxiv_id、新闻按 URL 归一化判定（B4 经典回看除外）。
- 实现位置：Horizon 采集后入 digest 前查 `archive_index`+`clicks` 过滤；顺带把点击数据反哺兴趣画像（点击过的论文标题关键词可加权）。
- 落地：
  - radar 侧：feedback.db 新增 `pushed`（每天 feed 被浏览时幂等落表，`INSERT OR IGNORE`，主键 day+anchor）+ `clicks`（埋点，UNIQUE day+anchor+role+kind，UPSERT 幂等）两表；`POST /api/click` 埋点端点**访客可调**（role 服务端由 cookie 判定，去重只看 admin 的点击）；api_feed 流程 = 落 pushed → `annotate_repush`（以前推过且 admin 未点 → 注入 `repush/last_pushed_day`）→ `filter_clicked`（展示层兜底移除 admin 已点条目，兜住"报告早生成、中午才点"时差）；URL 归一化 `url_norm()`（scheme/host 小写 + path 去尾斜杠，去 query/fragment）。
  - Horizon 侧：新增 `src/processing/seen.py`（只读打开 radar 库 `?mode=ro`，任何异常返回空态放行，绝不阻断推送；自带 arxiv 正则与 url_norm 等价实现），`select_digest_items` 在 filter_items 之后、twitter 扩展之前调用；规则（2026-10-22 简化）：admin 已点击 → 永久剔除（论文按 arxiv_id、新闻按 URL），未点击 → 一直保留可重推（原"推过 2 次未点剔除 + 新闻 7 天窗口"已按用户新口径移除）。
  - 前端：标题/📄 解读入口 sendBeacon 埋点（kind=link/paper，不阻塞跳转）；🔄 徽章（琥珀色，悬停看解释）；首页 CURRENT_DATE 供埋点用。
  - 实测：埋点→条目从信息流消失、UPSERT 幂等、guest 点击不影响 admin、repush 徽章注入、seen.py 六项规则单测全过；点击反哺兴趣画像的数据已落库，加权反哺待后续（B4 联动时一并考虑）。

**B3. 🧠 解读全局缓存 + 推送时预生成（修复"同一篇论文两次点击重复解读"）——✅ 2026-10-21 完成**

- 现状：缓存 key = `out/papers/{date}/{anchor}.md`——同日同篇秒开，但**同一篇论文跨日/跨板块重复收录时会重新解读**（用户实际遇到）；且为点击时才生成。
- 改造：① 缓存 key 改为 **arxiv_id 全局唯一**（`out/papers/{arxiv_id}.md` + 日期索引表），任何日期/板块命中即秒开；② **日报产出后后台批量预生成**当日全部论文解读（推送即保存，看时直接读本地）；③ 历史已解读直接复用。
- 落地：`migrate_old_cache()` 启动幂等迁移旧缓存（迁移 3 篇）；api_feed 解析时对未缓存论文 spawn 后台预生成（Semaphore 3 并发防 LLM 限流）；实测 12/12 paper_ready、/api/paper 缓存命中 0.1 秒秒回。

**B4. 🏆 经典论文回看（新论文 + 老牌重要论文混推）——✅ 2026-10-21 完成**

- 每日在论文板块末尾附 **1 篇经典**：按兴趣画像关键词（前 3，空则默认 5 词）检索高引论文，🏆 徽章 + 💡 一句话入选理由（LLM ≤60 字，失败兜底"被引 N 次（YYYY 年发表）"）。
- **数据源 OpenAlex**（免费无 key、限流宽松；原计划 Semantic Scholar 免费共享池实测持续 429 不可用，已弃用）：`cited_by_count:desc` per_page=50 × 多查询词合并候选池，去重 title、须带 arXiv 版（走 B3 解读缓存）+ 有摘要；abstract_inverted_index 倒排重组摘要，arXiv id 从 DOI/locations 双通道提取。
- **当天缓存**：`classics` 表（day 主键）当日多次触发结果固定；确定性选择 `random.Random(day)`（候选池前 10 抽 1）+ `_PICK_LOCK` 防并发；跨日去重 = classics 已推 arxiv_id ∪ admin 已点击 arxiv_id 不再推。
- 落地：`classic.py`（OpenAlex 检索 + LLM 理由）+ api_feed 注入论文板块末尾（失败不影响主流程）+ `/api/paper` classic- 前缀特判从 classics 表取条目；前端 🏆 金色徽章 + 💡 理由行。实测：SciPy 1.0（被引 38132，Nature Methods）入选，feed 注入 / 解读链路 / classics 缓存幂等全过。

**B5. 🛠 每日 GitHub 热门 AI 项目——✅ 2026-10-21 完成**

- **GitHub Search API**（免费无 key，搜索限流 10 req/min）：5 个 topic（ai/llm/agents/robotics/machine-learning）× `stars:>500` × `pushed:>近 30 天`（活跃维护），star 数降序合并候选池，未归档/有简介。
- 每日挑 **1 个**：`random.Random(day)` 从 star 前 10 确定性抽 1（同日固定），🛠 今日项目徽章 + ⭐ star 数 + LLM 一句话推荐理由（是做什么的/为什么火/适合谁，≤60 字，失败兜底"GitHub N 星热门项目"）。
- 落点：🔥热榜精选板块**置顶**；入 `pushed`（B2）+ `archive_index`（B1 可搜索）；跨日去重 = ghrepos 已推 full_name ∪ admin 已点 URL 不再推。
- 落地：`ghrepo.py`（镜像 classic.py 模式：ghrepos 表 day 主键当天缓存 + `_PICK_LOCK` 防并发 + 代理双通道）+ api_feed 注入 hot[0] + 前端 renderHotCard 绿色 🛠 徽章/简介/💡 理由行。实测：10-21 选 affaan-m/ECC（28.5 万星）、10-22 选 ollama/ollama（18.4 万星）——跨日不重复，缓存幂等 0 秒。

**B6. 📖 每日 AI 概念讲堂（从简单到难）——【暂缓：用户还没想好推送哪些概念】**

- 预设学习路径（示例，可增删）：RAG → Tool Use / Function Calling → Embedding 与向量检索 → Agent 基础 → MCP → OpenManus → Context Engineering → Agent Harness → Multi-Agent → RLHF…按序每日推进 1 个，`concept_progress` 表记进度不重推。
- 仿 paper_brief 模式自研 `concept_brief.py`：参考开源学习类 skill 裁剪（调研后再选型），LLM 生成结构化讲解（是什么 / 为什么重要 / 怎么用 / 一个例子 / 延伸阅读），反幻觉标注沿用。
- 前端：📖 今日概念板块 + 详情页 + 历史概念归档（入 B1 搜索索引）。

**B8. 👥 访客只读模式（朋友能看，不能改推荐）——✅ 2026-10-21 完成**

- **双口令分级**：`RADAR_ADMIN_KEY`（管理员，全功能：投票/偏好/兴趣/蒸馏）+ `RADAR_GUEST_KEY`（访客，只读：看资讯流/SoS/解读/搜索）。
- 实现：middleware 两把口令都能进门（cookie 存口令值）；**所有写 API**（/api/feedback、/api/prefs、/api/interests*）服务端校验 cookie 值 == ADMIN_KEY，访客调用返回 403（/api/click 埋点例外——B2 实施后访客也可上报点击，role 服务端判定，不影响 admin 去重）；`GET /api/me` 返回 role，前端 guest 隐藏 👍👎 与 ⚙偏好写控件。
- 登录页不变（一个口令框）；用户把访客口令发给朋友即可。
- 落地：5 个写端点 403 兜底 + `body.guest` CSS 隐藏投票区/偏好 tab；实测 admin 全功能、guest 写接口 403「访客只读：无权修改推荐」。

**B9. 🛡 安全加固（校园网暴露面防攻击）——✅ 2026-10-21 完成**

- **登录防爆破**：per-IP 失败计数（5 次失败锁 15 分钟），middleware 登录路径限速。
- **强口令**：admin 口令从 0000 换为强口令（deploy unit `RADAR_ADMIN_KEY` 环境变量），访客口令可简短。
- Cookie 加固：HttpOnly + SameSite=Lax + 30 天（沿用）；错误提示不区分"口令错/被锁"细节。
- 可选后续：fail2ban / ufw 限源网段（暂不做，防爆破已够校园网场景）。
- 落地：`_login_locked/_login_record_fail/_login_clear`（threading.Lock 防竞态）；实测连错 5 次后**正确口令也拒**且提示与口令错一致（不给爆破者探测信息）；双强口令在 radar-web.service 环境变量（不在 git 内）。

**B10. 📧 邮件附今日访问地址（点开即看）——✅ 2026-10-21 完成**

- 邮件顶部加横幅：`🌐 网页版：http://<局域网IP>:8080（访客口令 XXXX，校园网内可访问）`。
- IP 每次发信时动态获取（UDP connect 8.8.8.8 取本机出口 IP，应对 DHCP 变动）；**只附访客口令，不含管理员口令**（邮件可能被转发）；依赖 B8 的访客口令。
- 落地：Horizon `email.py` 新增 `_web_access_lines()`（纯文本版+markdown 版，HTML 转换后为可点击链接），`send_daily_summary` 顶部插入；访客口令从 Horizon .env 的 `RADAR_GUEST_KEY` 读取；实测生成 `http://<局域网IP>:8080` 可点击链接。

**B7. 原有缺口——✅ 2026-10-21 完成**

1. **邮件不含 SoS** → 单发一封 SoS 邮件（✅）：管道时序为 horizon-daily 08:00（生成日报并即时发日报邮件）→ sos-daily 08:40，日报邮件天然拿不到 SoS，故采用规格允许的"单发一封"：radar 新增 `mail.py`（复用 Horizon `data/config.json` email 段 + Horizon .env `EMAIL_PASSWORD` + `data/subscribers.json`，SMTP_SSL 465，主题/发件人 RFC2047 编码，Markdown 双版本正文，顶部附 B10 同款网页横幅——访客口令从 Horizon .env 随 env dict 读入）；`sos_daily.py` 生成成功后 hook 发信，**仅"目标为今天且文件是本次新建"时发**（历史日期重跑/当天重复生成均不刷邮件；周报同理），失败 print 不阻断退出码；实测测试邮件送达订阅邮箱。
2. **摘要风格 Profile 切换入口**（✅）：调研确认 DailySummarizer 为纯程序化渲染（无 LLM prompt），日报每条摘要的文风实际由 ai-news profile 的 `enrichment.md` 决定、评分 rubric `analysis.md` 决定选条——**只切 enrichment.md，analysis.md 等调优资产不碰**；新增 `digeststyle.py` 三预设（简洁快讯=默认原文 / 深度解读 3~4 句 / 轻松口语）+ 首次切换自动备份 `enrichment.base.md` 可逆（"我的调优版"仅当备份内容与预设不同才展示）；当前风格按文件内容实时匹配（手改文件也能正确显示）；`GET/POST /api/style`（POST 走 B8 admin 校验，访客 403）+ ⚙偏好面板顶部 🗞 日报风格按钮组；Horizon 管道每次运行重新加载 profile，切换后下一次日报（次日 08:00）生效。实测：切 deep→备份生成→切回 concise 与 git 原版逐字节一致，/api/feed 回归正常。

**B11. 📦 每日推送数量配额（⚙ 偏好面板可调）——✅ 2026-10-22 完成**（用户需求：设置里可以决定每个板块每天推送多少，如 GitHub 热门项目每天推几个、论文/经典论文每天推几篇）

- 三类配额：🛠 GitHub 热门项目（默认 1，1~10 个/天）、📜 经典论文（默认 1，1~10 篇/天）、📄 论文板块（默认 0=不限制，0~30 篇/天）。
- 存储：feedback.db 新增 `app_settings`（key-value，`quota_ghrepo`/`quota_classic`/`quota_papers`）；`get_quotas`（缺省回退默认 + 读时钳制）/`set_quotas`（写时钳制）。
- 多选改造：classic.py / ghrepo.py 的缓存表从 day 单主键迁移为 (day, paper_id) / (day, full_name) 复合主键（`PRAGMA table_info` 检测旧结构后 ALTER RENAME→CREATE→INSERT SELECT→DROP 幂等重建，历史推送记录保留用于跨日去重）；`pick_classic`→`pick_classics(day, n)`、`pick_ghrepo`→`pick_ghrepos(day, n)`——当天缓存 ≥n 直接返回前 n 条（rowid 序），不足时锁内 `random.Random(day).sample(pool[:10], k)` 补选（排除全部已推 + admin 已点），即**配额调大当天自动补选、调小只展示前 n 条**。
- api_feed 三处：① 论文板块截断——权重排序后每板块从前往后保留前 n 篇 arXiv 论文（0 不限制；非论文条目与顺序不动），被裁条目不预生成解读、不进热榜剔除文本；② B4 经典按配额循环 append 到论文板块末尾（anchor 含 arxiv_id 天然唯一）；③ B5 gh 多条——**anchor 从 `gh-{日期}` 改为 `gh-{full_name 转 slug}`**（同日多项目锚点各自唯一，避免 pushed 主键 / clicks 唯一键冲突），首选仍保持热榜置顶，record_pushed/annotate_repush/filter_clicked/archive 对整组生效。
- 接口/前端：`GET /api/quotas`（登录可读）+ `POST /api/quotas`（B8 admin 校验，超范围自动钳制）；⚙偏好面板日报风格框之后新增「📦 每日推送数量」框（三个数字输入 + 保存按钮；保存后 loadPrefs + 刷新 feed 即时生效）。
- 实测：GET 默认值 → POST 改 2/2 → 10-21 feed 经典 2 条（SciPy 1.0 + QUANTUM ESPRESSO，补选真实走 OpenAlex+LLM）+ gh 2 个（ECC + hermes-agent，补选真实走 GitHub API+LLM）；anchor slug 落 pushed 表无冲突；访客 POST 403「访客只读」；papers=99 钳到 30；papers=2 截断生效（日报论文留 2 篇、经典回看不占论文配额）；调小 → 只展示前 1 条；恢复默认 1/1/0。

**B12. 🛠 GitHub 项目深度解读（项目版解读 skill）——✅ 2026-10-22 完成**（用户需求：GitHub 板块要说明项目是做什么的、人们通常用它做什么、为什么好用、没有它的话人们用什么方案、解决什么问题；不知道怎么写就找/做一个解读 skill，生成类似论文解读的说明）

- 新增 `ghrepo_brief.py`（GitHub 项目解读 skill，与 paper_brief 论文解读同一套模式，每个项目 LLM 调用 ≤3 次）：① 免费抓 GitHub API `/repos/{full_name}/readme` 原文（无需 key；轻量清理徽章图片/HTML 标签/链接括号，截前 6000 字符；失败降级仅用官方简介）；② 固定七小节中文 Markdown——一句话总结 / 这个项目是做什么的 / 人们通常用它做什么 / 为什么好用 / **没有它的话人们通常用什么**（替代方案）/ 它解决了什么问题 / 值得注意的点；③ 写作规则适配项目类解读：允许使用开源生态公认可识（同类工具、替代方案，推理结论标 `[推断]`），但具体数字（star/版本/性能）只能来自材料（没有标 `[未明确给出]`），防编造。
- 缓存：`out/ghrepos/{full_name}.md`（全局唯一，同一项目跨日只解读一次；头部 `<!-- -->` 元信息 full_name/url/stars/language/description/generated_at）。
- webapp.py：GH_INFLIGHT/GH_ERRORS/GH_LOCK 状态 + `_gh_anchor` slug 助手 + `_start_gh_generation`/`_generate_gh_brief` 后台预生成（与论文解读共用 PAPER_SEM 并发上限防 LLM 限流）；api_feed B5 块条目带 `gh_ready`/`gh_brief_url`，未就绪推送即预生成（点开时多半已就绪）；`/ghrepo` 详情页（项目版 meta 卡：仓库标题链接/⭐star/语言/官方简介/生成时间/🛠 GitHub 仓库按钮，正文 markdown 渲染 + `[推断]`/`[未明确给出]` 标注，pending 轮询 2.5s）+ `GET /api/ghrepo`（缓存命中 ready / 后台生成 pending / 失败可 force 重试；按日期+锚点 slug 反查 ghrepos 表，跨日回看兜底全表匹配最近推送）。
- 前端：热榜 GitHub 卡片新增「🛠 解读」徽章链接（与 📄 论文解读同款 paper-link 样式；title 按 gh_ready 提示"已生成/点击生成"）。
- 实测：feed 条目带 gh_brief_url 且 gh_ready=false → 后台真实生成完成（journalctl `[GhBrief] affaan-m/ECC 生成完成`）；`/api/ghrepo` ready 返回七小节全齐解读（ECC：harness 概念解释/71 agent/311 技能/AgentShield 全部来自 README，无编造）；`/ghrepo` 页 200；访客可读（解读为浏览性质）；二次 feed gh_ready=true 缓存命中；错误分支「未找到该项目」正常。

### C. 可选增强（用户暂缓，做不做等指令）

- Docker 化收尾（Horizon/radar 容器化部署）
- newsnow 自托管前端（降级原则已定：自研前端即验收形态）
- 摘要风格 Profile 调优实验

### D. 环境依赖提醒

- 代理 clash（7897）停用 → TLDR/arXiv/Google News 等外部源自动空跑，本机源（:8001/:1200）与 HN 不受影响。
- 微信授权长期有效但可能失效 → we-mp-rss 网页（:8001）可重新扫码。

---

## 六、仓库文件地图

| 路径 | 是什么 |
|---|---|
| `app/Horizon/` | 主壳：采集/enrich/digest/邮件（上游开源项目 + 本项目配置） |
| `app/Horizon/data/config.json` | **信息源清单与配额都在这**（改源、改阈值、改配额） |
| `app/Horizon/data/summaries/` | 每日日报产物（horizon-YYYY-MM-DD-zh.md） |
| `app/Horizon/data/subscribers.json` | 邮件订阅者列表 |
| `app/Horizon/.env` | API 密钥/中转站/邮箱授权码（**含敏感信息，勿入库勿外发**） |
| `app/radar/` | 自研模块：sos_daily.py / webapp.py / distill.py / hotlist.py / paper_brief.py（论文解读 skill）/ ghrepo_brief.py（GitHub 项目解读 skill）/ interest.py（研究兴趣画像）/ static/index.html |
| `app/radar/data/feedback.db` | 调教数据（votes / 来源权重 / 关键词权重 / 用户关注与屏蔽词 / interest_papers / interest_keywords） |
| `app/we-mp-rss/` | 公众号转 RSS 自托管服务 |
| `config/profiles/` | ai-news / papers 两套 LLM prompt（改摘要风格在这） |
| `deploy/` | systemd unit 源文件（git 版本化；实际安装于 ~/.config/systemd/user/） |
| `out/` | 运行日志（horizon-daily.log 等）+ SoS 产物（out/sos/）+ 论文解读缓存（out/papers/）+ 项目解读缓存（out/ghrepos/） |
| `doc/` | report.md（需求规格）+ 本文档 |

---

## 七、常用命令

```bash
# 手动跑一次日报（长任务，--no-block 必加）
systemctl --user start --no-block horizon-daily.service

# 手动生成今日 SoS / 周报
cd ~/Myfile/AINews/app/radar && .venv/bin/python sos_daily.py
cd ~/Myfile/AINews/app/radar && .venv/bin/python sos_daily.py --weekly

# 手动跑蒸馏（votes≥5 才有产出）
cd ~/Myfile/AINews/app/radar && .venv/bin/python distill.py

# 状态与日志
systemctl --user list-timers
systemctl --user status radar-web.service
tail -n 50 ~/Myfile/AINews/out/horizon-daily.log

# 重启前端
systemctl --user restart radar-web.service
```

---

## 八、已知问题与对策（滚动更新）

| 问题 | 对策 |
|---|---|
| glm-5.3-flash 强制思考模型：enrich ~60s/条 × ~294 条远超 2h，08:00 run 曾被 `TimeoutStartSec=2h` SIGTERM | horizon-daily.service 改 `TimeoutStartSec=infinity`；radar 侧 `call_llm_robust` 兜底思考模型三坑（max_tokens 被推理占满 / 非流式 120s 超时 / thinking disabled 400 → 流式+16000） |
| QQ SMTP 拒收中文 From（550 "From header is missing or invalid"） | `src/services/email.py` 用 `Header` + `formataddr` 做 RFC2047 编码（From/Subject/退订回信三处），已实测通过 |
| app/Horizon/.env 两次被"未填写模板"覆盖（编辑器残留旧缓冲区被保存所致，已排查非程序行为） | 权威副本存于 `config/.env`，一条命令恢复：`cp config/.env app/Horizon/.env && chmod 600 app/Horizon/.env`；编辑器不要长期开着 .env |
| 新装 systemd timer `Persistent=true` 首次不补跑 | 无历史触发记录属正常；手动 `systemctl --user start sos-daily.service` 补当日 |
| 本机 127.0.0.1 源被 clash 劫持（httpx trust_env） | 所有涉及本机服务的 unit/脚本注入 `NO_PROXY=localhost,127.0.0.1`；radar 直连时 `trust_env=False` |
| rsshub.app 公共实例 403 | 自建 RSSHub（Docker，host 网络 + PROXY_URI 走 clash） |
| RSSHub HF daily-papers 503（DNS 污染 + Node 不读 HTTP_PROXY） | 容器加 `-e PROXY_URI=http://127.0.0.1:7897` |
| we-mp-rss 授权二维码无限加载 | playwright webkit 二进制缺失 → `playwright install webkit` |
| we-mp-rss 采集遇微信 200013 频率控制，单次快速重试 3 次即放弃导致当日条目为 0 | 增强抓取（2026-10-21）：主接口递增退避 120/240/360/480s 重试 4 次，耗尽自动降级 free_publish 通道（短退避 60/120s），web/app/api/free_publish 四文件；实测新重试已生效 |
| we-mp-rss 重复点授权提示"请勿重复运行" | 授权会话持有 `data/lock.lock` → 重启服务+删锁文件 |
| fish shell 不支持 heredoc/反引号 | 给用户的命令一律单行 |
| X 源依赖 Apify 付费 | 已弃用（enabled=false） |
| Google News 返回重定向链接（非原文直链） | 可接受，点击跳原文 |
| docker pull 直连超时 | 用全限定域名 `docker.m.daocloud.io/<image>` |

---

## 九、变更记录

- 2026-10-20：创建本文件；阶段 0 完成大半（网络复测、Horizon 检测、.env 模板、git 仓库）；等用户填 .env
- 2026-10-21：阶段 0 全部完成（uv 0.13.4、uv sync、A1）；阶段 1 执行项 1.1~1.5 完成（API 单次验证、config.json、自定义 ai-news profile、两轮调优产出首份中文日报、systemd 用户级 timer 08:00 CST）；关键发现：`ai.languages` 默认 en、threshold 7.0 过严、TLDR RSS 正确地址为 /api/rss/ai；A1/A2/A3/A6/A7 达标，A4/A5 待 2 天观察；唯一待办：linger 授权
- 2026-10-21（阶段 2）：linger 授权完成 ✅；arXiv 论文板块上线（cs.RO RSS + API 关键词 feed + papers profile，run3 验证 25 条中文摘要达标，46.5 万 tokens）；假 RSS 探测：机器之心/The Batch 官方 RSS 均 HTML、AIbase 404、smol.ai 404；run3 暴露论文挤掉新闻 → digest category_groups 配额修复；Docker 29.9.1 装好（用户执行，fish 兼容命令）；we-mp-rss 部署（uv+3.13 免 Docker，auth_web=True，playwright webkit 缺失致二维码 404 已修复重启）；RSSHub 容器启动（host 网络 + clash 分流）；机器之心 RSS route 待验证、微信扫码待用户
- 2026-10-21（阶段 2 续）：RSSHub 三 route 验证 200（PROXY_URI 修复 HF DNS 污染；正确路径 /aibase/daily、/deeplearning/the-batch、/huggingface/daily-papers；jiqizhixin 已被上游移除）并接入 Horizon config；Horizon RSSScraper 轻量实测 61 条采集成功（零 token）；we-mp-rss 常驻化 systemd 用户服务；外部日报 ≥2 达标；阶段 2 仅剩微信扫码 + 明早 08:00 timer 验收
- 2026-10-21（阶段 2 完成 ✅ + 阶段 3 启动）：微信扫码成功——一次授权订阅 **机器之心（MP_WXS_3073282833）+ 新智元（MP_WXS_3271041950）双号**（超公众号 ≥2 要求）；双号 RSS 接入 Horizon config（坑：路由前缀 /feed 单数；lock.lock 重复点授权会占锁，重启+删锁恢复）；**阶段 2 验收通过**：源覆盖=公众号 2 + arXiv 论文板块 + 外部日报 3（公众号条目受微信限频影响采集中，明早 timer 验证入报，A4/A5 连续观察）；阶段 3 详细计划写入（3.1~3.8），自研模块 app/radar/ 开工（SoS 综合三段式 + 反馈调权 SQLite + FastAPI 前端 :8080 + systemd 定时）
- 2026-10-21（阶段 3 ✅ + 阶段 4 ✅）：**app/radar/ 五件套建成并自验通过**（sos_daily.py 含思考模型流式兜底 call_llm_robust / webapp.py SQLite 调权 / distill.py votes→关键词蒸馏 / 深色单文件前端）；SoS 冒烟产出 sos-2026-10-20.md（三段式，真实 LLM）；webapp 闭环实测（feed 回退 + 👍+0.5/👎−1.0 + weights + 页面 200）；distill 5 votes→6 关键词落库；**systemd 三 unit 部署**（radar-web 常驻 :8080 / sos-daily 08:40 --wait-hours 4 错峰 / sos-weekly 周一 09:00，均 enable）；**邮件链路打通**：email.enabled=true + subscribers.json，首封 550（中文 From 未 RFC2047）→ 修复 src/services/email.py（Header/formataddr 三处）后测试邮件送达订阅邮箱；horizon-daily 已用新代码重启（11:11）确保今晚自动发报；Embodied-AI-Guide atom 接入 papers 源；horizon-daily TimeoutStartSec 2h→infinity（晨跑被杀根因）；待观察：A4/A5、SoS 首个定时产物、调教次日排序
- 2026-10-21（全链路首次端到端跑通 + 文档整理）：horizon-daily 手动重跑于 13:23 完成并**自动发送日报邮件成功**（本次 73.7 万 tokens）；SoS 同步产出 sos-2026-10-21.md（三段式质量达标，共识注明双源）；本文档重写为项目手册结构（现状/数据流/待办）
- 2026-10-21（P0~P4 体验升级 ✅）：**口令门禁**上线（0000，cookie 30 天，middleware 全站拦截）；radar-web 暴露 0.0.0.0 + ufw 8080，局域网直访实测通过；**🔥热榜精选**板块（hotlist.py 共享模块：aihot 官方 RSS + 本机 RSSHub 36kr 快讯，去重日报，webapp 附 hot 1~3 条）；**SoS 升级四段式**（新增 🧨破圈推荐：热榜挑 1~2 条 + 一句话理由，当日已重跑验证质量）；**关注词/屏蔽词前端化**（⚙偏好 tab + /api/prefs：关注 +8/词且标题高亮、屏蔽 −1000 沉底半透明）；**distill 定时器**（周日 21:00，Persistent）+ 增量水位门槛（distill_state.json）；修复投票事件重复绑定隐患；**enrich 并发 1→8**（实测 8 路零限流，全流程预计 5h→约 1h）；清理开发期测试投票/垃圾权重；.env 二次被模板覆盖已恢复（根源=编辑器旧缓冲区，已录入已知问题）
- 2026-10-21（📄 论文解读详情页 ✅）：参考两个开源论文分析 skill（flyer-Li/paper-analyst 的反幻觉标注 + Ayanami0730/paper-read-skill 的类型路由）裁剪自研 `paper_brief.py`：免费抓 arXiv abs 页 citation_* meta（标题/作者/官方摘要/学科，直连失败走代理）+ 每篇 **LLM ≤3 次调用**（非流式 4000 失败 → 流式 12000/16000 兜底）；webapp 新增 `GET /paper` 深色详情页（JS 轮询按需生成，md 轻渲染，标注词徽章化）+ `GET /api/paper`（缓存命中秒开，未命中后台线程生成，PAPER_INFLIGHT/ERRORS 防并发，force=1 重试）；前端论文条目加"📄 解读"入口（paper_ready 提示是否已生成）；缓存落盘 `out/papers/{date}/{anchor}.md`；实测：Ropedia W0 一次调用成功、HandsOn pending→ready 全链路 85 秒闭环，当日 12 篇论文条目全部带入口
- 2026-10-21（详情页完整信息卡 + 机器之心增强抓取 + 📚 研究兴趣画像 ✅）：
  ① **详情页完整信息卡**：paper_brief.py 抓取扩展 authors/subjects/abstract（citation meta）→ 缓存头部注释 → /api/paper meta 返回 → /paper 页顶部信息卡（作者"；"连接、原文链接 + PDF 直链 `/abs/`→`/pdf/` 前端转换）；实测 Ropedia W0 meta 全字段通过
  ② **机器之心抓不到根因与增强**：we-mp-rss 实际走 web.py（Web 浏览器模式），微信 appmsgpublish 接口 200013 频率控制、旧逻辑重试 3 次快速放弃 → web/app/api/free_publish 四文件增强：主接口 120/240/360/480s 递增退避重试 4 次，耗尽自动降级 `_fallback_to_free_publish` 共享会话备用通道（free_publish 内 200013 再做 60/120s 短退避）；手动触发全轮次实测：退避重试与自动降级、四端点依次尝试均按设计生效；当轮微信为全通道风控（free_publish/publish 返回非 JSON、appmsgpublish/appmsg 亦限频）最终 0 条——属微信侧现状，增强的价值在于扛过分钟级短暂限频后自动恢复
  ③ **📚 研究兴趣画像**：`app/radar/interest.py`（arXiv export API 链接解析 / `ti:"标题"` 检索 ≤5 候选 / LLM 提取 3~6 个英文研究方向关键词，max_tokens=4000 防推理模型挤占）+ feedback.db 新表 interest_papers / interest_keywords + webapp 四个 `/api/interests*` API + feed 排序兴趣词 +15/词（🎯 徽章）+ 前端偏好 tab 研究兴趣区块（输入 / 候选点选 / 列表 / pending 自动刷新）；全链路实测：喂入 Ropedia W0 → 6 关键词 → 7 篇相关论文加权、75 分居首
- 2026-10-21（阶段 5 规划录入）：用户集中提出 7 项新需求，方案建议写入「五、B」——B1 历史归档+SQLite FTS5 搜索（搜标题/摘要/标签）、B2 点击追踪+跨日去重（推送过且看过不再推）、B3 解读按 arxiv_id 全局缓存+推送预生成（修重复解读）、B4 经典论文回看（Semantic Scholar 高引）、B5 每日 GitHub 热门 AI 项目、B6 每日 AI 概念讲堂（易→难学习路径+学习 skill）、B7 原有缺口保留；待用户拍板优先级后实施
- 2026-10-21（阶段 5 开工 + 追加 3 项）：用户拍板 B6 暂缓、其余按 B3 → B9/B8/B10 → B2 → B1 → B4 → B5 顺序实施；追加 B8 访客只读模式（双口令分级：朋友能看不能改推荐）、B9 安全加固（校园网暴露面：登录防爆破+强口令）、B10 邮件附今日局域网访问地址（动态 IP+访客口令）
- 2026-10-21（首屏修复 + 口令简化）：修复首进网站 SoS 首屏空白——静态 HTML 的 `#panel-sos` 缺 `active` 类（CSS `.panel{display:none}`），首次加载只高亮 tab 未调用 showPanel，需点其他 tab 再点回才显示；补上 `panel active` 即修（FileResponse 实时生效）。按用户要求双口令简化（原强口令弃用，防爆破锁定仍生效；口令值不入 git）。
- 2026-10-21（B3/B8/B9/B10 ✅）：**B3** 解读全局缓存（key=arxiv_id + 旧缓存幂等迁移 3 篇 + api_feed 推送时后台预生成，Semaphore 3 限流）12/12 paper_ready、缓存命中 0.1 秒秒回；**B8** 双口令分级（/api/me 返回 role、5 个写端点 403「访客只读」、前端 guest 隐藏 👍👎 与 ⚙偏好）；**B9** 登录防爆破（per-IP 5 次失败锁 15 分钟，锁定提示与口令错一致防探测）+ 强双口令；**B10** 邮件顶部访问地址横幅（UDP 动态取局域网 IP、HTML 可点击链接、只附访客口令，口令从 Horizon .env 读取）；curl 全链路验证通过。⚠️ 口令已更换：旧 0000 失效需重新登录，管理员/访客口令见 radar-web.service 与 Horizon .env（不入 git）
- 2026-10-21（B2 ✅ 点击追踪 + 跨日去重）：radar 新增 `pushed`/`clicks` 两表 + `POST /api/click` 埋点（访客可上报，role 服务端判定，去重只看 admin）+ api_feed 落推送记录/🔄重推徽章注入/已点条目展示层兜底过滤；Horizon 新增 `src/processing/seen.py`（只读 radar 库，异常全放行不阻断推送）接入 `select_digest_items`——admin 已点永不再推、推 2 次未点不再推、新闻超 7 天未点不再推（论文无时间窗，按 arxiv_id）；前端标题/解读入口 sendBeacon 埋点 + 🔄「上次推送于 X」徽章。实测：埋点→条目消失、UPSERT 幂等、guest 不影响 admin、六项去重规则单测全过。B1→B4→B5→B7 待做。
- 2026-10-21（B1 ✅ 历史归档 + 搜索）：feedback.db 新增 `archive_index` 表；`rebuild_archive` 启动幂等回填 44 条（paper 37/news 5/sos 2）+ `archive_from_feed` 每日增量入库；搜索按计划兜底用 LIKE 子串匹配（FTS5/jieba 不可用；中文子串天然命中、多词 AND、转义防注入），`GET /api/search`；前端顶栏 🔍 搜索框（Enter）→ 动态搜索 tab + 结果面板（类型图标/日期/source/标签 chip/命中词高亮，可直达原文）。实测 4 项搜索用例 + 页面 curl 全过。下一个：B4。
- 2026-10-21（B4 ✅ 经典论文回看）：新增 `classic.py`，每日 1 篇高引经典附论文板块末尾。⚠️ 选型变更：Semantic Scholar 免费共享池实测持续 429（无 API key 不可用），**改用 OpenAlex**（免费无 key、限流宽松）；按兴趣关键词（前 3）× cited_by_count 排序取候选，须带 arXiv 版 + 有摘要，`random.Random(day)` 确定性抽 1 + classics 表当天缓存 + admin 已点/已推 arxiv_id 跨日去重；🏆 徽章 + 💡 LLM 一句话理由（兜底"被引 N 次"）；`/api/paper` classic- 前缀特判 + B3 解读缓存复用。实测 SciPy 1.0（38132 引）全链路通过。下一个：B5。
- 2026-10-21（B5 ✅ 每日 GitHub 热门项目）：新增 `ghrepo.py`，GitHub Search API 免费无 key——5 topic × stars>500 × pushed 近 30 天，star 降序合并候选，`random.Random(day)` 前 10 确定性抽 1；ghrepos 表当天缓存 + 已推 full_name/admin 已点 URL 跨日去重；🛠 今日项目置顶热榜精选（⭐ star + 💡 LLM 理由），入 pushed/archive_index 走 B2/B1。实测 10-21 affaan-m/ECC（28.5 万星）→ 10-22 ollama/ollama（18.4 万星）不重复，缓存幂等。下一个：B7。
- 2026-10-22（B7 ✅ SoS 邮件 + 日报风格切换）：**B7-1** radar 新增 `mail.py` 单发一封 SoS 邮件（复用 Horizon email 配置/EMAIL_PASSWORD/subscribers.json，RFC2047 中文编码，MD 纯文本+HTML 双版本，顶部 B10 同款网页横幅），sos_daily.py 日/周报生成成功后 hook——仅"目标为今天且文件本次新建"时发，失败不阻断；测试邮件实测送达。**B7-2** 调研确认日报文风载体是 ai-news profile 的 enrichment.md（DailySummarizer 纯渲染；analysis.md 评分 rubric 不碰）：新增 `digeststyle.py` 三预设 + 首次切换自动备份 enrichment.base.md 可逆 + 当前风格按内容实时匹配，`GET/POST /api/style`（admin 校验）+ ⚙偏好面板 🗞 日报风格按钮组，次日 08:00 日报生效；实测切 deep→切回 concise 与 git 原版逐字节一致。B 部分仅剩 B6（暂缓）。
- 2026-10-22（B2 规则简化 ✅ 用户口径更新）：跨日去重改为**只看管理员点没点过**——已点击 → 永不再推；未点击 → 一直可重推（🔄 徽章照旧）。移除 seen.py 的"推过 2 次未点不再推"次数上限与"新闻首推 7 天未点过期"时间窗（`filter_seen` 签名简化为二元组，orchestrator 调用点同步；pushed 表不再参与过滤，仅用于徽章与归档索引）。radar 展示层 filter_clicked / annotate_repush 本就无次数限制，无需改动。
- 2026-10-22（B11 ✅ 每日推送数量配额）：用户提出希望在设置中决定每类内容每天推送多少。feedback.db 新增 `app_settings` 表（quota_ghrepo/quota_classic/quota_papers，默认 1/1/0=不限制，区间 1~10/1~10/0~30 读写双向钳制）；classic.py/ghrepo.py 缓存表幂等迁移复合主键（历史推送保留）+ `pick_*s(day, n)` 多选——缓存不足当天锁内补选（配额调大当天生效、调小只展示前 n 条）；api_feed 论文板块按权重截断 + B4/B5 循环多条（gh anchor 从 gh-{日期} 改 full_name slug 防主键冲突，首选保持热榜置顶）；`GET/POST /api/quotas`（POST 走 B8 admin 校验）+ ⚙偏好面板「📦 每日推送数量」框（保存即时刷新 feed）。实测：改 2/2 当天补选真实跑通（经典 2 篇 + GitHub 2 个，OpenAlex/GitHub API + LLM 理由全走通）、访客 403、99→30 钳制、papers=2 截断正确（经典回看不占论文配额）、调小 LIMIT 生效、恢复默认。
- 2026-10-22（B12 ✅ GitHub 项目深度解读）：用户希望 GitHub 板块附带项目深度说明（是做什么的/怎么用/为什么好用/没有它用什么/解决什么问题），没有现成说明就做解读 skill 生成。新增 `ghrepo_brief.py`（GitHub API 免费 README + 七小节固定格式 LLM 解读，项目类反幻觉规则：生态通识可写但标 [推断]、数字只来自材料；每个项目 ≤3 次调用），缓存 `out/ghrepos/{full_name}.md` 跨日复用；webapp.py 后台预生成（推送即生成，与论文共用 LLM 并发闸）+ `/ghrepo` 详情页 + `GET /api/ghrepo`（pending 轮询/force 重试）；热榜卡片「🛠 解读」入口。实测 ECC 全链路通过（七小节齐全、数字均来自 README）。
