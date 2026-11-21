# OPS-RUNBOOK — rosetta1 服务运维手册

> 2026-11-21 整理。服务器 amax(LAN);rosetta1 仓库根 = `~/projects/rosetta1`。
> 相关文档:CHANGES-rosetta.md(修复与回滚)、REVIEW-followup.md §七(切换)、PROJECT-ROADMAP.md(总进度)、TASK-CONTRACT-v0.1.md(契约)。

## 一、服务清单(rosetta1 管辖)

| 端口 | 服务 | 启动方式 | 日志 |
|---|---|---|---|
| 7779 | `rosetta_server.py`(Web/API 平台,LangGraph 智能体) | `bash start_all.sh` 或 `nohup python -u rosetta_server.py >> rosetta_server.log 2>&1 &` | `rosetta_server.log` |
| 7782 | `rosetta_mcp_server.py`(MCP streamable-http,127.0.0.1,octop 挂载 21 工具) | `bash start_all.sh` 或 `bash mcp_restart.sh` | `mcp_server.log` |

不属于本仓库管辖(勿误动):7795=AIDD `ai蛋白质设计/web`;7796=bioplot 作图智能体;
7778=用户另一会话的 GROMACS Agent;7860=ros-gro Workbench;8080/8081=总部ui;8082/8083=wetlab/docking。

## 二、拉起 / 重启

- **服务器重启后/服务全挂**:`bash start_all.sh`(幂等,已在监听则跳过)。
- **单服务重启**(代码更新后生效):
  ```bash
  pkill -f "rosetta_server[.]py"; sleep 2          # pkill 单独一条,模式用 [.] 防自匹配
  nohup ~/.conda/envs/rosetta-agent/bin/python -u rosetta_server.py >> rosetta_server.log 2>&1 < /dev/null &
  ```
  MCP 直接 `bash mcp_restart.sh`(注意:会同时杀掉 Trae 以 stdio 拉起的实例,属预期)。
- **重启前自查**:确认无运行中计算(`pgrep -f "run_[d]dg|run_[r]elax"` 应为空)、
  `/api/tasks` 无 live 任务;GPU/CPU 空闲窗口操作。

## 三、健康检查

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:7779/          # 200
curl -s http://127.0.0.1:7779/api/host/status                            # 200
curl -s http://127.0.0.1:7779/api/tasks | head -c 300                    # 含 status 字段
ss -tln | grep -E ":(7779|7782) "                                        # 双端口监听
curl -s http://127.0.0.1:7778/api/security                               # gromacs(另一会话)自报版本
```

## 四、测试(改动后必跑)

```bash
cd ~/projects/rosetta1 && PY=~/.conda/envs/rosetta-agent/bin/python
$PY eval_v01_guardrails.py && $PY eval_p0_fixes_20261120.py && \
$PY eval_review_followup_20261121.py && $PY eval_contract_20261121.py && \
$PY eval_export_20261121.py && $PY eval_playbook_20261121.py && $PY eval_capabilities_20261121.py
```

## 五、常见故障定位

| 症状 | 排查 |
|---|---|
| 7779 无响应 | `tail rosetta_server.log`;端口被占?`ss -tlnp \| grep 7779` |
| 任务卡 running | 查 `output/<task>/task_status.json`;runner 存活 `pgrep -f "run_[d]dg"`;runner 已退且无汇总=interrupted(待核实) |
| 突变全失败 | 逐个 `output/<task>/saturation_mutagenesis/mut_*.log` 末尾 `[exit code]`;ROSETTA_BIN_DIR 是否有效 |
| MCP 工具列表为空/串任务 | 客户端重连(会话语义);`tail mcp_server.log` |
| 检索无结果 | `docs/playbooks/` 与 `rosetta_manuals/` 语料在磁盘上存在;服务日志看 RAG 构建告警 |
| 对话重启后丢失 | 检查 `output/checkpoints.sqlite` 是否存在(SqliteSaver 落盘);事件流不可回放是既有边界,续聊上下文可恢复 |

## 六、备份与回滚

- 代码快照/回退包:`~/backups/`(rosetta1_snapshot、gromacs_pre_r3 等);
- 原始 git 历史:`~/backups/rosetta1_git_pre_rewrite_20261121.bundle`(历史重写前);
- 代码回滚:`git checkout <commit>` + 重启;旧文件级回滚见 CHANGES §八(`.bak-*` 链)。
