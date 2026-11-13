# PROGRESS — Protein Design Workbench

> 依据 PROTEIN_WORKBENCH_PLAN.md §11 维护。**只有验收证据齐全才标"已完成"。**
> 当前主线：在 rosetta-agent 基础上渐进升级；学习目录（learn_*/alphafold_learning 等）不在本计划范围。

## 进度总表

| 阶段 | 状态 | 计划日期 | 实际日期 | 证据 / 文件 | 阻塞 |
|---|---|---|---|---|---|
| v0.0 基线盘点 | **待验证** | 2026-11 第 1 周 | 2026-11-12 | `docs/current_state.md`；本机环境实测数据 | 等用户核对 §9 待确认清单 |
| v0.1 科学纠偏 | **待验证** | 2026-11 第 2 周 | 2026-11-12 | git diff（4 文件）+ `eval_v01_guardrails.py` 26/26 通过 | 等用户抽查验收 |
| v0.2 CLI MVP | **进行中（核心已通）** | 2026-11 第 3-4 周 | 2026-11-12 | `workbench/` 包 + 单测 24/24 + **1UBQ G76E 真实跑通**（ΔΔG=+1.086 REU，报告/manifest 齐全） | 等用户验收；批量驱动与 README 用例未写 |
| v0.3 可靠运行 | **待验证** | 2026-11 第 5 周 | 2026-11-12 | 中断/恢复实测（interrupted→resume→ΔΔG 一致）、空格路径实测、单测 16+24+26 全过 | 等用户验收 |
| v0.4 科学评测 | **进行中（真实数据已出数，异常待排查）** | 2026-11 第 6-7 周 | 2026-11-13 | S669 已接入（669 条，provenance §6）；2HBB 批量 26 突变；首次真实评测 **r=-0.595 / ρ=-0.592 / n=25** | **负相关与文献矛盾，排查前不得下结论**（见 v0.4-02 日志） |
| v0.5 GROMACS | 未开始 | 待填写 | — | **实测：已装 2026.1（/opt/software，GPU CUDA 构建）**，cuFFT 11 链接断裂待修 | 修复方案待用户选择（§9） |
| v0.6 Agent / 界面 | 未开始 | 待填写 | — | — | — |
| v1.0 发布候选 | 未开始 | 待填写 | — | — | — |

**状态定义**：未开始 / 进行中 / 阻塞 / 待验证 / 已完成。

---

## 工作日志

### 2026-11-12 ｜ 任务：v0.0-01 基线盘点
- **本次目标**：只读盘点代码、环境、现状与风险，不修改任何逻辑。
- **修改文件**：无（纯只读检查）。新增 `PROTEIN_WORKBENCH_PLAN.md`（规划文档入仓）、`docs/current_state.md`（盘点报告）、`PROGRESS.md`（本文件）。
- **实际完成**：
  - 环境实测：Ubuntu 22.04.5 / 2×EPYC 9965（768 线程）/ 502G 内存 / 磁盘余 796G / 2×RTX PRO 5000 72GB（驱动 580.173.02）/ 无 Slurm / **gmx 未安装** / rosetta-agent env（Py 3.10.20）完好 / Rosetta 与 PyMOL 路径验证可用 / `molfile_to_params.py` 在位。
  - P0 四项科学问题全部在**实际工作副本**中定位核实（ESM-2 无 mask 取 log-prob 归一化、PDBbind min(total_score) vs 单 Kd 自动评级、cartesian_ddg 常规模式跑复合物对照 SKEMPI 结合 ΔΔG、SKEMPI key 丢链），位置与证据见 `docs/current_state.md` §4。
  - P1 工程问题定位：shell=True 拼接 ≥13 处、后台 Popen fire-and-forget + 文件存在即"完成"、MCP 单任务全局状态。
  - 梳理 4 个入口与调用链；确认当前**不存在无 LLM 的执行路径**（v0.2 必须补）。
- **验证命令与结果**：`git rev-parse HEAD`=1945a14、`git status --short` 干净；`uname`/`lscpu`/`free`/`df`/`nvidia-smi`/`command -v gmx`（无结果）/ conda env python 版本 / Rosetta bin 与脚本 `ls` 验证。
- **未验证**：Rosetta 版本号、ESM-2 推理实测、SKEMPI 多突变解析边界、binding_eval 跳过的精确路径、Gradio/MCP 运行时行为（见报告 §10）。
- **发现的问题 / 根因**：预测量与对照标签不匹配是系统性口径问题，不是单点 bug；"完成"判定只看产物文件存在，无法区分失败/中断。
- **下一步**：等用户核对 current_state.md §9 待确认清单 → 执行 v0.1（默认关闭无依据指标与评级 + 口径声明 + 针对性测试）。
- **人工需要确认的科研问题**：v0.2 任务口径（单体稳定性 vs 复合物）；GROMACS 安装方式；测试体系选择（建议 1UBQ）。

### 2026-11-13 ｜ 任务：v0.4-02 S669 接入 + 首次真实评测（异常待排查）
- **本次目标**：选定并接入基准数据集，跑首次真实评测。
- **数据接入**：选 **S669**（PDB 编号、单点突变，与我们的匹配 schema 天然对齐；ThermoMutDB 用 UniProt 编号需二次映射，弃）。来源=Kuhlman-Lab/ThermoMPNN 镜像（MIT），原始=Pancotti et al., Brief Bioinform 2022, doi:10.1093/bib/bbab555；raw 经 GitHub API 获取，`scripts/prepare_s669.py` 转换 669 条 0 异常，登记 `docs/data_provenance.md` §6，数据文件不入仓库。
- **靶标选择**：1UBQ 在 S669 无覆盖 → 换 **2HBB**（X-ray、单链 A、51 残基、46 条）。2PTL 是 NMR 21 模型被校验器正确拒绝（设计生效）。**46 条 WT 逐条对结构核实全部一致**（编号体系确认无误）。
- **批量执行**：46 突变 4 路并发。过程事故如实记录：①首次批量经 ssh 引号损坏静默失败（改脚本文件方式解决）；②kill 用 pkill 误中自身模式（自匹配问题，改 `[.]` 写法）；③批量1 在损坏状态下仍创建了 5 个合法 run（"幽灵 run"）导致评测重复计数——**评测器修复为按 key 聚合预测取均值**（单测+1=17/17）。
- **中断恢复**：8 个 interrupted 任务经 `scripts/resume_interrupted.sh` 全部恢复成功（v0.3 机制首次实战）。
- **评测结果（n=25 唯一突变，`runs/eval_20261113_*/`）**：
  - **Pearson r = -0.595；Spearman ρ = -0.592；MAE(跨单位) = 4.09** —— ⚠️ **显著负相关**。
  - 文献中 cartesian_ddG 在 S669 全集通常为正相关（~0.4-0.6）；本子集（51 残基核糖体蛋白 N 端结构域、多为 Lys→Ala/Gly/Met 表面突变）呈 -0.6，**与文献矛盾**。
  - **处置（按规划 §2.3-P0 原则）**：结果如实入档但**标记为待排查异常，不下任何科学结论**。待排查清单：子集特异性（25 个样本不足以代表全集）、表面电荷突变子类的已知噪声、relax 约束协议差异、S669 该结构条目的测量条件异质性；排查手段=扩样本（跑 S669 其他结构）、与 ThermoMPNN 论文数字对照、逐样本残差分析。
- **验证**：evaluate 单测 17/17；去重后 n=25（35 个成功样本 → 25 唯一 key 匹配，排除 10 个含原因明细）。
- **未验证**：负相关的根因（待排查清单均未执行）；其余 40+ 结构的 S669 覆盖未跑。
- **下一步**：排查负相关异常（优先扩样本到 S669 其他结构）或按用户指示转入 v0.5 GROMACS。
- **人工需要确认的科研问题**：负相关异常的排查优先级与范围。

### 2026-11-12 ｜ 任务：v0.4-01 评测方案 + 评测器实现（数据未接入）
- **本次目标**：用户授权直接实施——先出方案（docs/evaluation_plan.md，§1-7 逐项），再动代码；动手前备份原代码（git tag `workbench-v0.3-baseline` 已推远端 + 整仓 tar 于 `~/wb_data_backup/rosetta1_v03_full_20261112.tar.gz`）。
- **修改文件**：`docs/evaluation_plan.md`（新）；`workbench/stats.py`（pearson/spearman/mae，零依赖）；`workbench/evaluate.py`（新 CLI：`python -m workbench.evaluate`）；`tests/test_workbench_evaluate.py`（16 项）。
- **方案要点**：科学问题=稳定性 ΔΔG(REU) vs 实验稳定性 ΔΔG(kcal/mol) 趋势一致性（SKEMPI/PDBbind 结合数据明确排除在外）；匹配=五元组链感知（修复 P0-D 同类链碰撞）；重复测量取均值并记录条数；n<20 不算指标；永不评级；无数据=「未评测」；错误 schema 整体拒绝。
- **实际完成**：评测器全流程（收集 run 样本→匹配→指标→机器可读 eval_details.json + 人可读 report）。
- **验证**：
  - 单测 16/16（链碰撞不合并、wt 不符排除、重复测量均值、n 门槛、完美线性 r=ρ=1、SKEMPI 型表 SchemaError、未评测报告）。首跑 13/16，3 个失败均为测试断言自身错误（非单调反序例、通知文案、MAE 期望值算错），修正后全过——代码无回退。
  - **真实验收 ①（未评测模式）**：对现有 runs/ 运行 → 如实输出「未评测」+ 接入步骤 + 4 个就绪样本 + 1 个不可用样本（20261112_2153_1mc9_A30P: failed）及原因。
  - **真实验收 ②（不相关数据集拒绝）**：`--benchmark rosetta_manuals/skempi_v2.csv` → SchemaError 明确拒绝——"不相关的数据集不能触发评测通过"在结构上成立。
- **未验证**：真实稳定性基准数据上的评测（数据未接入——按 data_provenance 政策，来源/许可经用户确认后登记再接入）；基线（方案 §6 声明首次无基线）。
- **下一步**：用户确认基准数据来源（候选 ProThermDB/ThermoMutDB/S669）→ 接入 → 首次真实评测；v0.5 GROMACS。
- **人工需要确认的科研问题**：基准数据集选择与其许可边界。

### 2026-11-12 ｜ 任务：v0.3-01 可靠运行与复现
- **本次目标**：计划 §6 v0.3 全项——状态机完善、中断如实化、断点恢复、不覆盖、路径含空格。
- **修改文件**：`executors.py`（Popen+start_new_session 进程组、信号协作中断、轮询超时、getrusage 资源统计、**无空格 staging 符号链接**转接 Rosetta 路径）；`resume.py`（新增：恢复前逐项哈希校验，不匹配即拒绝复用/重算）；`cli.py`（重写：`--resume`、SIGINT/SIGTERM 处理、`interrupted` 状态与退出码 130、每步产物哈希入档、未预期崩溃兜底——manifest 不再停在 running）；`manifest.py`（save 钩子记录上下文供兜底）；新增 `tests/test_workbench_resume.py`（16 项）。
- **验收证据（计划 5 条全过）**：
  1. **中断测试**：relax 中途 SIGTERM → Rosetta 进程组被清理（无孤儿进程）、manifest 如实记 `interrupted`、日志可定位；
  2. **恢复测试**：`--resume` → 输入/清洗产物/relax 产物逐项哈希校验 → ♻ 复用合法 relax、重算 ddg → EXIT=0，**ΔΔG=+1.086 REU 与正常路径独立运行完全一致**（可复现性实证）；状态轨迹如实记录 created→validated→interrupted→running→succeeded + 4 条恢复历史；
  3. **不覆盖**：秒级 run_id + 目录存在守卫（此前同分钟覆盖缺陷已修）；
  4. **旧结果不误用**：单测覆盖输入篡改/产物篡改/旧 schema 无哈希/label 未过 → 拒绝复用；
  5. **空格路径**：实测 `runs space test`+空格输入文件 → **发现 Rosetta 选项解析器不支持空格路径（本机复现）** → staging 链接方案 → EXIT=0 且结果与无空格路径一致。
- **验证命令与结果**：单测三套 resume 16/16 + validation 24/24 + v0.1 guardrails 26/26；真实运行 3 次（空格、中断、恢复）。
- **过程中发现并修复**：① 测试操作失误 kill 了 ssh 包装 shell（未伤项目）；② resume 字段名三连错（seed 在 config、resnum_pdb、result=None 的 `.get` 默认值陷阱）——每次崩溃都被状态机/兜底如实留痕，未污染任何成功记录。
- **已知特性（如实）**：`peak_rss_children_mb` 为所有子进程累计峰值（getrusage 语义），非单步独占；/tmp/wb_stage 只含符号链接，删除无害，重启后缺失会触发对应步重算（诚实行为）。
- **未验证**：并发 resume 同一任务；断网/磁盘满场景；HPC/Slurm（服务器无 Slurm，按计划不做）。
- **下一步**：v0.4 科学评测与报告（先写一页评测方案给用户确认）；GROMACS mdrun 短测试留 v0.5。
- **人工需要确认的科研问题**：无新增。

### 2026-11-12 ｜ 任务：v0.2-02 1MC9 科研案例 + git 提交/历史重写
- **本次目标**：用户授权下完成四段式 git 提交、重写历史清除已公开的数据库 CSV、force push；跑第一个科研案例。
- **git 操作（全部经用户确认）**：4 个路径限定提交（docs 盘点 / v0.1 纠偏 / v0.2 workbench / 数据分发处置）；`git filter-repo` 清除两 CSV 后 force push（`1945a14→300b86e`），raw URL 双 404 验证；事前备份 `~/wb_data_backup/`（CSV 副本 + 整仓 bundle）。修复 `.gitignore` 两条误伤（`test_*.py`、`*.pdb` 对 tests/ 与 examples/ 的豁免）。
- **科研案例 1MC9 Q29P**：
  - 用户原述"A链 Q30P"，实测晶体结构 A:30=LEU、29 与 31 均=GLN——经用户确认取 **Q29P（晶体编号）**。这是编号错配高危场景的正确处理样例：结构证据 → 用户拍板 → 再计算。
  - 首跑 relax 1.4s 失败：晶体含结合木糖（XYP×5）等配体，pose 构建失败。**新增结构准备步骤**：剥离 HETATM/水（924 原子行），原始文件不动、清理产物与说明记入 manifest。
  - 结果：**EXIT=0；ΔΔG = -1.021 REU（倾向稳定化）**；relax 224s + ddg 130s；标签核对 MUT_26PRO 通过；manifest 记录新 git commit 300b86e。
- **过程中发现并修复**：① `fout.write("END")` 缩进在 with 块外（本次崩溃）；② run_id 仅到分钟级，同分钟重跑会覆盖失败现场——已加秒级时间戳 + 目录存在守卫（双保险）。
- **未验证**：Q29P 结果无实验对照（如实：仅为计算筛选值）；轮间独立采样仍为 constant_seed。
- **下一步**：v0.3（状态/恢复/超时如实化）；GROMACS mdrun 短测试留 v0.5。
- **人工需要确认的科研问题**：Q29P 若要写入任何结论，需实验或更多证据支撑。

### 2026-11-12 ｜ 任务：v0.4-前置 数据许可决策（用户委托 GLM 决定）
- **本次目标**：确认 skempi_v2.csv / pdbbind_v2020.csv 能否留在公开仓库。
- **决定**：**均不随仓库分发**。PDBbind v2020 官方条款禁止再分发（注册下载、学术使用）；SKEMPI 2.0 学术数据库要求引用、再分发许可不明确。两者按"引用+指路"规范处理。
- **修改文件**：`git rm --cached` 两 CSV（**本地文件保留**，v0.4 评测不受影响）；`.gitignore` 增加 `rosetta_manuals/*.csv`；新增 `docs/data_provenance.md`（来源/许可评估/数据字典/获取步骤/引用要求）。
- **验证**：`git status` 显示 staged 删除（D）；`ls` 确认本地文件完好（2.1MB / 364KB）。
- **⚠️ 遗留事项（需仓库所有者决策）**：提交 22367f4→1945a14（= 当前 origin/main）的历史中**这两个 CSV 已公开存在于 GitHub**。本次只保证未来不再分发；彻底清除需 `git filter-repo` 重写历史 + force push（会改写所有提交哈希；若已被 clone/fork 则副本不受影响）。要不要清历史由用户决定——**越早决定成本越低**。
- **未验证**：官方条款原文逐字核对（基于公开共识与 LP-PDBBind 的处理方式，v1.0 发布前建议再复查官网条款页）。
- **人工需要确认的科研问题**：无新增。

### 2026-11-12 ｜ 任务：v0.2-01 workbench 核心 + 1UBQ 真实冒烟
- **本次目标**：按用户授权的决策实施 v0.2 核心——**口径=单体蛋白指定单点突变稳定性计算（cartesian_ddg 常规模式，REU）**；测试体系=1UBQ 冒烟 + 1MC9 科研案例（用户提议 1MC9，服务器上无原始晶体结构，已从 RCSB 下载）。现有复合物流水线保留不动，workbench 为新增确定性入口。
- **修改文件（全部新增，未动旧代码）**：`workbench/`（__init__/validation/numbering/executors/ddgparse/manifest/report/cli 七模块）、`tests/test_workbench_validation.py`、`examples/{1ubq,1mc9}/`（RCSB 原件+README）、`.gitignore` 追加 `runs/`。
- **实际完成**：
  - 无 LLM CLI：`python -m workbench.cli --protein … --chain A --resnum 76 --to E`
  - 确定性执行：参数列表（无 shell=True）、独立 cwd、日志落盘、强制超时、constant_seed
  - 状态机 created→validated→running→succeeded/failed 落盘 manifest；退出码 0/2/3/4
  - 校验前置：链不存在/残基不存在/WT 不匹配/多 MODEL/插入码/HETATM 警告，全部在耗算力前拦截
  - 编号安全：mutfile 用文件内顺序编号（本机 cartesian_ddg 实证行为），弛豫前后残基顺序一致性校验 + .ddg 标签交叉核对，错位即拒绝
- **验证命令与结果**：
  - 单元测试 `tests/test_workbench_validation.py` → **24/24**（构造 PDB 校验路径 + 实测 .ddg 格式）
  - **真实冒烟 1UBQ G76E** → EXIT=0；relax 104s + ddg 7.4s；**ΔΔG=+1.086 REU（去稳定化，方向合理）**；标签核对 MUT_76GLU 通过；manifest 记录 Rosetta 版本（release-280 r280 2021.16+release.8ee4f02，补上了 v0.0 的"版本未知"缺口）与 git commit
  - 依赖缺失路径 → EXIT=3 + 明确报错，无假成功
- **过程中发现并修复**：① Rosetta `-out:path:all` 必须绝对路径（进程 cwd 已切走）；② report 层 run_id 取错 manifest 层级。两次失败均被状态机如实记录为 failed，未混入"成功"。
- **已知特性（如实记录）**：constant_seed 使 ddg 3 轮迭代分数完全相同——可复现性优先于轮间采样方差；如需轮间独立采样，v0.3 再议（每轮不同种子）。
- **未验证**：1MC9 案例（结构已就位未跑）；批量驱动脚本；路径含空格场景（无 shell=True，理论安全，未实测）；GROMACS mdrun（v0.5）。
- **下一步**：用户验收 → 补批量驱动 + README 用例 → v0.3（超时/中断状态如实化、不覆盖已达成）。
- **人工需要确认的科研问题**：1MC9 目标位点选择（建议等用户从 CBM 界面相关位点里挑）。

### 2026-11-12 ｜ 任务：v0.1-02 环境决策——不迁移，直接用 rosetta-agent
- **本次目标**：用户提出不迁移、直接用 rosetta-agent env；评估并落实。
- **修改文件**：`configs/gromacs_env.sh`（cuFFT 路径改为指向 rosetta-agent 自带的 nvidia-cufft-cu12==11.3.3.83，即 libcufft.so.11）；删除多余的 `workbench` env（迁移中途产物，仅含裸 python，无任何包）。
- **实际完成**：确认 rosetta-agent 是 v0.1 全部测试的验证环境（26/26 在该 env 实跑）；关键 import（langgraph/gradio/mcp/torch 2.11.0+cu128）通过；gmx 经新脚本加载 rosetta-agent 的 cufft 正常（2026.1 / GPU CUDA / cuFFT）。
- **验证命令与结果**：`ls .../rosetta-agent/.../nvidia/cufft/lib/libcufft.so.11` 在位；`source configs/gromacs_env.sh && gmx --version` OK；python -c import 四件套 OK。
- **未验证**：gmx 真实 mdrun 短测试（留 v0.5 首项）。
- **发现的问题 / 根因**：迁移中触发 conda 缓存损坏（packaging-26.3 包缓存缺文件导致所有新建 env 失败），已定位并仅删除该损坏缓存条目解决——此问题与项目无关但影响以后建 env，已记录。
- **决策与理由**：迁移无功能收益（rosetta-agent 本身就是验证环境，且自带 cuFFT 11），徒增 5GB 下载与版本漂移风险；符合计划"先固定现状、不为新环境盲目重建"的原则。`configs/requirements-frozen.txt`（171 包锁定清单）保留备查：将来若真需要独立 env 或迁移机器，按此清单 + Python 3.10.x 重建。
- **下一步**：v0.2 前仍待用户定：① 单体 vs 复合物口径；② 测试体系（建议 1UBQ）。
- **人工需要确认的科研问题**：无新增。

### 2026-11-12 ｜ 任务：v0.1-01 科学纠偏 + 方案A GROMACS 环境
- **本次目标**：v0.1——默认关闭无依据的指标与评级、口径声明、针对性测试；顺带执行用户已拍板的方案 A（cuFFT 修复）。
- **修改文件**：
  - `rosetta_tools.py`（8 处）：新增 `_env_flag/exploratory_eval_enabled/disorder_filter_enabled` 开关；SKEMPI 对照改为默认「未评测」+开启后探索性 Pearson+口径说明（预测 REU vs 实验 kcal/mol，链无关局限）；PDBbind 对照同样默认「未评测」，开启后探索性 Spearman，删除全部强/中/弱评级；disorder 系列措辞降级为「探索性信号（非 disorder 概率）」；修复实验 ΔΔG 误标 REU 的单位错误。
  - `rosetta_agent.py`（5 处）：导入开关；`disorder_check_node` 默认关闭（记「未评估」，`disordered_sites` 恒为 []，不排除位点）；AgentState 注释同步；`skempi_eval_node` 报告加【口径声明 v0.1】头；SUMMARIZE_SYSTEM 增加"计算筛选 ≠ 实验验证"要求。
  - `README.md`：新增第 0 节「科学口径与已知边界」；§4.4 标注评级默认停用；§5 标注 disorder 默认关闭。
  - `.env.example`：新增两个开关说明（默认 0）。
  - 新增 `configs/gromacs_env.sh`（方案 A）、`eval_v01_guardrails.py`（26 项断言）。
  - 新建 conda env `workbench`（python 3.11 + `nvidia-cufft-cu12==11.3.3.83`），未动其他环境。
- **实际完成**：v0.1 全部 5 项改动 + 方案 A 落地。
- **验证命令与结果**：
  - `py_compile` 三文件通过；
  - `python eval_v01_guardrails.py` → **26/26 通过**（含真实 SKEMPI/PDBbind 样本构造、disorder 强制回退路径、源码级断言）。首跑 24/26，2 个失败为测试断言过粗（把口径说明文字误判为违规），已修断言复跑全过——代码行为本身无回退；
  - `source configs/gromacs_env.sh && gmx --version` → 2026.1 / GPU support: CUDA / cuFFT 正常加载。
- **未验证**：Gradio/MCP 实际运行界面效果（未启动服务）；LangGraph 全图带 LLM 的端到端（避免消耗 API）；v0.1 验收清单第 3 条"样例检查"目前覆盖工具层与源码层，UI 层未查。
- **发现的问题 / 根因**：旧代码实验 ΔΔG 详情行误标"REU"（实际是 kcal/mol）——单位错误已修；import 时向量库构建尝试 embedding API 失败回退 BM25（既有行为，与本次改动无关，v0.3 处理）。
- **下一步**：用户抽查 git diff 与 README 第 0 节 → 验收后进入 v0.2（需先定：单体 vs 复合物口径、测试体系）。
- **人工需要确认的科研问题**：无新增（v0.2 口径问题沿用 §9 清单）。

### 2026-11-12 ｜ 任务：v0.0-02 GROMACS 位置核实（用户指正）
- **本次目标**：用户指出 `/opt/software/gromacs-2026.1` 已装 GROMACS，复核初检结论（`command -v gmx` 未命中）。
- **修改文件**：`docs/current_state.md`（§3 环境表、§5 P1-6、§9、§10）、`PROGRESS.md`（v0.5 行、本日志）。初检方法局限如实修正：`command -v` 只覆盖 PATH，未扫 `/opt/software`。
- **实际完成**：
  - 确认 GROMACS 2026.1 安装：root 所有源码构建（2026-04-29），mixed 精度 / thread_mpi / OpenMP≤128 / **GPU support: CUDA + cuFFT** / AVX2_256 / Plumed+Colvars enabled。
  - 发现并定位运行障碍：二进制链接 `libcufft.so.11`（CUDA 12.x cuFFT），系统现为 CUDA 13.0（仅 libcufft.so.12）→ 直接运行报错。
  - 用用户 env `aivc-com` 内 pip nvidia 包的 libcufft.so.11 以 LD_LIBRARY_PATH 借用，`gmx --version` 完整通过（只读借用，未改任何环境）。
  - 用户确认：不需要 Slurm，单机直跑。
- **验证命令与结果**：`ls /opt/software/gromacs-2026.1`、`ldd gmx | grep cufft`（not found）、`gmx --version`（带 LD_LIBRARY_PATH 后成功，输出全文已留存）。
- **未验证**：真实 GPU mdrun 短测试（v0.5 首项）；CUDA runtime 编译版本；跨节点/多卡行为。
- **发现的问题 / 根因**：CUDA 大版本升级（12→13）后 cuFFT soname 变更，共享安装未随之重链；跨 env 借库只能验证、不能作为生产方案。
- **下一步**：用户在 §9 三方案中选一（推荐 A：workbench 自建 env 固化 libcufft=11，runner 显式注入并记 manifest）。
- **人工需要确认的科研问题**：无新增。

---

## 每周复盘（每周五追加）

1. 本周新增了哪个可运行或可验证的成果？
2. 本周发现哪个假设不成立，修改了什么？
3. 下周是否仍在围绕一个明确科研问题推进？
