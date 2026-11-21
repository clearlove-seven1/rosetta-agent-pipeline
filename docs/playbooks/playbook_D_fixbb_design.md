# Playbook D — 定点改造(fixbb 固定骨架设计)

> 引用方式:`search_rosetta_docs` 检索"定点设计/fixbb"优先命中;与 Playbook A/B/C 组合使用。
> 铁律同前:计划是建议;科学参数由工具默认值锁定;设计结果是**计算候选,不是最终答案**。

## 适用条件

- 用户明确要求"把某位点改成特定氨基酸/做定点改造/设计组合",而非全部 19 种饱和扫描(饱和 → Playbook A)。
- 前提:工作区已有弛豫产物(`cartesian_relax/wt_relaxed.pdb` 或 `complex_rosetta_ready.pdb`)——没有先走 Playbook A 步骤 2。

## 步骤与默认参数

| 步骤 | 工具 | 默认参数 | 说明 |
|---|---|---|---|
| 1 前置 | `suggest_plan` / Playbook A 步骤 1-2 | nstruct=2 | 确保弛豫产物在位 |
| 2 定点设计 | `run_fixbb_design(positions="A_21,A_23", nstruct=5)` | nstruct=5(设计克隆数);骨架固定;侧链采样 ex1/ex2 由工具内置 | positions 格式 `链_PDB编号`,逗号分隔;放开为全部 20 种氨基酸由 packer 选优 |
| 3 查看结果 | `read_file` → `fixbb_design/design_summary.txt` | — | 各克隆 total_score、最优克隆、逐位点氨基酸变化 |
| 4 解读 | 报告口径见下 | — | 与 Playbook A 的 ΔΔG 口径**不同**:这里是设计打分,不是突变前后差值 |

## 停止条件(必须停下问用户)

- 无弛豫产物 → 先执行 Playbook A 步骤 2,不得直接设计;
- positions 格式错误/位点不存在 → 明确报错,等用户更正;
- 用户同时要饱和扫描 + 定点设计 → 先确认是两条独立任务,不要混在一条流水线里。

## 口径与限制(fixbb 官方三条 limitation,必须如实转述)

1. 固定骨架设计不能保证新序列真实折叠;
2. 随机性导致各克隆结果不同(不同 nstruct 种子结果不同);
3. 打分函数不完美——**设计结果是计算候选,需要实验验证**。

## 与其他 playbook 的关系

- Playbook A(饱和扫描)→ 找出敏感位点;Playbook D → 对已定位点做定向改造;
- 带配体体系 → D 自动携带配体参数(与 A/B 相同的 `-extra_res_fa` 机制);
- 设计产物如需 MD 观察突变体结构 → 突变体结构导出走 Playbook A 的 `export_structures` 通道。
