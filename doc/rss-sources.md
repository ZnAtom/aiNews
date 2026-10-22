# 📡 推荐信息源清单（全部实测）

> 所有 RSS 均于 **2026-10-22** 在国内网络环境实测验证，标注：
> - ✅ **直连**：无需代理即可抓取
> - 🪜 **需代理**：直连超时，走代理正常 —— 在 `app/Horizon/.env` 配好 `HTTPS_PROXY` 后由 Horizon 自动处理
>
> 不想逐个挑？**保持默认 10 源即可用**，本清单用于按自己兴趣加菜。

## 怎么接入

编辑 `app/Horizon/data/config.json`（首次部署时从 `config/config.example.json` 复制而来），
在 `sources.rss` 数组里追加一条：

```json
{
  "name": "TechCrunch AI",
  "url": "https://techcrunch.com/category/artificial-intelligence/feed/",
  "enabled": true,
  "category": "ai-news",
  "profile": "ai-news"
}
```

| 字段 | 说明 |
|---|---|
| `name` | 显示用名称，随意 |
| `url` | RSS 地址 |
| `enabled` | `false` 可临时停用而不删除 |
| `category` | 归属板块：`ai-news`（📰 AI 资讯）/ `papers`（📄 论文） |
| `profile` | 分析模板：`ai-news` / `papers`，一般与 `category` 同名 |

保存后**次日 08:00 日报自动生效**（定时任务每次运行都重新读取配置，无需重启）。

---

## 📰 综合资讯（国际）

| 源 | RSS 地址 | 说明 | 网络 |
|---|---|---|---|
| Hacker News 最佳 | `https://hnrss.org/best?points=100` | 100+ 赞的技术热帖，兼有 AI 与工程 | ✅ 直连 |
| Hacker News 首页 | `https://hnrss.org/frontpage` | 实时首页，噪声比"最佳"略高 | ✅ 直连 |
| Hacker News AI 关键词 | `https://hnrss.org/newest?q=AI` | HN 全站按关键词检索（模式：`?q=任意词`，多词用 + 连接如 `q=AI+robot`） | ✅ 直连 |
| Hacker News LLM 关键词 | `https://hnrss.org/newest?q=LLM` | 同上，专盯 LLM 讨论 | ✅ 直连 |
| TechCrunch AI | `https://techcrunch.com/category/artificial-intelligence/feed/` | 创投视角，融资/发布快讯多 | ✅ 直连 |
| The Verge AI | `https://www.theverge.com/rss/ai-artificial-intelligence/index.xml` | 消费级 AI 产品与行业报道 | ✅ 直连 |
| Ars Technica AI | `https://arstechnica.com/ai/feed/` | 技术深度报道 | ✅ 直连 |
| Wired AI | `https://www.wired.com/feed/tag/ai/latest/rss` | AI 与社会/政策交叉 | ✅ 直连 |
| MIT Technology Review AI | `https://www.technologyreview.com/topic/artificial-intelligence/feed` | 质量高，更新不算勤 | ✅ 直连 |
| GitHub Blog | `https://github.blog/feed/` | GitHub 官方动态（Copilot 等） | ✅ 直连 |

## 🏛 研究机构博客

| 源 | RSS 地址 | 说明 | 网络 |
|---|---|---|---|
| OpenAI News | `https://openai.com/news/rss.xml` | 官方发布，一有就是大事 | ✅ 直连 |
| Google DeepMind | `https://deepmind.google/blog/rss.xml` | Gemini / Alpha 系列官方动态 | ✅ 直连 |
| Simon Willison | `https://simonwillison.net/atom/everything/` | LLM 应用一线实践者博客，信息密度极高 | ✅ 直连 |
| Google Research | `https://research.google/blog/rss/` | 谷歌研究博客 | 🪜 需代理 |
| Microsoft Research | `https://www.microsoft.com/en-us/research/feed/` | 微软研究院 | 🪜 需代理 |
| Hugging Face Blog | `https://huggingface.co/blog/feed.xml` | 开源模型生态第一线 | 🪜 需代理 |
| Meta Engineering | `https://engineering.fb.com/feed/` | Meta 工程博客（⚠ 与避坑名单里的 ai.meta.com 不同站，此域名直连可用） | ✅ 直连 |

## 📄 论文

| 源 | RSS 地址 | 说明 | 网络 |
|---|---|---|---|
| arXiv cs.CL | `https://rss.arxiv.org/rss/cs.CL` | 计算语言学 / NLP | ✅ 直连* |
| arXiv cs.AI | `https://rss.arxiv.org/rss/cs.AI` | 人工智能综合 | ✅ 直连* |
| arXiv cs.LG | `https://rss.arxiv.org/rss/cs.LG` | 机器学习 | ✅ 直连* |
| arXiv cs.CV | `https://rss.arxiv.org/rss/cs.CV` | 计算机视觉 | ✅ 直连* |
| arXiv cs.RO | `https://rss.arxiv.org/rss/cs.RO` | 机器人学（**默认已接入**） | ✅ 直连* |

\* arXiv 官方 RSS 偶发超时，属正常现象，重试即可；量大时建议靠 `digest` 的板块配额与 LLM 打分过滤。

## 🤖 具身智能 / 机器人

| 源 | RSS 地址 | 说明 | 网络 |
|---|---|---|---|
| IEEE Spectrum Robotics | `https://spectrum.ieee.org/feeds/topic/robotics.rss` | 机器人行业标杆媒体 | ✅ 直连 |
| IEEE Spectrum AI | `https://spectrum.ieee.org/feeds/topic/artificial-intelligence.rss` | 同站 AI 频道 | ✅ 直连 |
| Robohub | `https://robohub.org/feed/` | 学术界机器人动态 | 🪜 需代理 |
| Embodied-AI-Guide 更新 | `https://github.com/TianxingChen/Embodied-AI-Guide/commits/main.atom` | 中文具身智能资料库（**默认已接入**） | ✅ 直连 |

## 🛠 AI 工具版本追踪（GitHub releases 模式）

GitHub 为每个仓库自带 releases.atom —— **把任意仓库变成 RSS**：`https://github.com/{owner}/{repo}/releases.atom`。
以下为 AI 领域高频更新、值得盯版本的工具（2026-10-22 实测全部直连可用）：

| 源 | RSS 地址 | 说明 | 网络 |
|---|---|---|---|
| Claude Code | `https://github.com/anthropics/claude-code/releases.atom` | Anthropic 官方 CLI | ✅ 直连 |
| OpenAI Codex | `https://github.com/openai/codex/releases.atom` | OpenAI 官方 CLI | ✅ 直连 |
| Gemini CLI | `https://github.com/google-gemini/gemini-cli/releases.atom` | Google 官方 CLI | ✅ 直连 |
| MCP Servers | `https://github.com/modelcontextprotocol/servers/releases.atom` | MCP 官方服务器集 | ✅ 直连 |
| LangChain | `https://github.com/langchain-ai/langchain/releases.atom` | Agent 框架标杆 | ✅ 直连 |

> 同理可追任意项目；要盯代码流可用 `/{owner}/{repo}/commits/{branch}.atom`（Embodied-AI-Guide 源即此模式）。

## 🇨🇳 中文资讯

| 源 | RSS 地址 | 说明 | 网络 |
|---|---|---|---|
| 机器之心 | `https://www.jiqizhixin.com/rss` | 官网 RSS（**默认已接入公众号版**） | ✅ 直连 |
| 量子位 | `https://www.qbitai.com/feed` | 官网 RSS，更新快 | ✅ 直连 |
| 爱范儿 | `https://www.ifanr.com/feed` | 消费科技，AI 报道不少 | ✅ 直连 |
| TLDR AI（中文读者友好） | `https://tldr.tech/api/rss/ai` | 英文短摘要日报（**默认已接入**） | ✅ 直连 |
| 阮一峰科技爱好者周刊 | `https://www.ruanyifeng.com/blog/atom.xml` | 中文圈最有影响力的技术周刊 | ✅ 直连 |
| IT之家 | `https://www.ithome.com/rss/` | 国内科技快讯，AI 报道量大 | ✅ 直连 |
| 少数派 | `https://sspai.com/feed` | 效率工具/数字生活，偏产品向 | ✅ 直连 |

## 💌 Newsletter / 深度专栏

| 源 | RSS 地址 | 说明 | 网络 |
|---|---|---|---|
| The Batch | 自建 RSSHub `/deeplearning/the-batch` | 吴恩达周刊（**默认已接入**） | 本地 |
| Latent Space | `https://www.latent.space/feed` | AI 工程师周刊 + 深访 | ✅ 直连 |
| Interconnects | `https://www.interconnects.ai/feed` | Nathan Lambert，开源模型/RLHF 深度分析 | ✅ 直连 |
| Ahead of AI | `https://magazine.sebastianraschka.com/feed` | Sebastian Raschka，机器学习前沿解读 | ✅ 直连 |
| Import AI | `https://importai.substack.com/feed` | Jack Clark（Anthropic），周更深度 | 🪜 需代理 |

## 🌐 社区 / 热榜

| 源 | RSS 地址 | 说明 | 网络 |
|---|---|---|---|
| Reddit r/MachineLearning | `https://www.reddit.com/r/MachineLearning/.rss` | 学术讨论为主 | 🪜 需代理 |
| Reddit r/LocalLLaMA | `https://www.reddit.com/r/LocalLLaMA/.rss` | 本地部署/开源模型社区 | 🪜 需代理 |
| Reddit r/robotics | `https://www.reddit.com/r/robotics/.rss` | 机器人社区 | 🪜 需代理 |
| LinuxDo | `https://linux.do/latest.rss` | 中文技术社区（Discourse 官方 RSS） | 🪜 需代理 |
| V2EX | `https://www.v2ex.com/index.xml` | 中文程序员社区全站 | 🪜 需代理 |
| GitHub Trending | 自建 RSSHub `/github/trending/daily/any` | 日榜（已用专属抓取器，无需配置） | 本地 |

## 自建 RSSHub 补充路由

已自建 RSSHub（`http://127.0.0.1:1200`）可解锁更多源，常用路由：

| 路由 | 内容 |
|---|---|
| `/aibase/daily` | AIbase 日报（**默认已接入**） |
| `/huggingface/daily-papers` | HF 每日精选论文（**默认已接入**） |
| `/deeplearning/the-batch` | The Batch（**默认已接入**） |
| `/github/trending/daily/any` | GitHub 趋势日榜（实测本机 503 不稳；热门项目已有专板，不必再接） |
| `/36kr/newsflashes` | 36氪快讯 |
| `/juejin/trending/all/weekly` | 掘金周榜（实测 20 条可用） |

更多路由见 [RSSHub 官方文档](https://docs.rsshub.app/)，路由可用性以文档为准。

## 微信公众号（we-mp-rss 渠道）

公众号源走自建 we-mp-rss（`http://127.0.0.1:8001`）：在其后台扫码订阅公众号后，
把生成的 `feed/MP_WXS_xxx.rss` 地址按上文格式填入即可。推荐订阅：

| 公众号 | 定位 |
|---|---|
| 机器之心（**已接入**） | 学术 + 产业综合 |
| 新智元（**已接入**） | 快讯向，量大 |
| 量子位 | 快讯向 |
| PaperWeekly | 论文推荐 |
| AI 前线 | 工程向 |
| 夕小瑶科技说 | 算法工程 |
| 具身智能之心 | 具身智能垂直 |

## 🔎 知名 RSS 索引与发现渠道

想自己找更多源？按靠谱程度排序（2026-10-22 核查）：

| 索引 / 工具 | 地址 | 说明 |
|---|---|---|
| awesome-rsshub-routes | github.com/JackyST0/awesome-rsshub-routes | 社区精选的 RSSHub 路由 + 官方源清单，按 AI/社区/安全/Newsletter 分类，本文档部分新源即出自这里 |
| ALL-about-RSS | github.com/AboutRSS/ALL-about-RSS | RSS 百科全书：阅读器/工具/索引/教程最全合集 |
| RSSHub 官方文档 | docs.rsshub.app | 路由查询权威来源，配合自建实例解锁各站点 |
| RSSHub Radar 扩展 | github.com/DIYgod/RSSHub-Radar | 浏览器插件：浏览任意网页时一键发现该站 RSS / RSSHub 路由 |
| Folo | folo.is | RSSHub 团队的新一代 AI 阅读器（39k+ ⭐），内置订阅发现 |
| 少数派 RSS 指南 | sspai.com/post/56391 | 中文 RSS 入门经典教程 |
| Feedspot 榜单 | feedspot.com | 商业目录，有 "Top 100 AI Blogs" 等主题榜单，挑源参考 |
| feedx.net | feedx.net | 主流外媒 RSS 镜像，国内直连友好 |

## ⚠️ 避坑名单（实测不可用）

| 源 | 结果 | 原因 |
|---|---|---|
| Anthropic News | 404 | 官方未提供 RSS |
| Meta AI Blog | 超时/400 | feed 不可用 |
| VentureBeat AI | 429 | 反爬限流严格，抓取器会被拒 |
| The Robot Report | 403 | 反爬，feedparser 无法访问 |
| Google AI Blog 旧路径 | 双通道超时 | `blog.google/technology/ai/rss/` 直连与代理均超时；谷歌动态用上文 Google Research / DeepMind 源即可 |
| 知乎热榜 | RSSHub 404 | `/zhihu/hotlist` 路由已移除/改名 |
| B 站排行榜 / UP 主投稿 | 301 / 503 | RSSHub `/bilibili/*` 需 cookie 或上游风控，对本雷达价值有限未深究 |

> 这些站点的动态可通过 Google News 关键词订阅兜底：
> `sources.google_news.query` 里加 `VentureBeat AI` 等词即可。
