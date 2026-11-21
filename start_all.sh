#!/bin/bash
# start_all.sh — rosetta-agent 服务自动拉起(幂等:端口已在监听则跳过)
# 管辖范围:仅 rosetta1 的两个服务(7779 平台 / 7782 MCP)。
# 7795(AIDD web)/7796(bioplot)等属其他项目,不在此管辖,见 OPS-RUNBOOK.md。
# 用法: bash start_all.sh
set -u
PY="$HOME/.conda/envs/rosetta-agent/bin/python"
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR" || exit 1

ensure_port() { ss -tln | grep -q ":$1 " && { echo "[skip] $2 已在 $1 监听"; return 0; }; return 1; }

# 1) 7779 平台(rosetta_server.py,对外 Web/API)
if ensure_port 7779 rosetta_server; then :; else
  nohup "$PY" -u rosetta_server.py >> rosetta_server.log 2>&1 < /dev/null &
  echo "[start] rosetta_server.py → 7779 (pid $!)"
fi

# 2) 7782 MCP(streamable-http,127.0.0.1,octop 平台挂载 21 工具)
if ensure_port 7782 rosetta_mcp_server; then :; else
  nohup setsid "$PY" rosetta_mcp_server.py --transport streamable-http \
        --host 127.0.0.1 --port 7782 >> mcp_server.log 2>&1 < /dev/null &
  # jieba 分词构建检索器使启动可达 30s+:轮询等待,勿用固定 sleep
  ok=""
  for i in $(seq 1 30); do
    if ss -tln | grep -q ":7782 "; then ok=1; break; fi
    sleep 2
  done
  if [ -n "$ok" ]; then
    echo "[start] rosetta_mcp_server.py → 7782 OK"
  else
    echo "[FAIL] 7782 60s 内未监听,查看 mcp_server.log 末尾"
  fi
fi

echo "完成。健康检查: curl -s http://127.0.0.1:7779/api/host/status ; curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:7782/mcp"
