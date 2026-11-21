#!/bin/bash
# BioPlot 作图智能体 重启脚本（在 amax 上执行）
cd ~/projects/rosetta1/plot_agent || exit 1
pkill -f "plot_agent_serve[r]" 2>/dev/null && sleep 1
nohup ~/.conda/envs/bioplot/bin/python -u plot_agent_server.py > plot_agent.log 2>&1 < /dev/null &
sleep 4
curl -s -o /dev/null -w "plot_agent: %{http_code}\n" -m 8 http://127.0.0.1:7796/api/health
