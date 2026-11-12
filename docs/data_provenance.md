# 实验数据库：来源、许可与使用决定（v0.4 前置决策，2026-11-12）

> 决定：**两个数据库 CSV 均不再随仓库分发**（已从 git 跟踪移除，本地文件保留原位）。
> 决定人：GLM（受用户委托）；依据 = 两个数据库的官方使用条款与学术数据库的保守惯例。
> 本文档替代"文件即数据"：任何使用者按 §4 自行获取，即可完整复现 v0.4 评测。

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
