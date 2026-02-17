<div align="center">

# 🧠 AI Brain Bot

### Your Personal AI Companion with Persistent Memory

**一个拥有持久记忆的 AI 私人伙伴**

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Discord.py](https://img.shields.io/badge/discord.py-2.3+-7289da.svg)](https://discordpy.readthedocs.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

[English](#-features) · [中文](#-功能特性)

</div>

---

## ✨ Features

AI Brain Bot is a Discord-based AI companion that **actually remembers you**. Unlike standard chatbots that forget everything between sessions, this bot builds a persistent understanding of who you are, what you care about, and what's happening in your life.

### 🧠 Persistent Memory System
- **Vector-based memory** powered by [Mem0](https://github.com/mem0ai/mem0) + [ChromaDB](https://www.trychroma.com/)
- Automatic extraction of facts, preferences, plans, and relationships from conversations
- Confidence scoring to distinguish direct statements from inferred information
- Self-healing vector store with automatic backup and recovery

### 🌙 Soul Personality System
- Customizable AI personality via `soul.md`
- Dynamic auto-summary that evolves as the AI learns more about you
- Categorized memory map: identity, education, work, relationships, emotions, and more

### 📔 AI Diary
- Automatic daily diary generation from your conversations
- Beautiful narrative-style entries with literary flair
- Web viewer with calendar navigation and dark mode
- Auto-triggers based on time or message count

### 🎯 Smart Follow-ups
- Detects plans, goals, and commitments from your messages
- Naturally follows up at appropriate intervals
- Tracks completion status automatically

### 🎭 Emotion Analysis
- Understands emotional context in conversations
- Adjusts response tone accordingly

### 🔍 Web Search & Archive Search
- Real-time web search integration
- Search through your own conversation history

### 📅 Event Scheduling
- Discord-integrated reminders and event tracking

---

## ✨ 功能特性

AI Brain Bot 是一个基于 Discord 的 AI 伙伴，**真正能记住你**。不同于标准聊天机器人在每次对话后就遗忘一切，这个机器人会持续积累对你的了解——你是谁、你关心什么、你生活中发生了什么。

### 🧠 持久记忆系统
- 基于 [Mem0](https://github.com/mem0ai/mem0) + [ChromaDB](https://www.trychroma.com/) 的**向量记忆**
- 自动从对话中提取事实、偏好、计划和人际关系
- 置信度评分，区分直接陈述与推断信息
- 向量数据库自我修复，自动备份与恢复

### 🌙 灵魂人格系统
- 通过 `soul.md` 自定义 AI 人格
- 动态自动总结，随着 AI 更了解你而进化
- 分类记忆地图：身份、教育、工作、关系、情绪等

### 📔 AI 日记
- 根据每天的对话自动生成日记
- 温暖细腻的叙事风格，富有文学感
- 网页查看器，支持日历导航和暗黑模式
- 基于时间或消息数量自动触发

### 🎯 智能跟进
- 从消息中检测计划、目标和承诺
- 在适当时机自然地跟进询问
- 自动追踪完成状态

### 🎭 情绪分析
- 理解对话中的情绪语境
- 相应调整回复语气

### 🔍 网络搜索 & 历史搜索
- 实时网络搜索集成
- 搜索你自己的对话历史

### 📅 事件提醒
- Discord 集成的提醒和事件追踪

---

## 🏗 Architecture / 架构

```
brain_bot/
├── main.py                  # Entry point / 入口
├── soul.md                  # AI personality (auto-created) / AI 人格定义
├── soul.example.md          # Personality template / 人格模板
├── .env.example             # Environment config template / 环境配置模板
├── start.sh / stop.sh       # Service scripts / 服务脚本
├── pyproject.toml           # Dependencies / 依赖定义
│
├── src/
│   ├── bot/
│   │   └── bot.py           # Discord bot & slash commands / Discord 机器人
│   ├── gateway/
│   │   ├── gateway.py        # Core LLM routing / 核心 LLM 路由
│   │   ├── enhanced_gateway.py  # Full-featured gateway / 增强网关
│   │   └── context_builder.py   # System prompt assembly / 系统提示构建
│   └── memory/
│       ├── memory_manager.py    # Vector memory CRUD / 向量记忆管理
│       ├── memory_extractor.py  # Structured memory extraction / 结构化记忆提取
│       ├── soul_manager.py      # Personality file management / 人格文件管理
│       ├── diary_manager.py     # AI diary generation / AI 日记生成
│       ├── emotion_analyzer.py  # Emotion detection / 情绪检测
│       ├── event_scheduler.py   # Event reminders / 事件提醒
│       ├── followup_manager.py  # Topic follow-ups / 话题跟进
│       ├── archive_manager.py   # Chat archive & search / 聊天存档与搜索
│       └── session_context.py   # Session memory / 会话记忆
│
└── diary_viewer/            # Web diary viewer / 网页日记查看器
    ├── server.py
    ├── index.html
    ├── app.js
    └── style.css
```

---

## 🚀 Quick Start / 快速开始

### Prerequisites / 前提条件

- **Python 3.10+**
- **Discord Bot Token** — [Create one here / 在这里创建](https://discord.com/developers/applications)
- **LLM API Access** — One of:
  - Direct API keys (OpenAI / Anthropic / Google Gemini)
  - [Antigravity Proxy](https://github.com/antigravityinc) (recommended for Claude/Gemini)

### 1. Clone the Repository / 克隆仓库

```bash
git clone https://github.com/YOUR_USERNAME/brain_bot.git
cd brain_bot
```

### 2. Create Virtual Environment / 创建虚拟环境

```bash
python3 -m venv .venv
source .venv/bin/activate   # macOS/Linux
# .venv\Scripts\activate    # Windows
```

### 3. Install Dependencies / 安装依赖

```bash
pip install -e .
# or / 或者
pip install -r requirements.txt
```

### 4. Configure Environment / 配置环境变量

```bash
cp .env.example .env
```

Edit `.env` with your settings:
编辑 `.env` 填入你的配置：

```ini
# Required / 必填
DISCORD_BOT_TOKEN=your_discord_bot_token_here

# Choose one LLM access method / 选择一种 LLM 访问方式：

# Option A: Direct API key / 方式 A：直接 API 密钥
ANTHROPIC_API_KEY=sk-ant-...
# or / 或
OPENAI_API_KEY=sk-...
# or / 或
GEMINI_API_KEY=...

# Option B: Antigravity Proxy (recommended) / 方式 B：反代代理（推荐）
ANTIGRAVITY_PROXY_ENABLED=true
ANTIGRAVITY_PROXY_URL=http://localhost:8080

# Personalization / 个性化
BOT_USER_ID=your_name          # Your identifier / 你的标识
BOT_NAME=Aura                  # AI's name / AI 的名字
USER_TIMEZONE=America/Los_Angeles  # Your timezone / 你的时区
```

### 5. Discord Bot Setup / Discord 机器人设置

1. Go to [Discord Developer Portal](https://discord.com/developers/applications)
2. Create a new application → Bot
3. Enable **Message Content Intent** under Privileged Gateway Intents
4. Generate an invite URL with `bot` + `applications.commands` scopes
5. Invite the bot to your server

前往 [Discord 开发者门户](https://discord.com/developers/applications)：
1. 创建新应用 → Bot
2. 启用 **Message Content Intent**（特权网关意图中）
3. 生成包含 `bot` + `applications.commands` 权限的邀请链接
4. 将机器人邀请到你的服务器

### 6. Run / 运行

```bash
# Simple start / 简单启动
python main.py

# Or use the service script (with proxy) / 或使用服务脚本（含代理）
chmod +x start.sh stop.sh
./start.sh

# Stop / 停止
./stop.sh
```

---

## ⚙️ Configuration Reference / 配置参考

### Core Settings / 核心设置

| Variable | Default | Description |
|---|---|---|
| `DISCORD_BOT_TOKEN` | — | **Required.** Discord bot token |
| `DEFAULT_MODEL` | `claude-opus-4-6-thinking` | LLM model to use |
| `BOT_USER_ID` | `user` | Your identifier in the memory system |
| `BOT_NAME` | `AI` | The AI companion's display name |
| `USER_TIMEZONE` | `America/Los_Angeles` | Your local timezone |

### Memory Tuning / 记忆调优

| Variable | Default | Description |
|---|---|---|
| `MEMORY_RETRIEVAL_LIMIT` | `120` | Memories retrieved per query |
| `MEMORY_TOKEN_BUDGET` | `18000` | Token budget for memory context |
| `SYSTEM_PROMPT_TOKEN_BUDGET` | `30000` | Total system prompt budget |
| `SOUL_PROMPT_MAX_CHARS` | `12000` | Max chars from soul.md in prompt |
| `MEMORY_DUPLICATE_SCORE` | `0.92` | Similarity threshold for dedup |
| `MEM0_INFER` | `false` | Let Mem0 rewrite/merge memories |

### Diary / 日记

| Variable | Default | Description |
|---|---|---|
| `DIARY_ENABLED` | `true` | Enable diary feature |
| `DIARY_AUTO_GENERATE` | `true` | Auto-generate daily diary |
| `DIARY_AUTO_GENERATE_TIME` | `23:30` | Auto-generation time |
| `DIARY_AUTO_GENERATE_AFTER_MESSAGES` | `30` | Trigger after N messages |
| `DIARY_VIEWER_PORT` | `8888` | Web viewer port |

### Special Person Tracking / 特别关注

| Variable | Default | Description |
|---|---|---|
| `SPECIAL_PERSON_NAME` | _(empty)_ | Name to track specially |
| `SPECIAL_PERSON_LABEL` | `Special Person` | Display label for this person |

---

## 🤖 Discord Commands / Discord 命令

| Command | Description |
|---|---|
| `/remember <text>` | Manually save a memory / 手动保存记忆 |
| `/recall <query>` | Search your memories / 搜索记忆 |
| `/soul` | View/manage personality file / 查看/管理人格文件 |
| `/diary [date]` | View diary entry / 查看日记 |
| `/events` | View upcoming events / 查看事件 |
| `/followup` | View tracked topics / 查看跟进话题 |

---

## 📔 Diary Viewer / 日记查看器

The diary viewer is a local web app for browsing AI-generated diary entries:

日记查看器是一个本地网页应用，用于浏览 AI 生成的日记：

```bash
# Start the viewer / 启动查看器
python diary_viewer/server.py
# Open http://localhost:8888 / 打开 http://localhost:8888
```

Features / 功能:
- 📅 Calendar navigation / 日历导航
- 🌙 Dark mode / 暗黑模式
- 📖 Full markdown rendering / 完整 Markdown 渲染

---

## 🛡 Privacy & Security / 隐私与安全

This project stores all data locally on your machine:
本项目所有数据都存储在你的本地机器上：

- **Memory database**: `data/chromadb/` (vector embeddings)
- **Chat archive**: `data/chat_archive.jsonl`
- **Diary entries**: `data/diary/`
- **Personality**: `soul.md` (gitignored)

⚠️ **Never commit your `.env` file** — it contains your API keys and tokens.
⚠️ **永远不要提交 `.env` 文件** — 它包含你的 API 密钥和令牌。

---

## 🤝 Contributing / 贡献

Contributions are welcome! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

欢迎贡献！请查看 [CONTRIBUTING.md](CONTRIBUTING.md) 了解指南。

---

## 📄 License / 许可证

This project is licensed under the [MIT License](LICENSE).

本项目采用 [MIT 许可证](LICENSE) 授权。

---

<div align="center">

**Built with ❤️ for people who want an AI that truly knows them.**

**为那些想要一个真正了解自己的 AI 的人而建。**

</div>
