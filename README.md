<div align="center">

# 📡 AI 资讯雷达 · aiNews

**每天早上 8 点，一份自动采集、AI 逐条摘要、可个性化调教的中文 AI / 具身智能日报，
准时送达你的邮箱和浏览器 —— 全流程无人值守，软件订阅费 ¥0。**

![Python](https://img.shields.io/badge/Python-3.12+-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-Web%20门户-009688?logo=fastapi&logoColor=white)
![uv](https://img.shields.io/badge/uv-依赖管理-DE5FE9?logo=astral&logoColor=white)
![systemd](https://img.shields.io/badge/systemd-用户级定时-4A154B)
![LLM](https://img.shields.io/badge/LLM-OpenAI%20兼容接口-412991?logo=openai&logoColor=white)
![Platform](https://img.shields.io/badge/平台-Linux%20%7C%20自托管-FCC624?logo=linux&logoColor=black)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

</div>

---

## 📖 这是什么

**AI 资讯雷达**是一套完全跑在自己电脑/服务器上的个人 AI 情报系统：

- **多源自动采集** —— Hacker News、TLDR AI、Google News、arXiv（机器人 + 关键词）、
  Hugging Face Daily Papers、AIbase 日报、The Batch、微信公众号（机器之心/新智元）等 10+ 信息源；
- **LLM 逐条加工** —— 相关性打分过滤噪音，每条压缩成 **1~2 句中文摘要 + 原文链接**；
- **🧠 SoS 独家综合** —— 跨源交叉印证生成四段式日报：今日要点 / 共识与分歧 / 值得关注 / 破圈推荐；
- **🎛 越用越懂你** —— 每条新闻可 👍👎，即时调整来源权重与关键词权重，周日自动"蒸馏"历史投票；
- **双通道推送** —— 🌐 深色系 Web 门户（局域网可访问）+ 📧 邮件全文日报，回复邮件即可订阅/退订。

> 命名灵感：像雷达一样 360° 扫描信息面，只把高价值目标标出来推给你。

<div align="center">
<img src="doc/img/screenshot-portal.png" alt="深色 Web 门户：SoS 综合速览 + 板块信息流" width="820">
<br/>
<img src="doc/img/screenshot-brief.png" alt="论文深度解读详情页" width="820">
</div>

## ✨ 核心特性

| 特性 | 说明 |
|---|---|
| 🌍 **多源聚合** | RSS / API / 微信公众号 / 自建 RSSHub，统一进一条流水线 |
| 🇨🇳 **中文摘要** | LLM 打分 + 逐条 1~2 句中文摘要，30 秒掌握全天动态 |
| 🧠 **SoS 综合** | 不只是罗列：跨源观点比对，标注"哪些源都提到、哪些有分歧" |
| 🎛 **调教闭环** | 👍 → 来源权重 +0.5；👎 → −1.0；关注词/屏蔽词面板即时生效 |
| 🧲 **偏好蒸馏** | 周日自动把投票记录蒸馏成关键词权重，LLM 复盘你的口味 |
| 📄 **论文解读** | arXiv 条目一键生成六小节深度解读（在做什么/方法/技术/效果，带反幻觉标注） |
| 🛠 **项目解读** | GitHub 热门项目一键生成七小节解读（做什么/怎么用/为什么好/替代方案） |
| 📧 **邮件推送** | Markdown→HTML 全文日报，顶部附网页版地址，访客口令即可查看 |
| 🔥 **热榜精选** | GitHub Trending / 36kr 快讯等热榜条目单独成板块 |
| ⚙️ **配额与风格** | 每个板块每天推几条可调；日报文风三预设一键切换 |
| 🔐 **双口令门禁** | 管理员（可改配置）/ 访客（只读），口令只存环境变量，绝不入库 |
| 📡 **RSS 输出** | `/rss?key=口令` 直连，在任意 RSS 阅读器里订阅自己的雷达（只读、不占推送去重） |

## 🤔 没有它，你平时怎么做？

| 手动信息流 | 用 AI 资讯雷达 |
|---|---|
| 每天刷 HN、Twitter、公众号、arXiv，东看西漏 | 10+ 源定时自动采集，一次看全 |
| 英文长文读不完，索性收藏吃灰 | 每条 1~2 句中文摘要，感兴趣的再点原文 |
| 信息过载，真新闻淹没在流水里 | LLM 相关性打分 + 板块配额，只留高价值条目 |
| 各说各话，难以分辨"是真火还是营销" | SoS 综合交叉印证，共识/分歧一目了然 |
| 想看的领域变了，得手动改订阅 | 👍👎 即时调权，偏好自动蒸馏跟进 |
| 论文只有标题摘要，看不懂在干嘛 | 一键生成六小节解读，含反幻觉标注 |

## 🏗 系统架构

```mermaid
flowchart LR
    subgraph SRC["信息源"]
        direction TB
        S1["Hacker News / TLDR AI / Google News"]
        S2["arXiv cs.RO + 关键词 / HF Papers"]
        S3["RSSHub：AIbase / The Batch"]
        S4["we-mp-rss：机器之心 / 新智元"]
    end

    subgraph HZ["Horizon 采集引擎（上游）"]
        direction TB
        E1["抓取 → LLM 打分过滤 → 中文逐条摘要"]
        E2["📝 中文日报 Markdown"]
        E3["📧 日报邮件"]
    end

    subgraph RD["radar 自研模块"]
        direction TB
        R1["🧠 SoS 综合（今日 / 每周）"]
        R2["🌐 Web 门户 :8080"]
        R3["👍👎 反馈调权 → 🧲 蒸馏"]
        R4["📄 论文解读 / 🛠 项目解读"]
    end

    SRC --> E1 --> E2 --> E3
    E2 --> R1 --> R2
    E1 --> R2
    R3 --> R1
    R2 --> R4
```

- **app/Horizon** —— [Horizon](https://github.com/Thysrael/Horizon)（上游开源项目，克隆使用）：多源抓取、LLM 打分、
  中文日报生成、邮件发送、订阅管理，通过自定义 profile 完成调优；
- **app/radar** —— 本仓库自研模块（FastAPI）：
  [sos_daily.py](app/radar/sos_daily.py) SoS 综合 ｜ [webapp.py](app/radar/webapp.py) Web 门户与调教 API ｜
  [distill.py](app/radar/distill.py) 偏好蒸馏 ｜ [hotlist.py](app/radar/hotlist.py) 热榜 ｜
  [paper_brief.py](app/radar/paper_brief.py) 论文解读 ｜ [ghrepo_brief.py](app/radar/ghrepo_brief.py) 项目解读 ｜
  [mail.py](app/radar/mail.py) SoS 邮件 ｜ [interest.py](app/radar/interest.py) 研究兴趣画像；
- **config/** —— Horizon 配置样例与两套 LLM 调优 profile（ai-news / papers）；
- **deploy/** —— systemd 用户级 service/timer，一行命令装好定时任务。

## 📅 每日自动时间线

| 时间 | 任务 | 由谁执行 |
|---|---|---|
| 08:00 | 全源采集 + LLM 打分摘要 → 日报 → 自动发邮件 | `horizon-daily.timer` |
| 08:40 | 等日报就绪 → 生成 SoS 今日综合 → 发 SoS 邮件 | `sos-daily.timer` |
| 常驻 | Web 门户 + 调教 API + 解读页按需生成 | `radar-web.service` |
| 周一 09:00 | SoS 周报 | `sos-weekly.timer` |
| 周日 21:00 | 👍👎 投票蒸馏成关键词权重 | `radar-distill.timer` |

## 🚀 部署方式

### 0. 前置依赖

- Linux（用了 systemd 用户级定时任务）+ [uv](https://docs.astral.sh/uv/)；
- 一个 OpenAI 兼容的 LLM API（中转站或官方均可，`base_url` 可配）；
- 可选：Docker（跑自建 RSSHub）、QQ 邮箱 SMTP 授权码（邮件推送）。

### 1. 获取代码

```bash
git clone https://github.com/ZnAtom/aiNews.git Myfile/AINews
cd Myfile/AINews
```

### 2. 配置 Horizon（采集引擎）

```bash
git clone https://github.com/Thysrael/Horizon app/Horizon
cp config/config.example.json app/Horizon/data/config.json
cp config/env.example       app/Horizon/.env
```

编辑 `app/Horizon/.env` 填入三项必填：`OPENAI_API_KEY`、`OPENAI_BASE_URL`、`HORIZON_MODEL`。
信息源、板块配额、阈值都在 `config.example.json` 里按需改。
想按兴趣挑选信息源？[doc/rss-sources.md](doc/rss-sources.md) 提供 30+ 经实测验证的分类 RSS 清单（含直连/需代理标注与避坑名单），复制 JSON 片段即可接入。

### 3. 安装 radar（Web 门户 + 综合）

```bash
cd app/radar && uv sync && cd ../..
```

### 4. 可选组件（不装也能跑，只是少几个源）

<details>
<summary><b>RSSHub</b>（AIbase 日报 / The Batch / HF Daily Papers）</summary>

```bash
docker run -d --name rsshub --network host diygod/rsshub
```

</details>

<details>
<summary><b>we-mp-rss</b>（微信公众号 → RSS，需微信扫码授权）</summary>

```bash
git clone https://github.com/rachelos/we-mp-rss app/we-mp-rss
cd app/we-mp-rss && uv sync && cd ../..
cp deploy/we-mp-rss.service ~/.config/systemd/user/
systemctl --user enable --now we-mp-rss.service
```

首次访问 `http://127.0.0.1:8001` 扫码授权公众号。

</details>

### 5. systemd 定时任务

```bash
mkdir -p ~/.config/systemd/user
cp deploy/*.service deploy/*.timer ~/.config/systemd/user/
systemctl --user daemon-reload

systemctl --user enable --now radar-web.service          # 常驻 Web 门户
systemctl --user enable --now horizon-daily.timer        # 08:00 日报
systemctl --user enable --now sos-daily.timer            # 08:40 SoS 今日
systemctl --user enable --now sos-weekly.timer           # 周一 SoS 周报
systemctl --user enable --now radar-distill.timer        # 周日偏好蒸馏

sudo loginctl enable-linger $USER                        # 不登录也照常跑
```

> 💡 unit 文件里用 `%h` 表示家目录，默认假定仓库位于 `~/Myfile/AINews`；
> 克隆到其他路径时请同步修改 service 里的 `WorkingDirectory` / `ExecStart`。

### 6. 开始使用

浏览器打开 `http://<你的局域网IP>:8080`：

- 管理员口令：由 `deploy/radar-web.service` 的 `Environment=RADAR_ACCESS_KEY=...` 配置，**初始 0000，务必修改**；
- 访客口令：由 Horizon `.env` 的 `RADAR_GUEST_KEY=...` 配置（转发邮件给朋友时用访客口令）。

邮箱推送：在 `config.json` 的 `email` 段填好 SMTP 信息，把收件地址加进
`app/Horizon/data/subscribers.json`（或直接回复日报邮件 `SUBSCRIBE`）。

RSS 订阅：任意 RSS 阅读器添加 `http://<你的局域网IP>:8080/rss?key=<访客口令>`，
每天日报更新后即在同一入口收到全部条目（只读实现，不影响网页端的去重与徽章）。

## 🔐 安全与隐私

- **所有口令/密钥只存在于环境变量**：LLM Key、SMTP 授权码、双口令分别放在
  `app/Horizon/.env`（`.gitignore` 已排除 `.env` / `*.env`）与 systemd unit 的 `Environment` 行；
- **仓库内只有脱敏样例**：[config/config.example.json](config/config.example.json)、
  [config/env.example](config/env.example)、[deploy/](deploy/) 不含任何真实地址与密钥；
- **运行数据不入库**：日报产出 `out/`、投票库 `feedback.db`、订阅者列表、上游克隆均在 `.gitignore` 中排除。

## 🗂 目录结构

```
aiNews/
├── app/
│   ├── Horizon/        # 上游采集引擎（git clone，不入库）
│   ├── radar/          # ★ 自研模块：SoS / Web 门户 / 调教 / 解读
│   │   ├── sos_daily.py        # SoS 今日/周报综合
│   │   ├── webapp.py           # FastAPI 门户 + 调教 API
│   │   ├── distill.py          # 👍👎 → 关键词蒸馏
│   │   ├── paper_brief.py      # 论文深度解读
│   │   ├── ghrepo_brief.py     # GitHub 项目解读
│   │   ├── hotlist.py / classic.py / interest.py
│   │   ├── mail.py             # SoS 邮件
│   │   └── static/index.html   # 深色单文件前端
│   └── we-mp-rss/      # 公众号 RSS（git clone，不入库）
├── config/
│   ├── config.example.json     # Horizon 配置样例（含 10+ 源）
│   ├── env.example             # 环境变量模板（密钥自己填）
│   └── profiles/               # LLM 调优 profile（ai-news / papers）
├── deploy/                     # systemd 用户级 service/timer
├── doc/                        # 需求规格 report.md + 项目手册 todo.md + 界面截图 img/
├── .github/workflows/ci.yml    # CI：编译检查 + ruff 错误级检查
└── LICENSE                     # MIT
```

## 🌿 分支说明

| 分支 | 用途 |
|---|---|
| `main` | 稳定版本 |
| `develop` | 开发主线，功能先进后合入 main |

## 🙏 致谢

- [Horizon](https://github.com/Thysrael/Horizon) —— 强大的 AI 日报采集引擎，本项目的信息源流水线；
- [we-mp-rss](https://github.com/rachelos/we-mp-rss) —— 微信公众号转 RSS；
- [RSSHub](https://docs.rsshub.app/) —— 万物皆可 RSS。

> 协议说明：Horizon 与 we-mp-rss 均为 MIT；RSSHub 为 AGPL-3.0——本仓库仅以官方 Docker 镜像独立运行并订阅其 RSS 输出，未包含其任何代码。

---

<div align="center">

⭐ 觉得有用的话，点个 Star 支持一下！

</div>
