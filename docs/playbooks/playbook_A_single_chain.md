# Playbook A — 单链蛋白突变扫描(稳定性 ΔΔG)

> 引用方式:`suggest_plan` 工具输出本文件指引;`search_rosetta_docs` 检索时计划手册优先命中。
> 铁律:计划是建议;**科学参数由工具默认值锁定**,对话中 LLM 不得修改默认旗标;
> 需要调整时由用户明确提出,执行前复述确认。

## 适用条件

- 单条蛋白链、标准 20 氨基酸(水分子 HOH/WAT 忽略,其他 HETATM → 走 Playbook B)。
- 目标:指定位点的饱和/定点突变稳定性初筛(ΔΔG,单位 REU)。

## 前置条件(缺任何一项 → 停下询问,不猜测)

- [ ] PDB 结构文件(7779 上传 / MCP `create_task`)。
- [ ] 目标位点用 **PDB 晶体学编号**:单链可写 `76`;多链同号必须写 `A_76`;插入码写 `100A`。
- [ ] 非标准残基(MSE 等)上的位点会被预检直接拒绝——先换位点或预处理。

## 步骤与默认参数

| 步骤 | 工具 | 默认参数(锁定) | 说明 |
|---|---|---|---|
| 1 预检 | 提交时自动 `precheck_targets` | — | 位点不存在/歧义/非标准残基 → 立即报错,不烧算力 |
| 2 弛豫 | `run_cartesian_relax` | nstruct=2;ref2015_cart;lbfgs_armijo_nonmonotone | 产物 `cartesian_relax/wt_relaxed.pdb`(多构象择优) |
| 3 突变扫描 | `run_saturation_mutagenesis` | iterations=3;cartesian;bbnbrs=1;jobs=16;delay=1.0 | 每位点 19 个突变;`export_structures=True` 可归集突变体结构 |
| 4 解读 | `summary/ddg_results.txt` | ΔΔG = min(MUT)−min(WT)(REU) | 负值=可能稳定化,正值=可能去稳定;**不能当结合自由能或实验亲和力** |
| 5 验收 | `task_status.json` + `contract_check` | — | 终态 completed/partial/failed;`gromacs_ready=False`(未做 MD 准备) |

## 参数口径(为什么是这个值)

- `nstruct=2`:弛豫取 2 个构象择优;改大只增加耗时,结果口径不变。
- `iterations=3`:cartesian_ddg 每突变 3 次迭代,ΔΔG 取 min 差值(口径写死在表头,防误读)。
- `jobs`/`delay`:纯并发调度参数,可按服务器负载调整;与科学结果无关。

## 停止条件(必须停下问用户)

- 位点解析失败/多链同号未带链 → 报错并列出候选链,等用户选择,不得提取数字兜底;
- 弛豫无产物(NoRelaxStructure) → 检查输入 PDB 完整性;
- 0 个成功突变(空壳) → 终态 failed,逐个 `mut_*.log` 查 `[exit code]`;
- 用户要求修改科学旗标 → 说明默认值出处,请用户明确决策,LLM 不自行修改。

## 产物与人工接力

- 突变体结构:`export_structures=True` → `mutant_structures/<wt>_<位点>_<突变>/`(内容级落点核对);
- 下游 MD:把选中突变体 PDB 交给用户自己的 GROMACS 会话(人工接力,本系统不做系统级整合);
- 编号审计:任务目录 `rosetta_tasks.txt` 中 `# [编号映射]` 注释行 = PDB 编号 ↔ Rosetta 行号真值。

## 相关手册

- 定点改造(只引入特定氨基酸,非全部 19 种) → `playbook_D_fixbb_design.md`;
- 蛋白+配体 → `playbook_B_protein_ligand.md`;多链复合物 → `playbook_C_complex.md`。
