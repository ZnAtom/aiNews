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

## 🇨🇳 中文资讯

| 源 | RSS 地址 | 说明 | 网络 |
|---|---|---|---|
| 机器之心 | `https://www.jiqizhixin.com/rss` | 官网 RSS（**默认已接入公众号版**） | ✅ 直连 |
| 量子位 | `https://www.qbitai.com/feed` | 官网 RSS，更新快 | ✅ 直连 |
| 爱范儿 | `https://www.ifanr.com/feed` | 消费科技，AI 报道不少 | ✅ 直连 |
| TLDR AI（中文读者友好） | `https://tldr.tech/api/rss/ai` | 英文短摘要日报（**默认已接入**） | ✅ 直连 |

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
| GitHub Trending | 自建 RSSHub `/github/trending/daily/any` | 日榜（已用专属抓取器，无需配置） | 本地 |

## 自建 RSSHub 补充路由

已自建 RSSHub（`http://127.0.0.1:1200`）可解锁更多源，常用路由：

| 路由 | 内容 |
|---|---|
| `/aibase/daily` | AIbase 日报（**默认已接入**） |
| `/huggingface/daily-papers` | HF 每日精选论文（**默认已接入**） |
| `/deeplearning/the-batch` | The Batch（**默认已接入**） |
| `/github/trending/daily/any` | GitHub 趋势日榜 |
| `/36kr/newsflashes` | 36氪快讯 |

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

## ⚠️ 避坑名单（实测不可用）

| 源 | 结果 | 原因 |
|---|---|---|
| Anthropic News | 404 | 官方未提供 RSS |
| Meta AI Blog | 超时/400 | feed 不可用 |
| VentureBeat AI | 429 | 反爬限流严格，抓取器会被拒 |
| The Robot Report | 403 | 反爬，feedparser 无法访问 |

> 这些站点的动态可通过 Google News 关键词订阅兜底：
> `sources.google_news.query` 里加 `VentureBeat AI` 等词即可。
