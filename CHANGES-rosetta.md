# CHANGES-rosetta.md — 正确性 / 任务隔离 / 可靠性修复（2026-11-20）

> 本次工作按 codex 建议清单执行：先核实、后修复、全程不跑 Rosetta 真实计算、不重启服务、不提交 Git。
> **工作目录判定**：codex 建议中全部目标文件（rosetta_agent.py / rosetta_server.py /
> rosetta_mcp_server.py / rosetta_tools.py / README.md）只存在于 `~/projects/rosetta1`，
> 故本次在 **rosetta1** 实施（用户口述的 `~/projects/rosetta` 为旧单数项目，无
> rosetta_server.py / rosetta_mcp_server.py，未做任何改动）。
> 原文件已备份为 `*.bak-codexfix-20261120`；文中行号均指**修复前**（即 .bak 文件）中的位置。

---

## 一、实际确认的问题与证据

### P0-1 残基编号与突变位点正确性（全部属实）

| # | 问题 | 证据（修复前行号） | 后果 |
|---|---|---|---|
| 1 | **插入码被丢弃**：`line[22:26].strip()` 只取数字，`100A` 变成 `100` | rosetta_tools.py.bak:170-176 | `100` 与 `100A` 互相覆盖 strict 键（后写覆盖），纯数字键先写保留——双向映射自相矛盾 |
| 2 | **多链同号静默取第一链**：纯数字键"首个链先到先得" | rosetta_tools.py.bak:174-176；server 预检同款 rosetta_server.py.bak:129 | 二聚体上突变错链，结果无告警 |
| 3 | **退化匹配（提取数字）**：`re.search(r'\d+', p)` 兜底 | rosetta_tools.py.bak:469-472 | `B76`→静默落到某链 76 位；`76B`→静默落到 76 位；错算无告警 |
| 4 | **只数 CA ≠ Rosetta pose**：altLoc 双 CA 重复计数；缺 CA 残基漏号；非标准残基(MSE)处理与清洗不一致；多模型 PDB 全部拼接计数 | rosetta_tools.py.bak:168-177（对照清洗 :354-362） | 位点整体偏移；预检(用原始 PDB)与执行(用清洗后 ready PDB)规则不同 → 预检通过、弛豫后才报错（白烧算力） |
| 5 | **四个入口四套解析**：get_rosetta_numbering(:204 只收纯数字)、突变工具(:456)、server 预检(:117-133)、fixbb(:1761-1777) 各自为政 | — | 同一输入在不同入口行为不同 |
| 6 | get_rosetta_numbering 默认 `chain_id="A"`：结构无 A 链时"未找到"，但多链同号时静默匹配 A 链 | rosetta_tools.py.bak:184,215 | 与 #2 同源 |

### P0-2 任务隔离 / 任务 ID / 路径安全（属实，部分此前已修）

| # | 问题 | 证据（修复前行号） | 状态 |
|---|---|---|---|
| 1 | **FastAPI 任务 ID 秒级时间戳+未清洗文件名**：同秒同文件重复提交 → `makedirs(exist_ok=True)` 复用目录互相覆盖 | rosetta_server.py.bak:370-373 | 属实，本次修复 |
| 2 | **Gradio/CLI 分钟级时间戳**，同分钟必然复用目录 | rosetta_agent.py.bak:847,966 | 属实，本次修复 |
| 3 | **MCP 分钟级时间戳 + task_label 未清洗**：`task_label="../../evil"` 直接拼路径可穿越出 output/ | rosetta_mcp_server.py.bak:174-177 | 属实，本次修复 |
| 4 | **MCP `_ACTIVE_TASK` 进程级全局**：SSE/HTTP 模式下多客户端交错 → 工具串任务 | rosetta_mcp_server.py.bak:101-110,212 | 属实，本次修复（显式 task_id 参数 + 严格校验的适配层） |
| 5 | **MCP set_workspace 仅 isdir 校验**：`../../rosetta1` 穿越成立 | rosetta_mcp_server.py.bak:259 | 属实，本次修复 |
| 6 | rosetta_tasks.txt 整行 shell 串（含重定向）+ runner `shell=True`：路径含空格断裂 | rosetta_tools.py.bak:497-504,601,608 | 属实，本次修复（JSON 参数列表） |
| 7 | 弛豫 runner 内部也是 shell 串 | rosetta_tools.py.bak:384-390 | 属实，本次修复 |
| 8 | Gradio/CLI PyMOL 分离配体为 shell 串（`cmd = f"{pymol_bin} -c -d '{pymol_cmds}'"`） | rosetta_agent.py.bak:866-869,985-988 | 属实，本次修复（7779/MCP 侧在 11-14 已改列表参数，本次对齐） |
| 9 | /files /download 路径遍历 | rosetta_server.py.bak:89-103 已有 `_safe_task_dir` | **已由 2026-11-14 会话修复**，本次仅加 realpath 符号链接防护 |
| 10 | `..`/符号链接绕过：`_safe_task_dir` 只查字符串，`output/task_x → /etc` 软链可穿透 | — | 本次修复（realpath 包含性校验） |

### P1 任务状态与失败处理（属实）

| # | 问题 | 证据（修复前行号） |
|---|---|---|
| 1 | 事件仅存内存 `TASKS={}`，重启即失；list 只剩文件系统信息 | rosetta_server.py.bak:66,423,511 |
| 2 | LangGraph `MemorySaver` 内存检查点，重启后续聊不可恢复（与续聊 404 提示一致但无替代方案） | rosetta_agent.py.bak:24,774-775 |
| 3 | `_consume` 无条件 `done ok=True`：工具全失败也"正常收场"，最终结果不区分失败/部分完成 | rosetta_server.py.bak:246 |
| 4 | 流水线失败仍进对照与总结：`workflow.add_edge("pipeline","skempi_eval")` 无条件连边（skempi_eval 有产物存在性检查兜底，不至于谎报数据，但 summarize 不区分成败） | rosetta_agent.py.bak:761-763 |
| 5 | 无持久状态文件；重启后 running/completed/failed 无从查证 | — |

### P1 文档口径（属实）

- README.md.bak:3 开篇宣称"自动接 SKEMPI/PDBbind 做**可信度评估**"+disorder"避免浪费计算"——与 §0/§5 默认关闭口径矛盾（§2:69 仍写"打 disorder **概率**，≥0.5 视为无序"——正是"开头免责、正文相反"的反例）。
- README.md.bak:28 "12 原子工具"过时（实为 16）。
- README.md.bak:55-59 图拓扑把 disorder_check 画在 step1 与 step2 之间，实际在 step2 之后。
- README 无 7779 平台（rosetta_server.py 整个平台未提及）、无状态/日志定位说明。

### P2 整合接口（此前不存在）

- 无结构化结果清单；cartesian_ddg 以 `-ddg:dump_pdbs false` 运行，**不产出突变体结构**（本就不该假设可喂 GROMACS，见 §7）。

---

## 二、修改的文件与原因

| 文件 | 类型 | 内容 |
|---|---|---|
| `task_runtime.py` | **新增（公共层）** | ①任务 ID（标签白名单清洗 + 秒级时间戳内嵌 4 位随机 hex + `makedirs(exist_ok=False)` 独占裁决，同标签同秒不再覆盖，分段结构保持 `task_<d>_<t>_<label>` 与 skempi_eval 的 `split("_")[3:]` 解析兼容）；②`validate_task_dir`（禁分隔符/前缀 task_/realpath 包含性，封死穿越与软链外指）；③**位点解析唯一实现**：`build_numbering`（残基身份=(链,编号,插入码,WT)四元组；按残基键首个原子行计数——缺 CA 残基仍占号；altLoc 只认 ' '/'A'/'1' 与清洗一致；非标准残基剔除；多模型只取第 1 个；全部计入 anomalies）+ `resolve_targets`（歧义明确报错列出候选链、错误链/不存在/歧义格式/引号一律报错、**无任何数字提取兜底**）+ `precheck_targets`；④持久状态 `task_status.json`（原子写，status∈created/running/completed/failed/interrupted，runner 进程存活+产物证据推导生效状态，判不了标「待核实」；内含轻量 manifest）；⑤`settle_task_after_stream`（流结束收口：完成/失败/后台仍在运行 三分） |
| `rosetta_tools.py` | 修改 | `_build_rosetta_numbering` 改为兼容包装（委托公共层）；`get_rosetta_numbering`（默认链改自动判定，歧义报错，输出异常说明与"静态推断"诚实边界）；`run_saturation_mutagenesis`（位点走共享解析；任务行改 JSON argv 参数列表；mutfile 不变；auto_clean 兼容插入码；runner 无 shell、退出码写进 mut_*.log、结束写 completed/failed；记录 runner_pids 与 manifest）；`run_cartesian_relax`（清洗加"只取第 1 个 MODEL"；runner 参数列表+退出码；状态落盘）；`run_fixbb_design`（位点走共享解析）。**所有 Rosetta 科学旗标逐字未动**（relax/cartesian_ddg 参数与旧 shell 串完全一致，测试断言核对） |
| `rosetta_server.py` | 修改 | 任务创建走 `create_task_dir`（不覆盖）+ 状态落盘（created→running→completed/failed，预检失败也留档）；预检委托公共层（预检=执行同一规则）；`_safe_task_dir` 加 realpath；`/api/tasks` 增加 `status/status_note`；新增 `GET /api/tasks/{id}/status`；`task_message` 重启后可续聊但明示"事件不可回放、对话为新上下文"；done 事件附加 `outcome`（前端忽略未知字段，**响应结构向后兼容**） |
| `rosetta_mcp_server.py` | 修改 | `create_task` 走 `create_task_dir`+状态落盘；`set_workspace` 严格校验；**每个原子工具新增可选 `task_id` 参数**（`__signature__` 注入 schema，单次调用显式绑定任务，多客户端不再依赖共享"当前任务"）；`list_tasks` 附持久状态 |
| `rosetta_agent.py` | 修改 | Gradio/CLI：任务 ID 走公共层；**新增与平台相同的提交前预检**；PyMOL 改参数列表；流结束状态收口；`package_results` 校验 task_id |
| `README.md` | 修改 | 开篇/§2/§4.4/§5 口径统一（disorder=默认关闭的探索性信号、对照默认未评测、4.4 表标注历史）；§1 结构补 rosetta_server/task_runtime/测试文件（16 工具）；§2 图拓扑修正；§3 编号铁律补多链/插入码/诚实边界；§6 新任务目录结构（task_status.json）；§8 新增 8.5 平台章节；§9 排错补状态定位/单位口径行 |
| `eval_p0_fixes_20261120.py` | **新增（测试）** | 75 项纯 Python 测试（见 §四） |

未动：`static/`（前端，接口向后兼容故无需改动）、`viz_render.py`、`plot_agent/`、`eval_v01_guardrails.py`、`run_cli.sh`、`.env*`、`rosetta_manuals/`。

---

## 三、对现有接口 / 输入格式 / 默认参数的影响

**兼容（旧用法继续可用）**：
- 位点格式 `76`（单链或全结构唯一时）、`A_76` 照常工作；旧任务目录 `task_YYYYMMDD_HHMM_x` 可被全部接口识别；`get_rosetta_numbering(pdb, targets)` 不带链的调用照常；MCP"create_task→set_workspace→原子工具"编排照常；7779 前端与全部 API 响应字段只增不改。
- 科学参数零变化：relax / cartesian_ddg 旗标、迭代数、权重、错峰逻辑、排序与 `ddg_results.txt` 表头逐字不变。

**有意的行为变化（旧行为有风险，属修复）**：
1. 多链同号的无链位点输入（如二聚体传 `76`）→ 从"静默取第一链"改为**明确报错并列出候选链**（`A_76`、`B_76`）；单链/唯一位点不受影响。
2. 错误链名（`Z_76`）、`B76` 类歧义格式、带引号输入 → 从"提取数字兜底/静默丢弃"改为**报错**。
3. 插入码 `100A` → 从"当成 100"改为按 100A 精确匹配。
4. 非标准残基（如 MSE）位点 → 预检阶段即拒绝（旧版预检接受、弛豫后才失败）。
5. 同标签同秒（7779）/同分钟（Gradio/CLI/MCP）重复提交 → 从"覆盖共用目录"改为各自独立目录。
6. `get_rosetta_numbering` 默认链从 `"A"` 改为自动判定：单链任务无感；多链同号必须显式给链（原为静默匹配 A 链）。
7. MCP 原子工具 schema 各多一个**可选** `task_id` 参数（老客户端不传不受影响）。
8. `rosetta_tasks.txt` 行格式从 shell 串改为 JSON 行（`run_ddg.py` 同步改；**旧任务的 run_ddg.py 不受影响**——runner 与任务文件成对生成于同一任务目录）。

**对外行为可见变化**：任务 ID 出现随机后缀（如 `task_20261120_224536f3a1_1MC9`）；`/api/tasks` 列表与 MCP `list_tasks` 多出 status 字段；任务目录内出现 `task_status.json`；SSE `done` 事件多出 `outcome` 字段。

---

## 四、新增与运行的测试及结果

**`eval_p0_fixes_20261120.py`（75 项，纯 Python，不跑 Rosetta/LLM）**，服务器实跑结果：**75/75 通过**。
覆盖：A 编号映射 21 项（连续/缺失/插入码/多链歧义/错误链/altLoc/MSE/缺 CA/多模型/预检与执行一致性/6 类非法 token 全拒）；B 任务 ID 与路径 12 项（同标签不覆盖/分段兼容 skempi/危险标签清洗/7 类非法 ID/符号链接外指/旧格式兼容/空格路径 JSON 保真）；C 持久状态 11 项（生命周期/跨进程"重启"读取/损坏与缺失文件/多任务隔离/进程退出+产物→interrupted 或 completed/存活进程→running/失败与完成收口）；D 执行文件生成 17 项（JSON 任务行 38 条计数/无 shell 元字符/ddg 旗标逐字一致/mutfile 绝对行号/状态+manifest 落盘/两个生成 runner py_compile 通过/清洗规则/fixbb 拒绝错误链）；E 入口一致性 14 项（各入口委托公共层/旧退化正则已移除/科学开关默认关闭/口径护栏仍在）。

**既有回归 `eval_v01_guardrails.py`**：26/26 通过（修复后代码上运行，科学口径护栏无回归）。
**py_compile**：6 个 py 文件全部通过；生成的 run_ddg.py / run_relax_parallel.py 亦单独编译通过。

---

## 五、未修复 / 无法在本机验证的风险（如实列出）

1. **静态映射 ≠ 真实 Rosetta pose 编号的极端情形**：零占用度全残基（`-ignore_zero_occupancy`）、Rosetta 对特殊残基的加载差异，无法从 PDB 文本静态确证。已按提示要求"不宣称扫描=pose"：工具输出与 README 均注明以任务目录 `rosetta_tasks.txt` 的 `# [编号映射]` 审计行 + 实际 ddg 日志为准。**需服务器验收 A-3**。
2. **真实长跑回归**：JSON 任务行 runner 与状态收口需一次真实弛豫+突变验证（预期与旧版同参同速）。**需服务器验收 B**。
3. **FastMCP schema 注入**：`__signature__` 追加可选参数在当前 mcp SDK 版本下需实测（沙箱已验证签名生成逻辑，未起真实 MCP 会话）。若个别客户端工具列表缓存旧 schema，需重连。**需服务器验收 C-2**。
4. **状态并发写**：单任务内服务线程与 runner 子进程同时更新 task_status.json 为"最后写者胜"，极小概率丢一次并发更新（最小实现，字段互不重叠，可接受）；跨任务无锁竞争不存在。
5. **auto_clean 对含多字符链 ID 的历史 .ddg 文件**：排序键取前导数字，与旧行为一致；空链等畸形文件名依旧被跳过（原有行为，未放大）。
6. `_consume` 的 done `outcome` 字段前端暂未展示（前端未改；状态已在 `/api/tasks` 与 `/status` 端点可见）。前端展示 outcome 可作为后续小改进。
7. Gradio/CLI 的 LangGraph MemorySaver 仍是内存检查点（本轮不做持久化图检查点——按提示"不为自动恢复重写整个系统"，已用状态文件+诚实提示覆盖可查询性）。

---

## 六、服务器验收步骤（Linux/Rosetta 环境已改好，需真实环境确认）

```bash
cd ~/projects/rosetta1
PY=~/.conda/envs/rosetta-agent/bin/python

# 0) 静态检查（已通过，复核用）
$PY -m py_compile task_runtime.py rosetta_tools.py rosetta_server.py rosetta_mcp_server.py rosetta_agent.py
$PY eval_v01_guardrails.py          # 预期 26/26
$PY eval_p0_fixes_20261120.py       # 预期 75/75

# A) 编号映射真实 Rosetta 验收（核心）
# A-1 单链纯蛋白小体系（如 1MC9 链 A）：提交 3 个位点，弛豫完成后
head -3 output/<task>/saturation_mutagenesis/rosetta_tasks.txt
#    预期：# [编号映射] PDB A_4 -> Rosetta绝对行号 4 (X)（审计） 行
#          且 JSON 行中 mutfile 名与 -out:prefix 与审计行一致
# A-2 打开任一 mut_*.log 确认 cartesian_ddg 实际读入的残基编号与审计行一致（无 "cannot find residue" 类报错）
# A-3 多链同号输入（如 4HHB）：7779 提交 "37" 预期 400 报错列出 A_37/B_37…；提交 "A_37,B_37" 预期通过
# A-4 插入码结构（如有 100A 的 PDB）：预检/执行均按 100A 匹配

# B) 真实流水线回归（与 11-16 的 1MC9 50 位点跑同参对比）
#    预期：950 突变量级下耗时与旧版同量级；summary/ddg_results.txt 表头与排序不变；
#    任务结束后 task_status.json = completed，manifest 含 target_sites/mutations_planned
#    中途 kill 服务进程后再查询 /api/tasks/{id}/status：
#    runner 存活 → running；runner 已死且无汇总 → interrupted（待核实）

# C) 平台与 MCP
# C-1 重启 7779（手动），上传含错误位点的 PDB+位点 → 预期立即 400 且任务目录留有 failed 状态文件
# C-2 重启 MCP（bash mcp_restart.sh），客户端重新连接：
#     list_tasks 应显示 status=…；任选原子工具传显式 task_id 应作用于该任务；
#     set_workspace("../../rosetta1") 应返回【执行报错】
# C-3 服务重启后（不重提任务）GET /api/tasks/{id}/status 预期返回持久状态
```

**本次未重启任何服务**：7779（pid 581739）与 7782（pid 1641461）仍在运行旧代码，验收时按上面步骤手动重启生效。

---

## 七、与 GROMACS 整合前还缺什么

1. **突变体结构产出**：cartesian_ddg 现以 `-ddg:dump_pdbs false` 运行，不导出突变体 PDB（旧版亦然）。整合需给选点突变单独跑 `fixbb`/`cartesian_ddg dump_pdbs=true` 并落盘结构——本轮未新增未验证的 MD 联动（按约束）。
2. **结构与参数适配**：ros-gro 的 `workbench/gromacs.py` 只接受**单链、干燥、全标准残基** PDB（--protein/--chain/--label/--ns）；rosetta1 产物多为复合物/多链/带配体，需结构筛选与预处理层。
3. **可复用件已确认（只读检查 ~/projects/ros-gro）**：`workbench/numbering.py`（编号/映射）、`workbench/manifest.py`（run manifest 0.2 同构）、`workbench/gromacs.py`（参数列表/状态机/断点恢复）、`workbench/resume.py`——rosetta1 新增的 task_status.json manifest 字段（input_structure/numbering_basis/chains/target_sites/ddg_params/gromacs_ready=false）已按可对接形状落盘，取不到的（rosetta_version）留空注明。
4. 任务级跨仓库引用：整合调用方需把 rosetta1 任务目录路径传给 ros-gro CLI；两侧 manifest 字段名对齐可在整合轮完成。

---

## 八、快速回滚

```bash
cd ~/projects/rosetta1
for f in rosetta_tools.py rosetta_server.py rosetta_mcp_server.py rosetta_agent.py README.md; do
  cp -p "$f.bak-codexfix-20261120" "$f"
done
rm -f task_runtime.py eval_p0_fixes_20261120.py   # 可选：公共层与新测试一并移除
```
