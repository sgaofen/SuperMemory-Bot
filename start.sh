#!/bin/bash
# AI Brain Bot — 后台启动脚本
# 启动 antigravity-claude-proxy + Discord bot，防止重复启动
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

PID_BOT="$DIR/.bot.pid"
PID_PROXY="$DIR/.proxy.pid"
LOG_BOT="$DIR/bot.log"
LOG_PROXY="$DIR/proxy.log"

# ---------- helpers ----------
is_running() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }

kill_old() {
    if is_running "$1"; then
        echo "⚠️  杀掉旧进程 PID=$(cat "$1")"
        kill "$(cat "$1")" 2>/dev/null || true
        sleep 1
    fi
    rm -f "$1"
}

# ---------- 杀掉所有残余 main.py 进程 ----------
echo "🔍 检查残余进程..."
pids=$(pgrep -f "python.*main\.py" 2>/dev/null || true)
if [ -n "$pids" ]; then
    echo "⚠️  发现残余 bot 进程: $pids — 正在杀掉"
    echo "$pids" | xargs kill 2>/dev/null || true
    sleep 1
fi

kill_old "$PID_PROXY"
kill_old "$PID_BOT"

# ---------- 启动 proxy ----------
echo "🔗 启动 antigravity-claude-proxy..."
nohup npx -y antigravity-claude-proxy@latest start > "$LOG_PROXY" 2>&1 &
echo $! > "$PID_PROXY"
echo "   PID=$(cat "$PID_PROXY") → $LOG_PROXY"

# 等待 proxy 就绪
echo -n "   等待 proxy 启动"
for i in $(seq 1 20); do
    if curl -sf http://localhost:8080/health > /dev/null 2>&1; then
        echo " ✅"
        break
    fi
    echo -n "."
    sleep 1
done
if ! curl -sf http://localhost:8080/health > /dev/null 2>&1; then
    echo " ❌ proxy 启动超时，请手动检查 $LOG_PROXY"
    exit 1
fi

# ---------- 启动 bot ----------
echo "🧠 启动 AI Brain Bot..."
source "$DIR/.venv/bin/activate"
nohup python3 "$DIR/main.py" > "$LOG_BOT" 2>&1 &
echo $! > "$PID_BOT"
echo "   PID=$(cat "$PID_BOT") → $LOG_BOT"

sleep 3
if is_running "$PID_BOT"; then
    echo ""
    echo "✅ 全部启动成功！可以安全关闭 terminal。"
    echo "   查看日志: tail -f $LOG_BOT"
    echo "   停止所有: bash $DIR/stop.sh"
else
    echo "❌ Bot 启动失败，请检查 $LOG_BOT"
    exit 1
fi
