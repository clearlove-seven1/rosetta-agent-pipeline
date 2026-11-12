# v0.0 基线盘点报告

> **Protein Design Workbench 升级前置盘点**（对应 PROTEIN_WORKBENCH_PLAN.md §6 v0.0）
> 检查日期：2026-11-12 ｜ 检查基线：Git commit `1945a14`（main，工作区干净）
> 检查对象：amax 服务器 `~/projects/rosetta1` **实际工作副本**（非公开仓库快照）
> 性质：**纯只读检查**，未修改任何代码、数据与环境。本报告不含任何密钥。

---

## 1. 一句话回答（v0.0 完成标准）

- **现在哪里能运行**：amax 服务器；conda env `rosetta-agent`（Python 3.10.20）+ Rosetta（gcc-9 release 构建）+ PyMOL 均已就位；LLM 走 MiniMax-M3 API（密钥在 `.env`，已加载）。
- **算的是什么**：`cartesian_ddg`（**常规稳定性模式**）在"蛋白+配体复合物"结构上对指定位点跑 19 氨基酸饱和扫描，输出 ΔΔG（**REU**）；弛豫用 `relax.linuxgccrelease -relax:cartesian -score:weights ref2015_cart`（nstruct=3 并发）。
- **输出在哪**：`output/task_<时间>_<名称>/{parameterize_ligand, cartesian_relax, saturation_mutagenesis, disorder, summary}`。
- **最大风险**：**预测量与对照标签不匹配**——把复合物稳定性 ΔΔG 拿去对照 SKEMPI 结合 ΔΔG、把复合物 total_score 拿去对照 PDBbind Kd 并输出"强/中/弱可信度"；叠加 ESM-2"无序概率"无验证依据并参与位点过滤（详见 §4）。

## 2. 仓库与入口

| 入口 | 位置 | 说明 |
|---|---|---|
| Gradio Web | `rosetta_agent.py`（`python rosetta_agent.py`，端口 7777） | `submit_task`（:735）内部 `shell=True` 起 CLI 子进程 |
| CLI 单跑 | `rosetta_agent.py --mode cli`（`run_cli` :859） | **仍需 `LLM_API_KEY`**（:33-40 无 key 直接 raise），目前不存在无 LLM 的执行路径 |
| CLI 批量 | `run_cli.sh` | 循环 `inputs/pdbs/*.pdb`，默认位点 71,72，并发 48 |
| MCP Server | `rosetta_mcp_server.py`（stdio/SSE） | 12 原子工具 + 3 管理工具，与 LangGraph 共用实现 |

调用链（主图）：`router → pipeline子图(step1参数化→disorder_check→step2弛豫→step3饱和突变, repair回路≤2次, progress_poll轮询) → skempi_eval（SKEMPI+PDBbind 对照）→ summarize`；旁路 `reply ⇄ reply_tools`（文档问答）。

工具层：`rosetta_tools.py` 持有全部 Rosetta/PyMOL/评测/RAG 逻辑；工作区切换靠 `TaskAwareToolNode`（`rosetta_agent.py:92`）从 `thread_id` 注入 `ContextVar`。编号映射 `_build_rosetta_numbering`（`rosetta_tools.py:82`）全链路只做一次——该设计经 0933/1004 两次真实任务验证可用，**保留不动**。

## 3. 环境清单（实测值）

| 项 | 实测 | 备注 |
|---|---|---|
| OS | Ubuntu 22.04.5 LTS，kernel 6.8.0-40-generic，x86_64 | |
| CPU | 2× AMD EPYC 9965（192 核/颗），768 线程 | CPU 资源充裕，Rosetta 主要吃 CPU |
| 内存 | 502 GiB（可用 473 GiB） | |
| 磁盘 | `/` 1.8T，已用 873G，**剩 796G** | GROMACS 轨迹需预算 |
| GPU | 2× NVIDIA RTX PRO 5000 72GB（Blackwell），驱动 580.173.02 | 当前均空闲（15 MiB） |
| 调度器 | 无 Slurm | 用户确认：**不需要 Slurm**，单机直跑 |
| **GROMACS** | **已安装**：`/opt/software/gromacs-2026.1`（root 所有，2026-04-29 源码构建）。实测 `gmx --version`：mixed 精度、thread_mpi、OpenMP≤128、**GPU support: CUDA + cuFFT**、AVX2_256、Plumed/Colvars enabled | **不在 PATH**，需 `source GMXRC`。⚠️ 链接断裂：依赖 `libcufft.so.11`（CUDA 12.x 时代 cuFFT），系统现仅有 CUDA 13.0（libcufft.so.12），直接运行报 `libcufft.so.11: cannot open shared object file`。已用用户 conda env `aivc-com` 内 pip nvidia 包自带的 libcufft.so.11（LD_LIBRARY_PATH）**验证 gmx 可正常启动**（只读借用，未改任何环境） |
| Python | `~/.conda/envs/rosetta-agent` = 3.10.20 | LangGraph 1.2.8 / Gradio 6 / mcp 1.29 / fair-esm 2.0 均已装 |
| Rosetta | `/opt/software/rosetta/main/source/bin`：`relax`、`cartesian_ddg` 符号链接 → gcc/9 release 构建（2026-03 建，属主 cbmgroup） | **版本号/commit 未确认**（二进制未跑，待补） |
| Rosetta 脚本 | `molfile_to_params.py` 在 `ROSETTA_SCRIPTS_DIR` ✓ | 参数化链路完整 |
| PyMOL | `/usr/local/bin/pymol` → `/opt/software/pymol/pymol` ✓ | |
| LLM | MiniMax-M3，`api.minimaxi.com`，temperature=0 | 密钥在 `.env`，不外泄 |

## 4. P0 科学问题核实（计划 §2.2 的四项**全部属实**）

### P0-A：ESM-2 分数被称"无序概率"缺依据 — **属实**

- **位置**：`rosetta_tools.py:944-984` `_predict_disorder_esm2`
- **证据**：把**未做 mask 的完整序列**喂给 ESM-2，取每个位置上**野生型氨基酸自身的 log-prob**，再用魔法常数归一化 `confidence=(log_p+3.0)/3.0`，`disorder=1-confidence`。注释自称"masked language modeling 的 disorder 概率"，实际实现既无 mask 也无任何校准。
- **影响**：该值本质是"模型对观测残基的自信度"，与无序倾向只是弱相关；`DISORDER_THRESHOLD=0.5` 无依据。结果经 `predict_disorder`（:986）/ `predict_disorder_struct`（:1075）进入 `disorder_check_node`（`rosetta_agent.py:417-445`），作为 `disordered_sites` 注入 step3 警告，会实际影响突变位点选择。
- **回退路径同病**：`_predict_disorder_heuristic`（:880-923）= 0.7×B-factor 归一化 + 0.3×序号 gap，同样不是无序概率；且 B-factor 含义随结构来源（X-ray/MD/NMR）漂移。

### P0-B：Rosetta total_score vs PDBbind Kd 自动评级 — **属实**

- **位置**：`rosetta_tools.py:1210-1324` `evaluate_against_pdbbind`
- **证据**：取 nstruct 中 `min(total_score)`（约 :1288）对一个 Kd，跨 ≥3 个 PDB 算 Spearman，然后直接输出"✅ 强负相关…""⚠️ 中等…可作初筛""❌ 弱/无相关"等**可信度评级**（约 :1310-1323）。复合物总分 ≠ 结合自由能；跨结构跨体系比 total_score 无物理依据；n≥3 就评级样本量严重不足。另外多记录时取 `matches[0]` 无去重。
- **影响**：报告会产出无依据的"可信度"结论，正是计划 §1.3 第 7 条要杜绝的"编造可信度"。

### P0-C：稳定性 ΔΔG 与结合 ΔΔG 混用 — **属实**

- **位置**：`rosetta_tools.py:329-497` `run_saturation_mutagenesis`（cartesian_ddg 命令约 :415-425）；对照节点 `rosetta_agent.py:585` `skempi_eval_node`；README §4
- **证据**：cartesian_ddg 用的是**常规模式**（对含配体的复合物 `wt_relaxed.pdb` 做突变扫描，`-extra_res_fa` 带配体 params），未用 interface 模式。SKEMPI 对照却拿这些值比**结合** ΔΔG。预测量是"复合物整体稳定性变化"，标签是"结合变化"——量纲同（REU）但物理量不同。
- **影响**：即使相关系数好看，也不能解释为"预测了结合"。当前 README/报告口径未区分。

### P0-D：SKEMPI 匹配 key 丢链 — **属实**

- **位置**：`rosetta_tools.py:676-677` `_skempi_key_from_parsed`（丢弃 `partner` 链）；`_parse_ddg_key`（:641-654）同样丢链
- **证据**：key = `(wt, pos, mut)`，注释明写"链无关"。多链体系里 A:63 与 B:63 的不同突变会**合并/串位**。
- **影响**：跨链位点碰撞 → 相关系数被污染。多突变条目、插入码的处理未逐行验证（列入 §10 未覆盖项）。

## 5. P1 工程问题核实（计划 §2.3）

1. **shell=True + f-string 拼接（≥13 处）**：`rosetta_tools.py:65,189,220,299,323,498,505,509,563`；`rosetta_agent.py:770,889`；`rosetta_mcp_server.py:191`。路径含空格/特殊字符即碎，且 `auto_repair` 会执行拼接命令（任意命令面）。
2. **后台进程 fire-and-forget**：`rosetta_tools.py:323`（relax）与 `:509`（ddg launcher）`Popen(nohup … &)`，不记录 PID、不查退出码；"完成"由 `progress_poll_node`（`rosetta_agent.py:236-287`）**只查 `ddg_results.txt` 是否存在**判定。README 排错表已记载"`.ddg` 0 字节"故障——证明文件存在 ≠ 成功。失败/中断状态目前无法与成功区分。
3. **MCP/全局状态**：`current_workspace` ContextVar 按 `thread_id` 切换（`rosetta_agent.py:92`、`rosetta_mcp_server.py:96`），单任务假设，并发任务会串目录。
4. **硬编码默认路径**：Rosetta/PyMOL 路径均有 `os.getenv` 兜底（`rosetta_tools.py:21-23` 等），单机可接受，记录在案。
5. **许可**：`skempi_v2.csv`、`pdbbind_v2020.csv`、Rosetta（学术授权）能否随公开仓库分发**待你确认**（P0 风险：真实数据/许可外泄）。
6. **GROMACS 运行环境断裂**：`/opt/software/gromacs-2026.1` 的 CUDA 构建依赖 cuFFT 11，系统只剩 CUDA 13.0（cuFFT 12），直接调用失败。临时借用其他 conda env 的 pip nvidia 库可启动，但**跨 env 借库不是可持续方案**（环境被删即失效，且不可追溯）。建议在 workbench 自建独立 env 固化 `libcufft=11`（conda-forge 或 pip `nvidia-cufft-cu12`），由 runner 显式注入 LD_LIBRARY_PATH 并记入 manifest（待你确认方案，见 §9）。

## 6. 现有运行证据

- `output/task_20260910_0933_best_relaxed_98840`、`task_20260910_1004_best_relaxed_99170`：**参数化→弛豫→饱和突变全链路跑通**，`ddg_results.txt` 正常（如 `Q_A71_E = -2.475 REU`）。证明工具层与编号映射可靠。
- 两个任务的 `summary/binding_eval_report.txt` **均缺失**：输入 PDB 名为 MD 命名（非真实 4 字符 PDB 代码），对照按设计优雅跳过——但"报告缺失"与"报告显示未评测"两种状态目前不可区分（v0.3 状态机要解决）。
- `task_20260910_0902`：无 summary，中途失败/中断，无任何状态记录。

## 7. 最小案例可行性结论

**能跑**：软件链路完整、算力充足。但要注意——

- 计划 v0.2 定义的第一版任务是"**单体、单点突变、稳定性**"；当前流水线是"**复合物+配体、多点饱和**"。要么在 v0.2 增加"无配体单体路径"（工具层已天然支持：不传 ligand 即可），要么沿用复合物路径但在报告里如实标注口径——**需你定**（§9）。
- 目前**不存在无 LLM 的执行入口**（`rosetta_agent.py:33-40` 强制要求 API key）：v0.2 需要绕过 LangGraph 直接以确定性脚本驱动 `rosetta_tools.py` 的工具函数。

## 8. v0.1 建议修改清单（待确认后执行）

| # | 改动 | 文件 | 方式 |
|---|---|---|---|
| 1 | disorder 过滤**默认关闭** | `rosetta_agent.py`（disorder_check_node）、`rosetta_tools.py` | 环境变量开关（如 `WB_DISORDER_ENABLED=0`）；关闭时节点输出"未评估"，不注入 `disordered_sites`；开启时报告措辞降级为"探索性信号（未经验证）" |
| 2 | PDBbind 对照**默认关闭自动评级** | `rosetta_tools.py:1210+` | 默认只输出原始数据表（PDB/亲和力/分数/n），删除"强/中/弱"评级输出；标"探索性"并注明样本数与适用条件 |
| 3 | SKEMPI 对照**默认关闭自动可信度** | `rosetta_tools.py:726+`、`rosetta_agent.py:585` | 同上；报告中列输入数/匹配数/排除数；链无关匹配保留但标注"链歧义未消除" |
| 4 | 口径声明 | `rosetta_agent.py`（SUMMARIZE/报告）、`README.md` | 明确"cartesian_ddg 常规模式 @ 复合物 = 稳定性口径，REU，不等于结合亲和力"；增加"计算筛选 ≠ 实验验证"声明 |
| 5 | 针对性测试 | `eval_context_changes.py`（沿用其纯逻辑测试风格） | 断言默认输出不含"无序概率/强可信度/亲和力预测"等表述；断言关闭开关时 `disordered_sites` 为空 |

**保留不动**：LangGraph 图拓扑、MCP 工具面、编号映射、并发弛豫、Gradio/CLI 入口形状——v0.1 只做"表述与默认行为纠偏"，不做重构。

## 9. 待你确认的问题（计划 §14）

- [ ] **v0.2 任务口径**：第一版走"单体无配体"还是保留"复合物+配体"（如实标注）？这决定 v0.2 改造范围。
- [ ] **GROMACS cuFFT 修复方案**：✅ **已决策并执行（2026-11-12，用户选方案 A）**——新建 conda env `workbench` 固化 `nvidia-cufft-cu12==11.3.3.83`，提供 `configs/gromacs_env.sh` 统一加载（source 后注入 LD_LIBRARY_PATH），实测 `gmx --version` 正常（GPU support: CUDA / cuFFT）。真实 mdrun 短测试留待 v0.5。
- [ ] **Rosetta 版本**：是否可以跑一次 `relax -help` 记录版本/commit？授权范围（课题组共享安装）能否随项目文档说明？
- [ ] **数据分发**：`skempi_v2.csv` / `pdbbind_v2020.csv` 是否允许留在（可能公开的）仓库？
- [ ] **第一个测试体系**：建议选一个小单体（如泛素 1UBQ）做 v0.2 最小案例，是否同意？
- [ ] 每周可投入时长与磁盘预算上限（当前剩 796G）。

## 10. 本盘点未覆盖 / 未验证（如实记录）

- Rosetta 二进制具体版本/commit（未执行二进制，避免触发计算）；编译选项未知。
- GROMACS 仅验证到"二进制能启动、版本信息完整"（借用 cuFFT 11 库路径）；**真实 GPU mdrun 短测试未做**（v0.5 首项），CUDA runtime 编译版本、多卡行为未知。
- ESM-2 本地缓存是否存在、推理实测耗时（未运行推理）。
- `_parse_skempi_mutation` 对多突变条目/插入码的完整行为（未构造样例逐条验证）。
- 0933/1004 任务 `binding_eval` 未触发的精确代码路径（推断为 PDB ID 不匹配，未逐步跟踪）。
- Gradio UI 实际显示效果、MCP 客户端接入实测（本轮只读检查，未启动服务）。
- 0902 任务中断的根因（日志未逐条排查）。
