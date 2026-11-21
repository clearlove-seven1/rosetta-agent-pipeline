#!/bin/bash
# Rosetta-Agent MCP Server（streamable-http, 127.0.0.1:7782）重启脚本
# 供 octop 平台挂载 15 个工具；日志: mcp_server.log
PORT=7782
PY="$HOME/.conda/envs/rosetta-agent/bin/python"
cd "$(dirname "$0")" || exit 1
pkill -f '[r]osetta_mcp_server.py' 2>/dev/null && sleep 1
nohup setsid "$PY" rosetta_mcp_server.py --transport streamable-http --host 127.0.0.1 --port "$PORT" >> mcp_server.log 2>&1 &
sleep 3
if ss -tln | grep -q ":$PORT "; then
  echo "OK: rosetta MCP listening on 127.0.0.1:$PORT (pid $(pgrep -f '[r]osetta_mcp_server.py' | tr '\n' ' '))"
else
  echo "FAILED to start:"; tail -8 mcp_server.log
fi
