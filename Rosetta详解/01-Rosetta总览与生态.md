# 01 · Rosetta 总览与发展生态

## 1. 什么是 Rosetta

**Rosetta** 是由华盛顿大学 **David Baker 实验室**（现扩展为 Rosetta Commons 联盟）开发的开源分子建模软件套件。它用 **C++** 编写，提供从原子级能量评估到蛋白设计、结构预测、分子对接的完整工具链。

### 1.1 一句话定义

> 用**蒙特卡洛 + 能量函数**驱动的蛋白设计与结构预测软件。

### 1.2 核心思想

Rosetta 把所有分子建模问题统一为**优化问题**：

```
给定：起始构象 + 任务
找到：能量最低（最稳定）的构象
方法：蒙特卡洛 / 模拟退火 / 遗传算法 + 能量函数评估
```

能量函数（force field）来自物理化学 + 统计势能，是 Rosetta 的灵魂。

### 1.3 与其他分子模拟软件的区别

| 软件 | 核心方法 | 优势 | 劣势 |
|---|---|---|---|
| **Rosetta** | 蒙特卡洛 + 启发式 | 蛋白设计/重设计极强 | 分子动力学不如 GROMACS |
| **GROMACS** | 分子动力学 (MD) | 物理真实、连续轨迹 | 采样空间大，难收敛 |
| **AMBER** | 分子动力学 + 量子力学 | 力场精准 | 计算昂贵 |
| **CHARMM** | 分子动力学 | 生物体系广泛 | 学习曲线陡 |
| **NAMD** | MD + GPU 加速 | 超大规模 | 主要做 MD |
| **AlphaFold** | 深度学习 | 速度快精度高 | 黑盒、不能设计 |
| **Chai-1 / Boltz-1** | 深度学习 | 多模态 | 较新未经大量验证 |

**Rosetta 的独特定位**：**蛋白设计（sequence design）领域**绝对王者，其他领域也都有覆盖但非最强。

## 2. 历史发展

### 2.1 关键时间线

| 年份 | 事件 |
|---|---|
| 1997 | Rosetta 由 Baker 实验室首次发布，用于**蛋白折叠**（从序列预测三维结构） |
| 2000 | Rosetta 在 CASP3 结构预测比赛崭露头角 |
| 2004 | RosettaDesign 发布，开启**蛋白设计**时代 |
| 2008 | RosettaDock 成为蛋白-蛋白对接标准工具 |
| 2010 | RosettaCommons 联盟成立（多机构协作） |
| 2011 | **PyRosetta** Python 绑定发布，大幅降低使用门槛 |
| 2012 | RosettaScripts XML 脚本系统发布 |
| 2014 | **Cartesian space** 弛豫协议发布 |
| 2017 | **ref2015** 能量函数成为标准 |
| 2018 | Rosetta 累计论文超 10000 篇引用 |
| 2020 | RFdiffusion 等 ML 方法兴起，Rosetta 与 ML 融合 |
| 2023 | Baker 团队用 ESM-2 + RFdiffusion 设计全新蛋白进入 Nature |
| 2024 | ProteinMPNN 等 ML 序列设计器成熟 |
| 2025 | Rosetta 与 Boltz-1 / AlphaFold3 集成 |

### 2.2 关键人物

- **David Baker**：华盛顿大学，Baker 实验室创始人，Rosetta 之父，HHMI 研究员
- **Richard Bonneau**：早期 Rosetta 核心开发者（已转去 New York University）
- **Brian Kuhlman**：RosettaDesign 主要开发者
- **Jeffrey Gray**：RosettaDock 主要开发者（Johns Hopkins）
- **Tanja Kortemme**（UCSF）：设计协议开发
- **Sarel Fleishman**（Weizmann）：抗体设计
- **Po-Ssu Huang**：酶设计

## 3. Rosetta 组件架构全景

Rosetta 不是单一软件，而是**多模块、多入口**的复杂套件：

```
Rosetta 套件
├── 核心库（lib + headers）
│   ├── core/         # Pose / ScoreFunction / Mover
│   ├── protocols/    # 各类协议实现
│   ├── basic/        # 基础工具
│   └── numeric/      # 数值计算
├── 应用程序（binaries）
│   ├── relax.linuxgccrelease
│   ├── cartesian_ddg.linuxgccrelease
│   ├── docking_protocol.linuxgccrelease
│   └── ...（数百个二进制）
├── Python 绑定
│   └── PyRosetta（独立发行版）
├── 脚本工具（source/scripts/python/public/）
│   ├── molfile_to_params.py
│   ├── clean_pdb.py
│   └── ...
├── 力场文件（database/scoring/weights/）
│   ├── ref2015.wts
│   ├── ref2015_cart.wts
│   └── ...
├── 残基类型（database/chemical/）
│   └── 20 种标准氨基酸 + 配体 + 修饰
└── 衍生产品
    ├── Robetta（Web 服务器）
    ├── Foldit（游戏化）
    ├── ROSIE（在线服务）
    └── SnugDock / FlexPepDock 等专项协议
```

## 4. Rosetta 衍生产品全景

这是本章最关键的部分——Rosetta 不是一个软件，而是一个**生态系统**。

### 4.1 学术协议与许可

⚠️ **重要**：Rosetta 是**学术免费 + 商业付费**的混合模式：

- **学术用途**：免费，需签学术协议（在线申请）
- **商业用途**：需购买商业许可（[通过 PyRosetta 商业版](https://www.pyrosetta.org/commercial)）

---

### 4.2 核心衍生产品分类

#### A. 命令行工具集（核心二进制）

所有这些都在 `source/bin/` 下，按功能分：

**蛋白结构预测**：

| 工具 | 用途 |
|---|---|
| `AbinitioRelax` | 从序列 ab initio 折叠预测三维结构 |
| `Abinitio` | 纯 ab initio 折叠 |
| `hybridize` | 同源建模 + ab initio 杂交 |
| `loopmodel` | Loop 区域建模 |
| `cm/alipt_cms` | RosettaCM 同源建模 |
| `partial_thread` | 部分序列 thread |

**蛋白设计**：

| 工具 | 用途 |
|---|---|
| `fixbb` | 固定 backbone 设计序列 |
| `relax` | 结构弛豫优化 |
| `fastrelax` | 快速弛豫 |
| `enzdes` | 酶活性位点设计 |
| `ligand_dock_script` | 配体对接 |
| `antibody` | 抗体设计 |
| `floppytail` | 柔性 N/C 端处理 |

**对接**：

| 工具 | 用途 |
|---|---|
| `docking_protocol` | 蛋白-蛋白对接 |
| `docking_prepack_protocol` | 预处理版 |
| `FlexPepDock` | 柔性肽对接 |
| `flexpepdock_abinitio` | 从头柔性肽对接 |
| `anchordock` | 锚定对接 |
| `recces` | 隐性溶剂对接 |
| `concatpdb` | PDB 拼接 |

**ΔΔG / 突变扫描**：

| 工具 | 用途 |
|---|---|
| `cartesian_ddg` | Cartesian 空间 ΔΔG（本项目用） |
| `ddg_monomer` | 单体 ΔΔG |
| `interface_ddg` | 界面 ΔΔG |
| `per_residue_energies` | 逐残基能量分解 |
| `alanine_scan` | Ala 扫描 |

**核酸**：

| 工具 | 用途 |
|---|---|
| `rna_denovo` | RNA 结构从头预测 |
| `rna_design` | RNA 序列设计 |
| `erraser` | RNA 结构修正 |
| `stepwise` | RNA 组装 |
| `dna_denovo` | DNA 设计 |

**糖类 / 膜 / 其他**：

| 工具 | 用途 |
|---|---|
| `glycan_relax` | 糖链弛豫 |
| `glycan_tree_relax` | 糖树弛豫 |
| `mpframework` | 膜蛋白框架 |
| `mp_dock` | 膜蛋白对接 |

#### B. Python 绑定

| 产品 | 用途 | 适用人群 |
|---|---|---|
| **PyRosetta** | Rosetta C++ 库的 Python 绑定 | 算法开发者、科研人员 |
| PyRosetta Jupyter | 浏览器内 Jupyter Notebook + PyRosetta | 教学、演示 |
| **PyRosetta.notebooks** | 教学 Notebook 集合 | 学生 |

**PyRosetta 的优势**：
- Python 易写，比 C++ 友好 10 倍
- 可与其他科学计算栈（numpy、scipy、pandas）无缝结合
- 支持交互式探索

**PyRosetta 的劣势**：
- 商业用途需付费
- 比 C++ 慢（特别是循环）
- API 不稳定（Rosetta 版本升级时易 break）

#### C. RosettaScripts（XML 协议编排）

**RosettaScripts** 是 Rosetta 的"剧本系统"——用 XML 文件描述协议流程：

```xml
<ROSETTASCRIPTS>
  <SCOREFXNS>
    <ScoreFunction name="ref15" weights="ref2015.wts"/>
  </SCOREFXNS>
  <MOVERS>
    <FastRelax name="relax" scorefxn="ref15"/>
    <MutateResidue name="mut1" target="71" new_res="ALA"/>
    <PackRotamers name="design" scorefxn="ref15"/>
  </MOVERS>
  <PROTOCOLS>
    <Add mover="relax"/>
    <Add mover="mut1"/>
    <Add mover="design"/>
  </PROTOCOLS>
</ROSETTASCRIPTS>
```

```bash
rosetta_scripts.linuxgccrelease -parser:protocol my_protocol.xml -s input.pdb
```

**优势**：
- 无需编译 C++
- 协议可读、可复现、可分享
- 学术论文附 XML 协议是惯例

**劣势**：
- 性能不如 C++ 内联
- 复杂协议 XML 维护困难

#### D. 在线服务 / Web 服务器

| 产品 | 网址 | 用途 |
|---|---|---|
| **Robetta** | https://robetta.bakerlab.org/ | 结构预测、design 在线服务 |
| **ROSIE** | https://rosie.rosettacommons.org/ | 各种 Rosetta 协议的 Web 界面 |
| **RosettaServer** | - | 集群版批量运行 |
| **FOLD.it** | https://fold.it/ | **游戏化**的蛋白折叠（公民科学） |

**Robetta** 提供：
- 结构预测（ab initio + 同源建模）
- 蛋白-蛋白对接
- 酶设计
- 抗体人源化

**FOLD.it** 是 Rosetta 团队 2008 年发布的**益智游戏**——让普通人玩游戏贡献蛋白设计。**首款由游戏玩家设计的酶**（Diels-Alder 合成酶）就是用 Foldit + Rosetta 设计的（2012 年）。

#### E. 专项协议工具集

围绕核心二进制，有大量专项工具：

| 工具 | 用途 | 论文 |
|---|---|---|
| **RosettaAntibody** | 抗体（Fv、scFv）建模 | Sircar et al. |
| **RosettaDesign** | 序列设计（多目标） | Kuhlman & Baker 2000 |
| **RosettaDock** | 蛋白-蛋白对接 | Gray et al. |
| **RosettaLigand** | 蛋白-配体对接 | Meiler & Baker |
| **FlexPepDock** | 柔性肽对接 | Raveh et al. |
| **AnchorGrow** | 锚定 + 逐步生长肽对接 | Rosenzweig et al. |
| **SnugDock** | 抗体-抗原对接 | Sircar et al. 2010 |
| **RosettaEnzymes** | 酶活性位点设计 | Rothlisberger et al. 2008 |
| **OZ** (Ozyme) | 酶优化框架 | Fazelinia et al. |
| **DHR** (De novo Heme Recognition) | 血红素结合设计 | Koder et al. |
| **LayerDesign** | 按层级设计（核心/表面/边界） | Fleishman et al. |
| **CoupledMoves** | backbone + 序列协同设计 | Ollikainen et al. |
| **FloppyTail** | N/C 端柔性处理 | Kleiger et al. |
| **MPer** | 膜蛋白能量函数 | Yarov-Yarovoy et al. |
| **RosettaMembrane** | 膜蛋白从头设计 | Lu et al. |
| **RosettaNMR** | NMR 数据整合 | Lange et al. |
| **RosettaCM** | 比较建模（同源建模） | Song et al. |
| **Partial_THREAD** | 部分序列 thread | - |
| **CCD** (Cyclic Coordinate Descent) | Loop 闭合 | Canutescu & Dunbrack |
| **KIC** (Kinematic Closure) | Loop 闭合 | Mandell et al. |
| **enzymatic_design** | 酶从头设计 | Siegel et al. |
| **fold_from_loops** | 从 Loop 折叠 | - |
| **legacy_sewing** | 结构片段拼接 | - |
| **SEWING** | 结构片段设计 | Jacobs et al. |
| **GENIP** | 蛋白 interface 设计 | Fleishman et al. |
| **Match** | 蛋白-小分子匹配设计 | - |

#### F. 力场 / 权重文件

能量函数权重文件在 `database/scoring/weights/` 下，常用：

| 权重组 | 用途 |
|---|---|
| `ref2015.wts` | **当前标准**，通用 |
| `ref2015_cart.wts` | Cartesian 空间（**本项目用**） |
| `talaris2013.wts` | 早期主流 |
| `talaris2014.wts` | 早期修订 |
| `score12.wts` | 经典（很旧） |
| `gen_potential.wts` | ML 增强 |
| `beta_nov16.wts` | β-sheet 设计专用 |
| `soft_rep_design.wts` | 设计用软斥力 |
| `ref2015_soft.wts` | 设计用软化版 |
| `hbnet.wts` | 氢键网络分析 |

#### G. 化学 / 残基类型库

`database/chemical/` 包含：

- 20 种标准氨基酸残基类型
- 各种修饰氨基酸（MSE、SEP、TPO 等）
- 配体模板（通过 molfile_to_params.py 添加）
- 核苷酸（A、C、G、U、T）
- 糖类残基
- 共价修饰

#### H. 辅助工具脚本

`source/scripts/python/public/` 下的关键脚本：

| 脚本 | 用途 |
|---|---|
| **molfile_to_params.py** | 配体 PDB/mol2 → Rosetta .params（**本项目用**） |
| `clean_pdb.py` | PDB 清洗（去水、标准化） |
| `prepack.py` | 预处理侧链 |
| `print_pdb_secondary_structure.py` | 提取二级结构 |
| `pdb2fasta.py` | PDB → FASTA |
| `ligand_dock_setup.py` | 配体对接前置 |
| `res_lig_interaction_stats.py` | 残基-配体接触统计 |

#### I. 测试集与基准

- **Rosetta test set**：每日回归测试
- **CASP**：结构预测基准比赛
- **CAPRI**：蛋白对接基准比赛
- **Continuous Fluctuation Model (CFM) benchmark**

## 5. Rosetta 学术影响力

### 5.1 论文统计

- Rosetta 相关论文：**> 10,000 篇**（2024）
- Nature/Science/Cell 正刊：**> 50 篇**
- 引用的 Rosetta 工具：design、docking、loop modeling、ab initio 是最常被引

### 5.2 里程碑成果

| 年份 | 成果 | 论文 |
|---|---|---|
| 2003 | 首次设计全新蛋白 Top7 | Kuhlman et al. Science |
| 2010 | 设计 Diels-Alder 合成酶 | Siegel et al. Science |
| 2012 | Foldit 玩家设计首个酶 | Khatib et al. |
| 2014 | 设计自组装纳米颗粒 | King et al. Science |
| 2016 | 设计微型蛋白 cage | Hsia et al. Nature |
| 2019 | 设计蛋白开关 | Langan et al. |
| 2023 | ESM-2 + RFdiffusion 设计全新蛋白 | Watson et al. Nature |
| 2024 | RFdiffusion 全 de novo 蛋白设计 | - |

### 5.3 产业化

- **Cyrus Biotechnology**（2024 被 Sleeper 收购）：Rosetta 商业版
- **Monod Bio**（2021）：蛋白设计公司
- **Neoleukin Therapeutics**：蛋白药物设计
- **Generate:Biomedicines**：ML + Rosetta 设计

## 6. Rosetta 当前定位（2024-2026）

### 6.1 与 ML 工具的融合

近 5 年 ML 工具（AlphaFold、ESM、RFdiffusion、ProteinMPNN）爆发，Rosetta **不是被取代，而是与 ML 互补**：

| 任务 | ML 工具 | Rosetta 优势 |
|---|---|---|
| 结构预测 | AlphaFold2/3, ESMFold | - |
| 单点设计 | ProteinMPNN, ESM-2 | Rosetta 物理约束更准确 |
| De novo 设计 | RFdiffusion | Rosetta 能量评估更精确 |
| 亲和力优化 | - | Rosetta 是金标准 |
| 配体结合 | DiffDock | Rosetta 物理采样更准 |
| 酶活性 | - | Rosetta 专用 |

**典型工作流**：AlphaFold 预测 → RFdiffusion 生成骨架 → ProteinMPNN 设计序列 → Rosetta 评估 ΔΔG → 实验验证

### 6.2 Rosetta 3.x 与未来

- **Rosetta 3.x**：当前主线版本
- 持续更新：每月小版本，每年大版本
- 添加 ML 力场（与深度学习融合）
- GPU 加速部分协议（ref2015_cart 已支持）
- 与 PyTorch 集成（ref2015_cart 已在 GPU 上跑）

### 6.3 学习资源

- 官方文档：https://www.rosettacommons.org/docs/latest/
- Rosetta 教程工作坊：每年夏季（华盛顿大学）https://www.rosettacommons.org/workshops
- 论文集合：https://www.rosettacommons.org/docs/latest/publications
- **PyRosetta 教学 Notebooks**：https://github.com/RosettaCommons/PyRosetta.notebooks
- 中文教程：Bilibili 搜索"Rosetta 教程" / CSDN 多篇
- 官方论坛：https://www.rosettacommons.org/forum

## 7. 一句话总结

**Rosetta 是一个 25+ 年积累的开源分子建模套件，由 Baker 实验室主导，从蛋白设计起家，逐步扩展到结构预测、对接、核酸、糖类、膜蛋白等领域，并衍生出 Robetta/Foldit/PyRosetta/RosettaScripts 等完整生态。它在蛋白设计与亲和力优化任务上仍是事实标准的科学计算工具。**