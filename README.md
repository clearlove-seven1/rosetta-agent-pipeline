# Rosetta-Agent

**Rosetta-Agent** 是一个由 LLM（大语言模型）驱动的自动化计算生物学工作流工具。它将 LangChain / LangGraph 的智能 Agent 架构与 Rosetta 分子建模套件深度结合，专为高通量蛋白质工程和结构改造设计。

本项目实现了从配体参数化、复合物构象弛豫、晶体学编号自动映射，到多核高通量饱和突变（ddG 计算）的 **端到端物理流水线自动化**，并提供智能化的报错修复机制。

---

## 核心特性

- **LLM 智能驱动**：基于 LangGraph 构建的状态机 Agent，能够根据自然语言指令自动调度底层 Rosetta 工具，并在遇到缺少文件或计算崩溃时触发**自动拦截与自我修复**。
- **流水线**：内置严格的强制执行规则，依次完成：`parameterize_ligand` ➡️ `run_cartesian_relax` ➡️ `run_saturation_mutagenesis`，拒绝大模型“幻觉”；`get_rosetta_numbering` 作为只读核对工具供人工确认编号映射。
- **错峰高并发计算**：专为本地多核高性能计算节点优化，支持 Python 原生多线程并发执行突变任务，极大缩短计算周期。
- **任务隔离**：每次任务都会自动创建带有时间戳的独立工作区（如 `task_YYYYMMDD_HHMM`），保证多任务并发时的文件系统绝对整洁，绝不串车。
- **双端支持**：提供基于 Gradio 的现代化 Web 控制台与专为服务器后台挂载设计的 CLI 命令行模式。

---

##环境依赖

在运行本项目之前，请确保你的 Linux 服务器已安装以下计算套件：

1. **Python 环境**: Python 3.10+
2. **Rosetta Suite**: 需要编译包含 `relax` 和 `cartesian_ddg` 功能的可执行文件。
3. **PyMOL**: 用于运行后端的配体加氢与 MOL2 格式转换。
4. **前端对接**: 在将复合物送入本 Agent 之前，建议先使用 AutoDock Vina 完成对接，或通过 GROMACS 分子动力学模拟提取出高质量的结合快照。

### Python 库安装

```bash
pip install -r requirements.txt
```
*(主要依赖包括：`langchain-openai`, `langgraph`, `gradio`, `python-dotenv`, `chromadb` 等)*

---

##配置指南

在项目根目录下创建一个 `.env` 文件，并根据你的服务器实际路径进行配置：

```env
# ==============================
# LLM 模型配置
# ==============================
LLM_API_KEY=""
LLM_BASE_URL=""
LLM_MODEL=""

# ==============================
# 底层生信软件路径配置
# ==============================
ROSETTA_BIN_DIR="/opt/software/rosetta/main/source/bin"
ROSETTA_SCRIPTS_DIR="/opt/software/rosetta/main/source/scripts/python/public"
PYMOL_BIN="/usr/local/bin/pymol"
```

---

## 使用方法

### 模式一：Web 图形化控制台
直接运行以下命令启动服务：
```bash
python rosetta_agent.py
```
- 打开浏览器访问 `http://0.0.0.0:7777`。
- 上传 PDB 蛋白结构（如有配体则一并上传）。
- 在侧边栏配置突变位点（支持多位点，如 `76, 21`）并点击**提交执行**。
- 流水线全部跑通后，可一键将结果打包为 `.zip` 下载。

### 模式二：CLI 批量提交模式

针对长时间的高通量饱和突变扫描任务，推荐使用 `run_cli.sh` 脚本批量提交 PDB 文件：

**1. 准备 PDB 文件**
将待处理的 PDB 文件放入 `inputs/pdbs/` 目录下：

```bash
mkdir -p inputs/pdbs
cp /path/to/your/target1.pdb /path/to/your/target2.pdb inputs/pdbs/
```

**2. 一键批量提交**
```bash
bash run_cli.sh
```

脚本会自动遍历 `inputs/pdbs/` 下的所有 `.pdb` 文件，依次执行完整的流水线：
配体参数化 → 结构弛豫 → 饱和突变（ddG 计算），并汇总到 `tool5_summary/ddg_results.txt`。

如需自定义突变位点或并发参数，可直接编辑 `run_cli.sh` 中的 `--mutation` 和 `--params` 选项。`run_cli.sh` 内部本质仍调用 `python rosetta_agent.py --mode cli`，因此也支持单独使用：

```bash
python rosetta_agent.py --mode cli --protein complex.pdb --mutation 76,73 --params "并发数 48, 延迟 1.5"
```

---

## 编号体系说明（重要）

本项目全程使用 **PDB 晶体学编号**（即 PDB 文件 `ATOM` 记录中的残基号）作为唯一输入接口，
例如 `--mutation 76,73` 表示 PDB 编号 76 与 73 两个位点。

- `run_saturation_mutagenesis` 接收 PDB 晶体学编号，内部自动通过共享函数 `_build_rosetta_numbering`
  完成到 Rosetta 绝对行号（CA 原子按顺序从 1 开始）的映射，**该转换在整个流水线中只会发生一次**。
- `get_rosetta_numbering` 为只读核对工具，可展示"PDB 编号 → Rosetta 绝对行号"的对应关系。
  请勿把它输出的绝对行号再传回 `run_saturation_mutagenesis`，否则会造成双重偏移错位。

---

## 目录结构说明

运行任务后，系统将自动生成任务文件夹，内含完整的流转日志与结果：

```text
├── rosetta_agent.py       # 主程序入口 (Agent 路由与界面构建)
├── rosetta_tools.py       # 底层工具库 (调用 Rosetta/PyMOL 等模块)
├── task_20260822_1020/    # 独立任务沙盒
│   ├── protein.pdb        # 原始输入蛋白
│   ├── ligand.pdb         # 原始输入配体
│   ├── tool2_params/      # 参数化结果 (.params, mol2)
│   ├── tool3_relax/       # 结构弛豫结果 (wt_relaxed.pdb)
│   ├── tool4_mut_results/ # 突变执行日志与具体 .ddg 文件
│   └── tool5_summary/     # 最终自动清洗解析的 ddG 汇总表
├── .env                   # 环境变量配置
└── README.md              # 项目说明
```

---

## 贡献与支持

欢迎提交 Issue 和 Pull Request！如果在高通量计算中遇到死锁或 PDB 格式报错问题，请附带 `tool4_mut_results` 下的具体 Log 文件以供排查。
