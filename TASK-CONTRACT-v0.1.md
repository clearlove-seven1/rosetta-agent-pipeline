# TASK-CONTRACT-v0.1 — rosetta1 任务清单下游对接契约（阶段 3）

> 2026-11-21 制定。目标:让 rosetta1 产出的每个任务都带一份**结构化、可校验、可追溯**的任务清单
> (`task_status.json`),下游执行器(当前预留:ros-gro workbench.gromacs 确定性执行器)据此决定
> 能否消费、如何消费;**缺少必要信息时下游必须明确拒绝,不得猜测补齐**。
> 可执行校验器:`task_runtime.contract_check(task_dir)`(只读)。范围:本轮只做 Rosetta 侧;
> GROMACS 侧(~/projects/gromacs)由另一会话管辖,其接入契约留待阶段 3 后续对齐。

## 1. 任务身份

| 字段 | 说明 |
|---|---|
| `task_id` | 全局唯一,格式 `task_<YYYYMMDD>_<HHMMSS+4位hex>_<标签>`;同秒同标签不覆盖(`create_task_dir` 独占建目录) |
| `created_at` / `updated_at` | ISO 本地时间 |
| `manifest.research_id` | **预留**(v0.1 不写入):未来"研究任务"聚合多个子任务时的关联字段,下游不得依赖 |

任务目录结构(与现有一致):`protein.pdb`(原始上传)、`cartesian_relax/`、`saturation_mutagenesis/`、
`summary/`、`task_status.json`、`rosetta_tasks.txt`(含 `# [编号映射]` 审计行)。

## 2. 状态机(7 态,语义唯一)

| 状态 | 语义 | 判定来源 |
|---|---|---|
| `created` | 已创建,未开算 | 服务端 |
| `running` | 计算进行中(runner 存活,按 /proc cmdline marker 判定) | 服务端/查询 |
| `completed` | **预期计算全部成功**(成功数==计划数,且 ≥1) | runner 收尾/settle/state_effective 三处一致 |
| `partial` | **部分成功**(0<成功<计划),失败项看 mut_*.log 的 `[exit code]` | 同上;**仅当 `manifest.mutations_planned` 已知** |
| `failed` | 失败(0 个成功/预检失败/异常) | 同上 |
| `interrupted` | 已确认中断(runner 退出且无汇总;不自动重试) | state_effective 证据推断 |
| `待核实`(running+无进程记录) | 无法可靠判断,如实标注 | state_effective |

`partial` 为 v0.1 新增对外可见状态;旧任务(无 `mutations_planned`)保持旧语义(completed)。
注:此状态描述**计算状态**;LangGraph 对话检查点是内存态,重启不可续(由接口层如实说明)。

## 3. 结构与编号身份(manifest 必填集)

| 字段 | v0.1 要求 | 说明 |
|---|---|---|
| `input_structure_path` | 必填 | 相对任务目录的原始输入(如 `protein.pdb`) |
| `input_structure_sha256` | 必填 | **原始上传文件**哈希(提交时计算);下游可自行复核 |
| `chains` / `n_residues` | 必填/参考 | 清洗后结构(`complex_rosetta_ready.pdb`)的链与残基数 |
| `numbering_basis` | 必填 | 编号映射基准结构说明(固定为清洗后结构) |
| `numbering_audit` | 参考 | 审计行位置(`rosetta_tasks.txt` 中 `# [编号映射]`) |
| 结构异常 | 参考 | `build_numbering` 的 anomalies 记录于工具返回文本(v0.1 未单独落盘,留待 v0.2) |

## 4. 突变身份

| 字段 | v0.1 要求 |
|---|---|
| `target_sites` | 必填;四元组文本 `链_编号(插入码)(WT=X)`,如 `A_67(WT=G)`;插入码保留(`A_100A`) |
| `mutations_planned` | 必填;计划突变总数 |
| `mutations_succeeded` / `mutations_failed` | runner 收尾回填;succeeded=实际 .ddg 数 |
| `ddg_program` / `ddg_params` | 必填;程序名与旗标(逐字,含 iterations/cartesian/bbnbrs/weights/min_type/extra_res_fa) |
| `rosetta_version` | runner 从计算日志 `core.init: Rosetta version:` 捕获;取不到留 null(不虚构) |
| 明细数据 | `summary/ddg_results.txt`(ΔΔG=REU,min(MUT)-min(WT));逐任务日志 `mut_*.log` |

## 5. 配体(存在时)

`input_summary.ligand`(bool)、`parameterize_ligand/*.params` 产物、`ddg_params.extra_res_fa`(是否携带配体参数)。
Rosetta 配体参数 **不可假定** 对 GROMACS 拓扑通用;下游消费需自行参数化并记录来源(v0.1 仅如实登记)。

## 6. 下游消费规则(拒绝语义)

下游执行器消费前必须调用 `contract_check(task_dir)`:

- `ok=False`(缺必填字段 / 生效状态非 completed|partial)→ **拒绝执行**,向用户出示 `missing` 清单;
- `legacy=True`(无 `contract_version` 的旧任务)→ 字段不全属预期,需人工确认后才能消费;
- `gromacs_ready=False` → 结构未经 MD 准备,**不得**直接作为 GROMACS 输入(需结构与参数适配层);
- 哈希不一致 warning → 输入文件疑似被改动,须人工核对后再消费;
- 下游不得依据聊天上下文/文件名猜测链、编号、配体或电荷。

## 7. 与 ros-gro Workbench manifest 0.2 的对齐(参考,未改其代码)

ros-gro `workbench/manifest.py`(0.2)记录:输入结构与哈希、软件与参数、逐阶段产物哈希、状态机
(参数列表/进程组/退出码+预期产物)。本契约与其同构点:输入哈希、参数逐字登记、产物路径、诚实终态;
差异点:本契约多"编号映射审计"与 `mutations_*` 计数,ros-gro 多"逐阶段哈希断点恢复"。阶段 4 打通时
由调用方做字段映射,任一侧不得静默改语义。

## 8. 兼容性

- 旧任务目录(11-21 之前/含 11-21 早间 verify 任务)无 v0.1 字段:`contract_check` 判 `legacy=True`、
  `missing` 如实列出——**不回填、不改历史状态文件**;
- 对外接口(`/api/tasks`、MCP `list_tasks`)状态值新增 `partial`,旧客户端按未知字符串处理即可。
