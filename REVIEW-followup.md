# REVIEW-followup.md — 2026-11-21 独立复核与验收报告

> 对 2026-11-20（见 CHANGES-rosetta.md）修改的独立复核：真实重跑测试、MCP 双会话交错实测、
> 状态并发跨进程实测、Rosetta 编号地面真值验证、小规模真实流水线回归。
> 本轮约束遵守情况：未整合 GROMACS；未改科学计算参数；**未重启/替换生产服务**；
> 未清理历史产物（仅删除本轮自建的 2 个夹具错误的 FAILSIM 模拟目录，其错误现场文本已留档本文）。

---

## 一、确认当前状态（全部本轮实测，未沿用旧报告）

- **生产进程仍为旧代码**（按名重新定位，非旧 PID）：
  - 7779 `rosetta_server.py`：pid 581739，启动于 **11-18 21:45:48**，早于 11-20 23:54 的代码推送；
  - 7782 `rosetta_mcp_server.py`：pid 1641461，启动于 **11-19 21:28:52**，同样早于推送。
  - 两进程 cwd 均为项目根；11-20 推送后无其他会话改动仓库（mtime 23:54–23:56 一致）。
- 测试真实重跑：`eval_v01_guardrails.py` **26/26**、`eval_p0_fixes_20261120.py` **75/75**（复核轮更新后为 **76/76**）。

## 二、已确认正确的内容及证据

| 项 | 证据 |
|---|---|
| 任务 ID 唯一性/不覆盖/分段兼容 | 两真实 MCP 会话 create_task 得到不同 ID（`task_20261121_101404678c_SESSA` vs `_1014058ae9_SESSB`）；eval_p0 B 组 12 项 |
| 位点预检/编号映射（5 类输入） | **Rosetta 地面真值 7/7 MATCH**（见 §四）：从 cartesian_ddg 输出反推实际 pose 视图逐位对照 |
| 持久状态生命周期/重启查询 | R 系列 14 项（跨进程读取、损坏文件、隔离、进程证据判定）+ C4a/b/d |
| 科学口径护栏 | guardrails 26/26；WB 两开关默认关闭复测通过 |
| 前一版"JSON 任务行/参数列表执行" | eval_p0 D 组 17 项（旗标逐字一致、无 shell 元字符）+ 真实流水线阶段一（§五） |

## 三、新发现的问题及修复（本轮全部已修复并实测）

1. **【严重】MCP 任务隔离在真实多会话下仍然失效**（复核实证，取代 11-20 方案）
   - 实测：两个独立 streamable-http 会话 A/B 交错 set_workspace 后，A 的无参调用读到 B 的任务文件
     （`A_default_reads_B=true`，VERDICT: NOT_ISOLATED）。根因：无参原子工具仍读进程级共享 `_ACTIVE_TASK`。
   - 修复：`_ACTIVE_TASK` 全局降级为仅无会话上下文的回退；"当前任务"改为**按 MCP 会话隔离**
     （`_SESSION_TASKS[session_key]`）。SDK 1.29.1 的 Context/ServerSession 无公开 session_id（实测），
     故以进程内唯一的 `id(ctx.session)` 为键（同时存活的会话地址不复用；断线重连需重新绑定，
     与 MCP 会话语义一致，已写入代码注释）。ctx 经 `Tool.from_function(context_kwarg="ctx")`
     显式注入（装饰器路径检测不到我们注入的签名，实测 get_type_hints 会 unwrap 掉 wraps）。
   - 修复后复测：**VERDICT: ISOLATED_OK**——A 设→B 设→A 再操作仍读 A；双向无污染；
     显式 task_id 跨任务读写正确；非法 task_id 被拒；**FastMCP 实际暴露的 inputSchema 含 `task_id` 且无 `ctx`**（真实 list_tools 实测）。
   - 旧客户端影响：**无需调整**。同一会话内 create_task→set_workspace→原子工具行为不变；
     跨会话不能再隐式共享"当前任务"（这正是缺陷本身）；断线重连需重新绑定。
2. **【严重】run_ddg.py 生成代码缺 `score_path`/`wt_pdb_path` 定义**（11-20 重构引入的回归）
   - 实测：fail-sim 首跑 runner 即 `NameError: score_path is not defined` 崩溃（run_launcher.log 留档）。
     11-20 的测试只 py_compile（语法级），抓不到未定义名——测试盲区。
   - 修复：补回两行赋值；测试新增 **AST 未定义名检查**（eval_p0 D2，可抓此类回归）。
3. **【中】0 个成功突变也可能被记为 completed**（空壳 `ddg_results.txt` 仅表头即算完成）
   - 修复：completed 需 `summary 在位且 ≥1 个 .ddg`；空壳 → `failed/NoSuccessfulMutants`（runner 收尾、
     state_effective、settle 三处一致）。实测：可控失败模拟 19 个任务全部 rc=-999 → **终态 failed**，不虚报。
4. **【中】task_status.json 并发写丢更新**（服务线程与 runner 子进程无锁读改写）
   - 修复：`state_write` 与 runner 内嵌状态代码均加 **flock 跨进程互斥**（临界区读-改-写 + os.replace 原子替换；
     flock 随进程崩溃由内核释放，无残留锁）。实测：6 进程×40 次交错写，字段零丢失、全程文件可解析（14/14 中 R1 组）。
5. **【中】PID 复用会把旧任务误判为"仍在运行"**
   - 修复：判活改为读 `/proc/<pid>/cmdline` 并校验 marker（run_ddg.py / run_relax_parallel.py）——
     PID 被无关进程复用 → 按"已退出"处理；僵尸（cmdline 空）→ 判死；不可判时仍标"待核实"。
     实测 R2a–R2e。
6. **【中】旧 runner 晚到收尾可能把新提交的状态改回 completed/failed**
   - 修复：runner 收尾走 `_finalize_status(pid 守卫)`——状态文件记录的 ddg runner 与自身 pid 不符时
     只写 `stale_runner_note`，不改状态。实测 R3a/R3b。
7. **【重要·编号语义】缺主链原子(N/CA/C)残基的处理与 Rosetta 实际行为相反**
   - 地面真值实测（§四）：Rosetta 会 **跳过** 缺主链残基（"skipping pdb residue... missing: CA"），
     其不进入 pose；此前"按任意原子行计入编号"会造成错位（且伴随链断裂使 cartesian_ddg 断言崩溃）。
   - 修复：`build_numbering` 剔除缺主链残基并计入 anomalies（N/C 缺失未单独实测，按同一保守规则处理并注明）。
   - 生产链路不受影响的机理：生产 ddg 输入是 relax 产物（缺失残基在 relax 加载时已被 Rosetta 剔除）；
     直接以含缺失残基的清洗文件喂 cartesian_ddg 会崩溃（实测 ROSETTA_CRASH）——该路径本不存在于生产。

## 四、Rosetta 编号地面真值验证（7/7 MATCH，命令与产物全留档）

方法：每类输入用真实 `cartesian_ddg.linuxgccrelease -ddg:iterations 1 -ddg:cartesian -ddg:dump_pdbs true`
跑一个突变，然后**枚举 Rosetta 输出 WT_*.pdb 的残基顺序（=实际 pose 视图）逐位对照
`task_runtime.build_numbering`**；另以 MUT_*.pdb 中突变残基的实际落位交叉验证 mutfile 命中。
基底：`inputs/pdbs/1MC9_protein.pdb`（链 A，残基 4-129）。产物目录：`verify_numbering_20261121/`
（每 case 含 input_raw.pdb / cleaned / mutfile / ddg_log.txt（含完整命令）/ WT_/MUT_ 输出），
汇总 `verify_report.json`。

| 用例 | 结果 | 要点 |
|---|---|---|
| single 普通单链（非 1 起始编号 4-129） | **MATCH** 126/126 | 含 mut 命中位置 == 预测 |
| multi_dup 多链同号（A/B 残基号完全重叠） | **MATCH** 252/252 | 探测点落在第二链 B_4 |
| icode 插入码（60 号后插入 60A） | **MATCH** 127/127 | 60A 作为独立残基进入 pose，编号一致 |
| altloc 备用构象（30 号 CA 复制 altloc B） | **MATCH** 126/126 | Rosetta 与映射同视图（B 副本不入 pose） |
| noca 缺 CA | **MATCH** 125/125（修正后） | 证据链：Rosetta skip 缺失残基（ddg 日志）→ 剔除规则对齐 → 模拟 skip 后 pose 的 ddg 逐位一致；含缺失残基的文件直接喂 ddg 会崩（ROSETTA_CRASH），生产路径不存在该输入 |
| ligand_before 配体行在蛋白原子之前 | **MATCH** 126/126 | HETATM 前置不影响编号 |
| ligand_tail 配体 HETATM 追加尾部（模拟生产复合物拼接） | **MATCH** 126/126 | 蛋白编号不受尾部配体影响 |

**诚实边界（保留）**：零占用度原子（生产 relax 带 `-ignore_zero_occupancy`，突变输入为 relax 产物）
对突变阶段编号无影响，但预检阶段基于原始上传文件的映射仍属静态推断；本验证已覆盖
插入码/多链/altloc/缺主链/配体位置等全部"可构造差异"，剩余风险为已修复语义之外的非典型文件。

## 五、小规模真实流水线回归（1MC9，1 位点 ×19 突变）

- 独立任务：`output/task_20261121_105431c07d_1MC9_verify`（`create_task_dir` 创建，未触碰 22 个历史任务）。
  资源门控：启动时 load1=3.54/768 核。
- 命令（工具层直调，与生产同代码路径，不经 LLM）：
  `precheck_targets(protein.pdb,"67")` → 通过 `A_67(G)`；
  `run_cartesian_relax(nstruct=2)` → 【计算已挂起】；
  `run_saturation_mutagenesis("67", jobs=16, delay=1.0)` → 【流水线通关】。
- 结果（PHASE1 见下"结果补记"）。
- **可控失败模拟**（PASS）：独立任务 `task_20261121_1053176c67_FAILSIM`，手工放置 relax 产物 +
  `ROSETTA_BIN_DIR=/nonexistent` → 19 个计算全部 `[exit code -999]` → **终态 failed / NoSuccessfulMutants**，
  表头汇总文件存在但状态如实 failed，不虚报成功。

### 结果补记

- PHASE1（真实回归）：**PASS** —— `task_20261121_105431c07d_1MC9_verify`
  终态 `completed`（status_note="突变计算完成（19 个 .ddg）"），19/19 个 .ddg，`ddg_results.txt` 19 行数据，
  19 个 mut 日志全部 `[exit code 0]`，审计行 `# [编号映射] PDB A_67 -> Rosetta绝对行号 64 (G)（审计）`
  ——该位点正是地面真值验证 single case 的 MATCH 位点（真实 Rosetta 落点 == 映射预测），
  task_status.json 记录 runner_pids 与完整 manifest（chains/n_residues/target_sites/ddg_params/
  numbering_basis/gromacs_ready=false/rosetta_version=null）。
  注：首轮 phase1（修复前启动）因 runner NameError 失败，其任务目录如实保留为回归证据
  （两个夹具错误的 FAILSIM 模拟目录已删除，错误现场文本已留档本文 §三.2）。

## 六、仍需人工/真实环境验证的内容

1. **生产服务切换后的真实负载验收**：7779/7782 重启后按 CHANGES-rosetta.md §六 A-1~A-4、B、C 复走一遍
   （本轮已在独立目录完成等价验证，但生产端口本身未重启未验证）。
2. **MCP 客户端兼容性**：octop 平台（15 工具挂载）重连后工具列表 +1（新增 task_id 参数不改变旧调用）；
   建议重连后先 `list_tasks` 冒烟。断线重连后需重新 `create_task`/`set_workspace` 绑定（会话语义，旧全局方案在该场景同样不可靠）。
3. LangGraph 内存检查点不持久（维持原状，未做架构改造）——重启后对话历史不可恢复，接口已如实提示。

## 七、生产服务状态与切换步骤（本轮未执行）

**当前生产服务仍运行旧代码**（7779: pid 581739 @11-18；7782: pid 1641461 @11-19——重启后即加载本轮新代码，
无需任何文件操作）。切换、验证、回滚：

```bash
cd ~/projects/rosetta1
# 0) 切换前自查（已在本文档验证过，复核用）
~/.conda/envs/rosetta-agent/bin/python eval_v01_guardrails.py && \
~/.conda/envs/rosetta-agent/bin/python eval_p0_fixes_20261120.py && \
~/.conda/envs/rosetta-agent/bin/python eval_review_followup_20261121.py

# 1) 切换（单独一条命令，勿与 pkill 同行；pkill 模式用 [] 防自匹配）
pkill -f "rosetta_server[.]py"; sleep 2
nohup ~/.conda/envs/rosetta-agent/bin/python -u rosetta_server.py >> rosetta_server.log 2>&1 &
# MCP 同理（bash mcp_restart.sh 或按原启动方式重启 7782）
# 2) 验证
curl -s http://127.0.0.1:7779/api/tasks | head -c 300      # 预期返回任务列表含 "status" 字段
curl -s http://127.0.0.1:7779/api/host/status >/dev/null && echo OK
# 提交一个带错误位点的小任务 → 预期 400 且任务目录出现 task_status.json(status=failed)
# MCP：客户端重连 → list_tasks 含 status= → set_workspace("../../x") 应【执行报错】
# 3) 回滚（3 个文件回到 11-20 前状态即可恢复昨日行为；再重启两服务）
for f in rosetta_tools.py rosetta_mcp_server.py task_runtime.py; do cp -p "$f.bak-codexfix-20261120" "$f"; done
# 注：rosetta_server/agent/README 未在本轮改动，其回滚同 CHANGES §八
```

## 八、本轮改动文件清单（相对 11-20 状态）

| 文件 | 改动 |
|---|---|
| `rosetta_mcp_server.py` | 会话隔离（_SESSION_TASKS/ctx 注入/显式注册）、管理工具 ctx 化（.bak-review-20261121 备份） |
| `task_runtime.py` | flock 并发写、_pid_alive marker 防复用、缺主链残基剔除（地面真值修正）、空壳汇总 failed、settle 对齐 |
| `rosetta_tools.py` | runner 补 score_path/wt_pdb_path（回归修复）、_RUNNER_STATUS_SRC 加 flock+_finalize_status 守卫、execute OSError 兜底、0 成功突变→failed |
| `eval_p0_fixes_20261120.py` | A8 反转（缺主链剔除）、A6 构造补全、C4c 用真实 stub runner、C4d 空壳、D2 加 AST 未定义名检查（76 项） |
| `eval_review_followup_20261121.py` | 新增：并发/PID 复用/晚到守卫/空壳 14 项 |
| `verify_numbering_groundtruth_20261121.py`、`verify_numbering_20261121/` | 新增：编号地面真值验证（脚本+全部输入输出留档） |
| `pipeline_regression_20261121.py` | 新增：真实流水线回归+失败模拟脚本 |
| `REVIEW-followup.md` | 本文档 |

备份：`*.bak-review-20261121`（本轮改动前状态）；`*.bak-codexfix-20261120`（11-20 全部改动前状态）仍在。
