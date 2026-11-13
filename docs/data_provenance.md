# 实验数据库：来源、许可与使用决定（v0.4 前置决策，2026-11-12）

> 决定：SKEMPI / PDBbind **不随仓库分发**（理由见 §1）；v0.4 起接入任务匹配的 S669 稳定性基准（§6）。
> 决定人：GLM（受用户委托）；依据 = 各数据库官方使用条款与学术数据库的保守惯例。
> 本文档替代"文件即数据"：任何使用者按获取步骤自行获取，即可完整复现评测。

## 6. `rosetta_manuals/stability_benchmark_s669.csv`（S669 稳定性基准，2026-11-12 接入）

| 项 | 内容 |
|---|---|
| 用途 | **v0.4 唯一任务匹配评测集**：单点突变稳定性 ΔΔG（kcal/mol，PDB 编号）——与 workbench 计算口径一致 |
| 原始数据集 | S669：Pancotti, Benevenuta, Birolo, Alberini, Repetto, Sanavia et al., *"Predicting protein stability changes upon single-point mutation: a thorough comparison of the available tools on a new dataset"*, **Briefings in Bioinformatics 2022**, doi:10.1093/bib/bbab555 |
| 获取镜像 | Kuhlman-Lab/ThermoMPNN 仓库 `data_all/testing/s669_clean_dir.csv`（镜像仓库 **MIT License**） |
| 下载日期 | 2026-11-12（经 GitHub API 原样获取） |
| 原始文件 | `rosetta_manuals/s669_raw_original.csv`（670 行：表头+669 条） |
| 转换 | `scripts/prepare_s669.py` → 669 条写入本文件，0 条异常；`PDB`列（结构ID+链）与 `MUT`列（wt+编号+mut）拆分为五元组 schema |
| 本地列 | pdb_id, chain, resnum_pdb, wt_aa1, mut_aa1, exp_ddg_kcal_mol, source, reference |
| 覆盖 | 669 条 / ~95 个结构；本项目选用 **2HBB**（X-ray、单链 A、51 残基、46 条，WT 已逐条对结构核实全部一致） |
| 分发决定 | **不随仓库分发**（与 §1 同政策；`.gitignore` 已覆盖） |
| 引用要求 | 使用任何基于本文件的评测结果，必须引用上述 Pancotti et al. 2022 与所用结构的原始文献 |

### 6.1 符号口径求证（2026-11-13，悬案闭环）

**结论：镜像 CSV 的 DDG 列原始约定为【负 = 去稳定化】；ThermoMPNN 官方在加载时对其取负（×−1），统一为【正 = 去稳定化】。本项目的 `SIGN_FLIP = -1` 与官方实现完全一致，予以保留。**

证据链（全部来自 Kuhlman-Lab/ThermoMPNN 仓库源码，2026-11-13 查证）：

1. `datasets.py` 三个数据源加载时符号处理**不一致**：MegaScale 取负（`ddG = -torch.tensor([float(row.ddG_ML)], ...)`）、FireProt 原样（`torch.tensor([row.ddG], ...)`）、**ddgBench 取负（`torch.tensor([row.DDG * -1.], dtype=torch.float32)`）**——作者清楚各原始文件约定不同，在加载层统一。
2. `analysis/thermompnn_benchmarking.py` 的 `main()` 显示 **S669（`s669_clean_dir.csv`）正是经 `ddgBenchDataset` 加载**（`"S669": ddgBenchDataset(..., csv_fname=.../s669_clean_dir.csv)`）——官方评测 S669 前对其 DDG 做了 ×−1。
3. 评测代码对实验值无其他符号操作（直接进 PearsonCorrCoef），ProteinMPNN baseline 以 `-logprob` 为 ddG 代理——方向自洽性要求统一约定为【正 = 去稳定化】（突变残基概率越高 → ddG 越负 → 越稳定）。
4. 佐证（v0.4-03 已记录）：镜像 CSV 原始读法下，疏水核心 I/L/V→A（−4.28/−3.21/−2.33）与螺旋 A→G（−0.3~−1.8）为热力学不可能值，反号后均为教科书值。

**对既有记录的修正**：v0.4-03 曾记载"ThermoMPNN 官方代码无符号翻转、报正 Pearson，与我们的翻转结论存在张力"——**该记载有误**：官方翻转位于 `datasets.py` 加载层而非基准 CSV 生成层；其论文的正 Pearson 本就是在翻转后约定下报告的，与我们 r=+0.34 无矛盾。悬案就此关闭。

**对 `SIGN_FLIP` 维护条款的更新**：原回滚条款"如来源方澄清约定，删除 SIGN_FLIP 即回滚"——来源方口径已查证澄清（本节），SIGN_FLIP 由"经验性修正"升级为"与官方实现核对一致"；仅当上游 `s669_clean_dir.csv` 数值约定变化（以本节证据链复查为准）才需重审。


## 1. 决定与理由

| 数据 | 决定 | 理由 |
|---|---|---|
| `skempi_v2.csv`（SKEMPI 2.0） | 不随仓库分发 | 学术数据库（BSC），使用要求引用，官方**未提供明确的开源再分发许可**；虽存在第三方镜像，但"他人再分发"不构成授权 |
| `pdbbind_v2020.csv`（PDBbind v2020） | 不随仓库分发 | PDBbind 官方条款要求**注册下载、学术使用、不得再分发**数据库内容；其重整理版 LP-PDBBind（本 CSV 的直接来源）也因此只提供参照文件，要求用户自行下载 PDBbind |

两条共同原则：① 科研数据库的默认姿势是"引用 + 指路"，不是"搬运"；② 规划 §2.3-P1-6 明确要求不得把未确认可公开的数据推到公开仓库。

## 2. 现状与历史说明（重要）

- 2026-11-12 起：两文件已 `git rm --cached`，新增 `.gitignore` 规则 `rosetta_manuals/*.csv`；**本地文件保留原位**，v0.4 评测照常使用，不受影响。
- ⚠️ **历史提交（22367f4 起至 1945a14）中这两个文件仍存在于 GitHub 仓库历史**。从当前提交起不再分发 ≠ 历史消失。彻底清除需要 `git filter-repo` 重写历史 + force push——是否执行由仓库所有者决定（影响：已 clone/fork 的副本不受重写影响）。

## 3. 数据字典（本地副本实测）

### 3.1 `rosetta_manuals/skempi_v2.csv`（2.1 MB，7,085 条数据行，分号分隔）

来源：[SKEMPI 2.0](https://life.bsc.es/pid/skempi2)（经 GitHub 镜像 youpze/ScML 获取，2026-09 前后）
引用：Jankauskaitė et al., *J Mol Biol* 2019（SKEMPI 2.0 论文）
关键列：

| 列 | 含义 | 本项目用法 |
|---|---|---|
| `#Pdb` | PDB ID（4 字符） | 与任务名匹配 |
| `Mutation(s)_cleaned` | 突变列表，如 `DA63B`（ partner=链B, WT=D, 63, MUT=A） | 解析出 (wt, pos, mut) |
| `Affinity_wt_parsed` / `Affinity_mut_parsed` | WT/MUT 亲和力（M） | 无现成 ddG 列时按 `RT·ln(Kd_mut/Kd_wt)` 换算 ΔΔG（kcal/mol） |
| `Temperature` | 测量温度（K） | 换算用 |
| `Reference` / `Method` | 文献与实验方法 | 溯源 |

注意：本副本**没有**现成 `ddG_exp_kcal_mol` 列；`rosetta_tools.py` 的 SKEMPI 对照按亲和力换算，且匹配 key 为链无关三元组（链歧义未消除，见 README 第 0 节）。

### 3.2 `rosetta_manuals/pdbbind_v2020.csv`（364 KB，19,443 条数据行，逗号分隔）

来源：[LP-PDBBind](https://github.com/THGLab/LP-PDBBind)（Leak-Proof PDBBind 重整理）→ 上游 [PDBbind v2020](http://www.pdbbind.org.cn/)（refined+general）
引用：PDBbind 官方引用 + LP-PDBBind 论文（*Nat Mach Intell* 2025）
列：`pdb_id, Kd (M), Ki (M), IC50 (M)`（工具取优先级 Kd > Ki > IC50）

⚠️ 评测适用性警告（v0.4 前必读）：把**复合物 total_score** 与 **Kd** 做跨体系相关，缺乏物理与统计依据，当前代码默认只展示数据、不做评级——该对照不得用于任何"可信度"结论。

## 4. 获取步骤（复现者操作）

1. SKEMPI 2.0：到 https://life.bsc.es/pid/skempi2 下载 → 导出为分号分隔 CSV（列名见 §3.1）→ 存为 `rosetta_manuals/skempi_v2.csv`
2. PDBbind v2020：到 http://www.pdbbind.org.cn/ 注册并下载 v2020 → 取每 PDB 的 -log(亲和力) 数据；或直接采用 LP-PDBBind 的参照表 → 导出为 §3.2 列结构的 CSV → 存为 `rosetta_manuals/pdbbind_v2020.csv`
3. 记录：下载日期、版本号、导出脚本；将"下载日期/版本"填入本文件 §5

## 5. 本地副本元数据

| 项 | skempi_v2.csv | pdbbind_v2020.csv |
|---|---|---|
| 本地大小 | 2.1 MB | 364 KB |
| 数据行数 | 7,085 | 19,443 |
| 首次入库提交 | 22367f4（2026-09） | 同左 |
| 移除分发 | 本文档对应提交 | 同左 |
| 官方下载日期 | **unknown**（待补） | **unknown**（待补） |
