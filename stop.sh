#!/bin/bash
# AI Brain Bot — 停止脚本
DIR="$(cd "$(dirname "$0")" && pwd)"

echo "🛑 停止 AI Brain..."

# 停止 bot
if [ -f "$DIR/.bot.pid" ]; then
    pid=$(cat "$DIR/.bot.pid")
    if kill -0 "$pid" 2>/dev/null; then
        kill "$pid" && echo "   Bot (PID=$pid) 已停止"
    fi
    rm -f "$DIR/.bot.pid"
fi

# 停止 proxy
if [ -f "$DIR/.proxy.pid" ]; then
    pid=$(cat "$DIR/.proxy.pid")
    if kill -0 "$pid" 2>/dev/null; then
        kill "$pid" && echo "   Proxy (PID=$pid) 已停止"
    fi
    rm -f "$DIR/.proxy.pid"
fi

# 确保没有残余
pids=$(pgrep -f "python.*main\.py" 2>/dev/null || true)
if [ -n "$pids" ]; then
    echo "   清理残余 bot 进程: $pids"
    echo "$pids" | xargs kill 2>/dev/null || true
fi

echo "✅ 全部已停止"
