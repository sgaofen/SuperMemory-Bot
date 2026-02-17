#!/bin/bash
# ╔══════════════════════════════════════════════════════════════╗
# ║          🧠 SuperMemory Bot — One-Click Setup               ║
# ║         Interactive onboarding for new users                ║
# ╚══════════════════════════════════════════════════════════════╝
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# ── Colors ──
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

# ── Helpers ──
info()    { echo -e "${BLUE}ℹ️  $1${NC}"; }
success() { echo -e "${GREEN}✅ $1${NC}"; }
warn()    { echo -e "${YELLOW}⚠️  $1${NC}"; }
error()   { echo -e "${RED}❌ $1${NC}"; }
header()  { echo -e "\n${BOLD}${CYAN}═══ $1 ═══${NC}\n"; }

ask() {
    local prompt="$1"
    local default="$2"
    local var_name="$3"
    if [ -n "$default" ]; then
        echo -ne "${BOLD}$prompt${NC} [${CYAN}$default${NC}]: "
    else
        echo -ne "${BOLD}$prompt${NC}: "
    fi
    read -r input
    if [ -z "$input" ]; then
        eval "$var_name='$default'"
    else
        eval "$var_name='$input'"
    fi
}

ask_yes_no() {
    local prompt="$1"
    local default="$2"  # y or n
    local var_name="$3"
    if [ "$default" = "y" ]; then
        echo -ne "${BOLD}$prompt${NC} [${CYAN}Y/n${NC}]: "
    else
        echo -ne "${BOLD}$prompt${NC} [${CYAN}y/N${NC}]: "
    fi
    read -r input
    input="${input:-$default}"
    case "$input" in
        [Yy]* ) eval "$var_name=true" ;;
        * )     eval "$var_name=false" ;;
    esac
}

# ══════════════════════════════════════════════════════
#  Welcome
# ══════════════════════════════════════════════════════
clear
echo -e "${BOLD}${CYAN}"
cat << 'BANNER'
  ____                        __  __                                 
 / ___| _   _ _ __   ___ _ __|  \/  | ___ _ __ ___   ___  _ __ _   _ 
 \___ \| | | | '_ \ / _ \ '__| |\/| |/ _ \ '_ ` _ \ / _ \| '__| | | |
  ___) | |_| | |_) |  __/ |  | |  | |  __/ | | | | | (_) | |  | |_| |
 |____/ \__,_| .__/ \___|_|  |_|  |_|\___|_| |_| |_|\___/|_|   \__, |
             |_|                                  Bot Setup      |___/ 
BANNER
echo -e "${NC}"
echo -e "Welcome! This wizard will set up everything you need in one go."
echo -e "欢迎！这个向导会一次性帮你配置好所有东西。\n"

# ══════════════════════════════════════════════════════
#  Step 1: Check Prerequisites
# ══════════════════════════════════════════════════════
header "Step 1/6 — Checking Prerequisites / 检查前提条件"

# Python
if command -v python3 &> /dev/null; then
    PY_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
    PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
    PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
    if [ "$PY_MAJOR" -ge 3 ] && [ "$PY_MINOR" -ge 10 ]; then
        success "Python $PY_VERSION found"
    else
        error "Python 3.10+ is required, but found $PY_VERSION"
        echo "  Install from: https://www.python.org/downloads/"
        exit 1
    fi
else
    error "Python 3 not found. Please install Python 3.10+"
    echo "  Install from: https://www.python.org/downloads/"
    exit 1
fi

# Node.js (optional, for proxy)
HAS_NODE=false
if command -v node &> /dev/null; then
    NODE_VERSION=$(node --version 2>&1)
    success "Node.js $NODE_VERSION found (needed for Antigravity Proxy)"
    HAS_NODE=true
else
    warn "Node.js not found — Antigravity Proxy won't be available"
    echo "  Install from: https://nodejs.org/ (optional, for free Claude/Gemini access)"
fi

# Docker (optional, for Qdrant)
HAS_DOCKER=false
if command -v docker &> /dev/null; then
    success "Docker found (optional: for Qdrant vector DB)"
    HAS_DOCKER=true
else
    info "Docker not found — will use ChromaDB (default, no Docker needed)"
fi

# ══════════════════════════════════════════════════════
#  Step 2: Python Virtual Environment & Dependencies
# ══════════════════════════════════════════════════════
header "Step 2/6 — Setting Up Python Environment / 设置 Python 环境"

if [ -d ".venv" ]; then
    info "Virtual environment already exists (.venv/)"
    ask_yes_no "Recreate it? / 要重新创建吗？" "n" RECREATE_VENV
    if [ "$RECREATE_VENV" = "true" ]; then
        rm -rf .venv
        python3 -m venv .venv
        success "Virtual environment recreated"
    fi
else
    info "Creating virtual environment..."
    python3 -m venv .venv
    success "Virtual environment created (.venv/)"
fi

source .venv/bin/activate
success "Activated virtual environment"

info "Installing dependencies (this may take 1-2 minutes)..."
pip install --upgrade pip -q 2>&1 | tail -1
if [ -f "pyproject.toml" ]; then
    pip install -e "." -q 2>&1 | tail -1
elif [ -f "requirements.txt" ]; then
    pip install -r requirements.txt -q 2>&1 | tail -1
fi
success "All Python dependencies installed"

# ══════════════════════════════════════════════════════
#  Step 3: Discord Bot Token
# ══════════════════════════════════════════════════════
header "Step 3/6 — Discord Bot Token / Discord 机器人令牌"

echo -e "You need a Discord bot token to connect your bot."
echo -e "你需要一个 Discord 机器人令牌来连接你的机器人。\n"
echo -e "  1. Go to ${CYAN}https://discord.com/developers/applications${NC}"
echo -e "  2. Create a new application → Bot"
echo -e "  3. Enable ${BOLD}Message Content Intent${NC} (under Privileged Gateway Intents)"
echo -e "  4. Copy the bot token"
echo ""

ask "Paste your Discord Bot Token / 粘贴你的 Discord Bot Token" "" DISCORD_TOKEN

if [ -z "$DISCORD_TOKEN" ]; then
    warn "No token provided — you'll need to set DISCORD_BOT_TOKEN in .env manually"
    DISCORD_TOKEN="your_discord_bot_token_here"
fi

# ══════════════════════════════════════════════════════
#  Step 4: LLM Configuration
# ══════════════════════════════════════════════════════
header "Step 4/6 — LLM Model Configuration / 大语言模型配置"

echo -e "How do you want to access LLM models?"
echo -e "你想如何访问大语言模型？\n"
echo -e "  ${BOLD}1)${NC} ${GREEN}Antigravity Proxy (recommended)${NC} — Free access to Claude & Gemini"
echo -e "     反代代理（推荐）— 免费使用 Claude 和 Gemini"
if [ "$HAS_NODE" = "false" ]; then
echo -e "     ${YELLOW}⚠️  Requires Node.js (not found on your system)${NC}"
fi
echo ""
echo -e "  ${BOLD}2)${NC} Direct API Key — Use your own Anthropic/OpenAI/Gemini key"
echo -e "     直接 API 密钥 — 使用你自己的 API 密钥"
echo ""
echo -e "  ${BOLD}3)${NC} Ollama (local) — Run models locally, no API needed"
echo -e "     本地 Ollama — 在本地运行模型，无需 API"
echo ""

ask "Choose (1/2/3) / 选择" "1" LLM_CHOICE

USE_PROXY=false
API_KEY=""
API_KEY_VAR=""
DEFAULT_MODEL=""

case "$LLM_CHOICE" in
    1)
        USE_PROXY=true
        DEFAULT_MODEL="anthropic/claude-opus-4-6-thinking"
        success "Using Antigravity Proxy — will auto-start with start.sh"
        echo ""
        echo -e "  Available models via proxy / 代理可用模型:"
        echo -e "    • ${CYAN}claude-opus-4-6-thinking${NC} (strongest, recommended)"
        echo -e "    • ${CYAN}claude-sonnet-4-5${NC} (fast + smart)"
        echo -e "    • ${CYAN}gemini-3-pro-high${NC} (Gemini Pro)"
        echo -e "    • ${CYAN}gemini-2.5-flash${NC} (fastest)"
        echo ""
        echo -e "  You can switch at any time with ${CYAN}/model${NC} command in Discord."
        ;;
    2)
        echo ""
        echo -e "Which provider? / 哪个供应商？"
        echo -e "  ${BOLD}a)${NC} Anthropic (Claude)"
        echo -e "  ${BOLD}b)${NC} OpenAI (GPT)"
        echo -e "  ${BOLD}c)${NC} Google Gemini"
        echo ""
        ask "Choose (a/b/c) / 选择" "a" PROVIDER_CHOICE
        
        case "$PROVIDER_CHOICE" in
            a|A)
                API_KEY_VAR="ANTHROPIC_API_KEY"
                DEFAULT_MODEL="anthropic/claude-sonnet-4-5"
                ask "Paste your Anthropic API Key (sk-ant-...)" "" API_KEY
                ;;
            b|B)
                API_KEY_VAR="OPENAI_API_KEY"
                DEFAULT_MODEL="gpt-4o"
                ask "Paste your OpenAI API Key (sk-...)" "" API_KEY
                ;;
            c|C)
                API_KEY_VAR="GEMINI_API_KEY"
                DEFAULT_MODEL="gemini/gemini-2.5-pro"
                ask "Paste your Gemini API Key (AIza...)" "" API_KEY
                ;;
            *)
                API_KEY_VAR="ANTHROPIC_API_KEY"
                DEFAULT_MODEL="anthropic/claude-sonnet-4-5"
                ask "Paste your API Key" "" API_KEY
                ;;
        esac
        
        if [ -z "$API_KEY" ]; then
            warn "No API key provided — set $API_KEY_VAR in .env manually"
        fi
        ;;
    3)
        DEFAULT_MODEL="ollama/llama3"
        echo ""
        info "Make sure Ollama is running: https://ollama.ai"
        ask "Which Ollama model to use?" "llama3" OLLAMA_MODEL
        DEFAULT_MODEL="ollama/$OLLAMA_MODEL"
        ;;
    *)
        USE_PROXY=true
        DEFAULT_MODEL="anthropic/claude-opus-4-6-thinking"
        ;;
esac

# ══════════════════════════════════════════════════════
#  Step 5: Personalization
# ══════════════════════════════════════════════════════
header "Step 5/6 — Personalization / 个性化设置"

echo -e "Let's personalize your AI companion!"
echo -e "让我们来个性化你的 AI 伙伴！\n"

ask "Your name/nickname (for the AI to call you) / 你的名字/昵称" "user" BOT_USER_ID
ask "AI companion's name / AI 伙伴的名字" "Aura" BOT_NAME

echo ""
echo -e "Common timezones / 常见时区:"
echo -e "  • ${CYAN}America/Los_Angeles${NC} (US Pacific / 美西)"
echo -e "  • ${CYAN}America/New_York${NC} (US Eastern / 美东)"
echo -e "  • ${CYAN}Asia/Shanghai${NC} (China / 中国)"
echo -e "  • ${CYAN}Asia/Tokyo${NC} (Japan / 日本)"
echo -e "  • ${CYAN}Europe/London${NC} (UK / 英国)"
ask "Your timezone / 你的时区" "America/Los_Angeles" USER_TZ

echo ""
ask "AI's language preference / AI 使用的语言 (zh=中文, en=English, mixed=混合)" "mixed" LANG_PREF

echo ""
ask_yes_no "Enable AI diary feature? / 启用 AI 日记功能？" "y" DIARY_ENABLED
ask_yes_no "Enable web search? / 启用网络搜索？" "y" WEB_SEARCH

echo ""
ask "Special person to track (e.g. partner name, leave empty to skip) / 特别关注的人" "" SPECIAL_PERSON

# ══════════════════════════════════════════════════════
#  Step 6: Generate Config Files
# ══════════════════════════════════════════════════════
header "Step 6/6 — Generating Configuration / 生成配置文件"

# ── Generate .env ──
info "Creating .env file..."

cat > "$DIR/.env" << ENVFILE
# ═══════════════════════════════════════════════════════
# SuperMemory Bot — Generated by setup.sh
# $(date '+%Y-%m-%d %H:%M:%S')
# ═══════════════════════════════════════════════════════

# ===== Discord =====
DISCORD_BOT_TOKEN=$DISCORD_TOKEN

# ===== User Identity =====
BOT_USER_ID=$BOT_USER_ID
BOT_NAME=$BOT_NAME
SPECIAL_PERSON_NAME=$SPECIAL_PERSON
SPECIAL_PERSON_LABEL=Special Person

# ===== LLM Access =====
ANTIGRAVITY_PROXY_ENABLED=$USE_PROXY
ANTIGRAVITY_PROXY_URL=http://localhost:8080
ENVFILE

# Add API key if provided
if [ -n "$API_KEY" ] && [ -n "$API_KEY_VAR" ]; then
    echo "$API_KEY_VAR=$API_KEY" >> "$DIR/.env"
fi

cat >> "$DIR/.env" << ENVFILE

# ===== Default Model =====
DEFAULT_MODEL=$DEFAULT_MODEL

# ===== Timezone =====
USER_TIMEZONE=$USER_TZ

# ===== Features =====
USE_ENHANCED_GATEWAY=true
WEB_SEARCH_ENABLED=$WEB_SEARCH
DIARY_ENABLED=$DIARY_ENABLED
DIARY_AUTO_GENERATE=true
DIARY_AUTO_GENERATE_TIME=23:30
DIARY_AUTO_GENERATE_AFTER_MESSAGES=30
DIARY_VIEWER_PORT=8888
VOICE_AUTO_TRANSCRIBE=true

# ===== Memory Tuning (defaults work well, tweak if needed) =====
MEMORY_RETRIEVAL_LIMIT=120
MEMORY_TOKEN_BUDGET=18000
HISTORY_TOKEN_BUDGET=120000
SYSTEM_PROMPT_TOKEN_BUDGET=30000
TOTAL_CONTEXT_TOKEN_BUDGET=160000
RESPONSE_RESERVE_TOKENS=8192
MAX_OUTPUT_TOKENS=4096
MAX_HISTORY_TURNS=2000

# Soul personality auto-sync
SOUL_SYNC_EVERY_N_ADDS=6
SOUL_SYNC_MIN_INTERVAL_SEC=90
SOUL_PROMPT_MAX_CHARS=12000

# Memory dedup
MEMORY_DUPLICATE_SCORE=0.92
MEM0_INFER=false
AUTO_RECOVER_MEMORY_ON_START=true
ENVFILE

success "Created .env"

# ── Generate soul.md ──
if [ ! -f "$DIR/soul.md" ]; then
    if [ -f "$DIR/soul.example.md" ]; then
        cp "$DIR/soul.example.md" "$DIR/soul.md"

        # Personalize soul.md with user's bot name
        if [ "$BOT_NAME" != "Aura" ]; then
            sed -i.bak "s/AI Companion/$BOT_NAME/g" "$DIR/soul.md" 2>/dev/null || \
            sed -i '' "s/AI Companion/$BOT_NAME/g" "$DIR/soul.md" 2>/dev/null || true
            rm -f "$DIR/soul.md.bak"
        fi

        success "Created soul.md from template (customize it to define your AI's personality!)"
        info "  Edit soul.md to give your AI a unique personality"
    fi
else
    info "soul.md already exists, keeping it"
fi

# ── Make scripts executable ──
chmod +x "$DIR/start.sh" "$DIR/stop.sh" 2>/dev/null || true
success "Made start.sh and stop.sh executable"

# ── Create data directory ──
mkdir -p "$DIR/data"
success "Created data/ directory"

# ══════════════════════════════════════════════════════
#  Summary & Next Steps
# ══════════════════════════════════════════════════════
echo ""
echo -e "${BOLD}${GREEN}"
cat << 'DONE'
  ╔══════════════════════════════════════════════════╗
  ║           🎉 Setup Complete! 设置完成！           ║
  ╚══════════════════════════════════════════════════╝
DONE
echo -e "${NC}"

echo -e "${BOLD}Your configuration / 你的配置:${NC}"
echo -e "  🤖 AI Name:  ${CYAN}$BOT_NAME${NC}"
echo -e "  👤 User:     ${CYAN}$BOT_USER_ID${NC}"
echo -e "  🧠 Model:    ${CYAN}$DEFAULT_MODEL${NC}"
echo -e "  🌍 Timezone: ${CYAN}$USER_TZ${NC}"
if [ "$USE_PROXY" = "true" ]; then
echo -e "  🔌 LLM Mode: ${GREEN}Antigravity Proxy (free)${NC}"
else
echo -e "  🔌 LLM Mode: ${YELLOW}Direct API${NC}"
fi
if [ "$DIARY_ENABLED" = "true" ]; then
echo -e "  📔 Diary:    ${GREEN}Enabled${NC}"
fi
if [ -n "$SPECIAL_PERSON" ]; then
echo -e "  💖 Special:  ${CYAN}$SPECIAL_PERSON${NC}"
fi

echo ""
echo -e "${BOLD}Next steps / 下一步:${NC}\n"

if [ "$DISCORD_TOKEN" = "your_discord_bot_token_here" ]; then
echo -e "  ${YELLOW}1. Set your Discord token in .env${NC}"
echo -e "     编辑 .env 填入你的 Discord Bot Token\n"
fi

echo -e "  ${BOLD}Start the bot / 启动机器人:${NC}"
echo -e "  ${CYAN}./start.sh${NC}"
echo ""
echo -e "  ${BOLD}Or start manually / 或手动启动:${NC}"
if [ "$USE_PROXY" = "true" ]; then
echo -e "  ${CYAN}npx -y antigravity-claude-proxy@latest start${NC}  (Terminal 1)"
fi
echo -e "  ${CYAN}source .venv/bin/activate && python main.py${NC}  (Terminal 2)"
echo ""
echo -e "  ${BOLD}Stop everything / 停止:${NC}"
echo -e "  ${CYAN}./stop.sh${NC}"
echo ""
echo -e "  ${BOLD}Customize personality / 自定义人格:${NC}"
echo -e "  Edit ${CYAN}soul.md${NC} to define your AI's personality"
echo ""

# ── Auto-start option ──
echo ""
ask_yes_no "Start the bot now? / 现在启动机器人吗？" "n" START_NOW

if [ "$START_NOW" = "true" ]; then
    echo ""
    info "Starting SuperMemory Bot..."
    exec "$DIR/start.sh"
else
    echo ""
    echo -e "${GREEN}All done! Run ${CYAN}./start.sh${GREEN} when you're ready. 🚀${NC}"
fi
