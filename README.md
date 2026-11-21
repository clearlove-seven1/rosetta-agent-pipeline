# Rosetta Agent

把 Rosetta 三段流水线（**配体参数化 → Cartesian 弛豫 → 饱和突变 ΔΔG**）用 LLM（LangGraph）串起来，跑高通量突变扫描；跑完可选接 SKEMPI / PDBbind 数据做**展示性对照（默认「未评测」，不做可信度结论）**。另有一个 **ESM-2 disorder 探索性信号节点（默认关闭）**，开启后也仅作展示、不自动剔除位点。

> 既不是"套壳 LLM 调 Python 脚本"，也不是"裸 bash 跑 Rosetta"——把"顺序、权限、错误恢复"全部编码进图拓扑，prompt 只负责说明意图。

---

## 0. 科学口径与已知边界（v0.1，请先读）

本项目正按 `PROTEIN_WORKBENCH_PLAN.md` 渐进升级为可追溯、可复现、可评估的科研工作台。v0.1 起，以下口径是**默认行为**：

1. **cartesian_ddg = 常规稳定性模式**，在"蛋白+配体复合物"上计算，输出 **ΔΔG（REU）**。它**不是结合自由能**，不等于实验亲和力。
2. **SKEMPI / PDBbind 对照默认只展示数据，状态为「未评测」**——自动相关性与"强/中/弱可信度"评级已默认停用（`WB_EXPLORATORY_EVAL_ENABLED=0`）。原因：预测量（稳定性 ΔΔG / 复合物总分）与实验标签（结合 ΔΔG / Kd）物理量不同，且 SKEMPI 匹配 key 链无关、多链同位点可能串位。显式开启后也只输出**探索性**相关系数，不构成可信度结论。
3. **ESM-2 "disorder" 节点默认关闭**（`WB_DISORDER_FILTER_ENABLED=0`）：原实现取未 mask 序列的 log-prob 做线性映射，既非 masked LM 也未在任何无序数据集上校准，不得称为"无序概率"。开启后输出也仅是**探索性信号**，不自动排除任何突变位点。
4. **计算筛选 ≠ 实验验证**。任何 Rosetta / MD / ESM 输出都不应被表述为已验证的实验结论。

开关见 `.env.example`；改动依据与证据见 `docs/current_state.md` 与 `PROGRESS.md`。

---

## 1. 仓库结构

```
rosetta-agent/
├── rosetta_agent.py        # 主程序：LangGraph 图 + Gradio Web / CLI 入口
├── rosetta_server.py       # FastAPI+SSE 平台（端口 7779，static/ 前端）：任务提交/进度/产物/解读
├── rosetta_tools.py        # 底层工具：Rosetta / PyMOL / SKEMPI / PDBbind / RAG
├── rosetta_mcp_server.py   # MCP Server：16 原子工具 + 3 管理工具暴露给 Claude Desktop / Cursor / Trae
├── task_runtime.py         # 任务公共层：任务 ID/路径校验/位点解析（唯一实现）/持久任务状态
├── eval_v01_guardrails.py  # v0.1 科学口径护栏测试（26 项）
├── eval_p0_fixes_20261120.py # P0 编号/隔离/状态修复测试（75 项）
├── run_cli.sh              # 批量 CLI 驱动 (循环 inputs/pdbs/*.pdb)
├── setup_blackwell.sh      # Blackwell GPU 服务器上的 torch cu128 一键切换
├── environment.yml         # Conda 环境 (Python 3.10 + langchain/langgraph/gradio …)
├── .env.example            # 配置模板 (复制为 .env 后填入真实 KEY / 路径)
├── .gitignore              # 忽略 .env / 大型 PDB/mol2 / output / __pycache__ / .clinerules / learn_*
├── inputs/                 # 输入 (PDB / 配体)，结构由 .gitkeep 占位
│   ├── pdbs/
│   └── ligand/
├── output/                 # 运行产物 (每个任务一个 task_YYYYMMDD_HHMMSSxx_<PDB>/)
│   └── task_*/task_status.json   # 任务持久状态（重启后仍可查询）
└── rosetta_manuals/        # RAG 语料 + 实验数据库
    ├── rosetta_docs/                 # RosettaCommons/documentation 官方文档（relax/cartesian-ddG/打分/排错…）
    ├── 报错解决笔记.txt
    ├── skempi_v2.csv                 # 蛋白-蛋白 ΔΔG (~7k 条)
    └── pdbbind_v2020.csv             # 蛋白-小分子 Kd/Ki/IC50（源自 PDBbind 2020，~19k 条）
```

---

## 2. 框架（LangGraph）

```
主图 rosetta_agent.py:
  START → router ─┬─ pipeline (子图) → skempi_eval → summarize → END
                   └─ reply ⇄ reply_tools ─────────────────────────┘

子图 pipeline (build_pipeline_subgraph):
  step1_llm ─(tool_call)→ step1_tool ─(成功)→ step2_llm → step2_tool ─(成功)→ disorder_check → step3_llm → step3_tool
       │                     │                            │                │                    │               │
   (无tool_call)         (失败→repair)                (失败→repair)     (失败→repair)       (默认关闭,        (失败→repair)
       ▼                     ▼                            ▼                ▼                仅播报)              ▼
      END                  repair ◀───────────────────── repair ◀──────── repair ◀─────────────  repair
                              │
                          (tool_call)
                              ▼
                          repair_tools ─(retry_count 超限 → END / 否则按 failed_step 回退)
```

**关键设计**：

- 每个 step 的 LLM 都 `bind_tools([本步工具])`，把"绝对禁止调用"从 prompt 文字变成 **schema 物理约束**——LLM 想调别的也调不出来。
- `disorder_check` 节点**默认关闭**（`WB_DISORDER_FILTER_ENABLED=0`）：关闭时只播报「未评估」，不推理、不排除位点；显式开启后输出 ESM-2 启发式**探索性信号**（该分数未在任何无序数据集上校准，不是"无序概率"），不自动过滤位点。详见 §5。
- 错误恢复走专用 `repair` 分支（调 `check_logs` / `diagnose_and_fix` / `auto_repair`），而不是把 force-fix 写进 prompt。
- 位点解析只有一个实现（`task_runtime.resolve_targets`）：提交预检、`get_rosetta_numbering` 核对、`run_saturation_mutagenesis` 执行、`run_fixbb_design` 四处共用，保证"预检通过的位点 = 执行时映射的位点"。

---

## 3. 流水线三步

| Step | 工具 | 作用 |
|---|---|---|
| 1 | `parameterize_ligand` | 调 Rosetta `molfile_to_params.py` 把 `ligand.mol2/.pdb` 转 `.params`，自动检测 3 字代号 |
| 2 | `run_cartesian_relax` | 物理清洗 PDB + 拼接配体，3 路并发跑 `relax.linuxgccrelease -relax:cartesian -score:weights ref2015_cart` |
| 3 | `run_saturation_mutagenesis` | 调 `cartesian_ddg` 对指定位点跑 19 氨基酸饱和扫描，自动建任务文件夹 + 提交并发 |

### 编号铁律

只接受 **PDB 晶体学编号**（如 `76,73`，多链/插入码建议显式写 `A_76`、`A_100A`）。`run_saturation_mutagenesis` 内部通过 `task_runtime.build_numbering` 自动映射到 Rosetta 绝对行号，整条流水线只做一次。

- **多链蛋白同号残基必须带链 ID**（如 `A_76`）：无链输入遇到多链同号会明确报错列出候选链，不会静默取第一条链。
- 插入码（如 `100A`）按 编号+插入码 精确匹配，不会被折叠成 100。
- 错误链名、不存在的位点、`B76` 这类歧义格式一律**报错**，不做"提取数字"式的猜测匹配。
- `get_rosetta_numbering` 只读核对，**别把它的绝对行号回传给 `run_saturation_mutagenesis`**，会双重偏移。
- 诚实边界：绝对行号是按弛豫清洗规则静态推断的 Rosetta pose 编号；极端情况（零占用度原子等）以实际 Rosetta 日志为准（任务目录 `saturation_mutagenesis/rosetta_tasks.txt` 的 `# [编号映射]` 注释行为审计记录）。

---

## 4. 实验对照

跑完饱和突变后，主图自动接 `binding_eval` 节点，**同时**评估两类结合：

### 4.1 蛋白-蛋白（SKEMPI 2.0）

把 `summary/ddg_results.txt` 的预测 ΔΔG 跟 [SKEMPI 2.0](https://github.com/youpze/ScML/blob/main/skempi_v2.csv) 里同位点的实验 ΔΔG 比对，算 Pearson 相关性。

### 4.2 蛋白-小分子（PDBbind v2020）

把 `cartesian_relax/score.sc` 的 `total_score` 跟 [PDBbind v2020](http://www.pdbbind.org.cn/) 里同 PDB 的实测 Kd 比对，算 Spearman 秩相关（**期望负相关**：Rosetta 越低 ≈ Kd 越小）。

### 4.3 触发条件

1. **真实 PDB ID**：任务标签（末段）填真实 4 字符 PDB 代码（`task_<日期>_<时间+后缀>_<PDB>`；旧任务的历史格式同样兼容）
2. **数据库收录**：
   - SKEMPI：蛋白-蛋白相互作用体系（**不包括**单链酶如 1MC9）
   - PDBbind：蛋白-小分子复合物（HIV protease / thrombin / carbonic anhydrase 等有大量配体）
3. **多配体评估（PDBbind）**：需要同一蛋白 ≥3 个不同 PDB ID 才有 Spearman ρ

### 4.4 阈值（历史设计，v0.1 起默认停用）

下表为早期设计中的分级设想，**当前默认不产出任何评级**：

| 数据库 | 指标 | 强 | 中 | 弱 |
|---|---|---|---|---|
| SKEMPI | Pearson r | >0.7 | 0.4 – 0.7 | <0.4 |
| PDBbind | Spearman ρ（负相关）| <-0.6 | -0.6 ~ -0.3 | >-0.3 |

> **v0.1 起上表评级已默认停用**：对照工具只输出数据表与「未评测」状态；显式开启 `WB_EXPLORATORY_EVAL_ENABLED=1` 后仅输出探索性相关系数并注明局限，**不输出强/中/弱结论**。预测量与实验标签物理量不同（REU vs kcal/mol；复合物总分 vs Kd），评级缺乏依据。

### 4.5 不在数据库怎么办

- **优雅降级**：节点返回 `未找到 PDB XXX 的记录`，**不影响后续 summarize**
- **想 demo**：用真实 PDB 重新跑（PDBbind 里 HIV protease / thrombin 都好找）

---

## 5. ESM-2 disorder 过滤

> **v0.1 默认关闭**（`WB_DISORDER_FILTER_ENABLED=0`）：节点记「未评估」，不推理、不排除位点。原实现的分数不是经过验证的无序概率——它对未 mask 的序列取 log-prob 再线性映射，未在任何无序数据集上校准。开启后输出仅作探索性信号展示，不自动排除任何突变位点。以下为原设计背景，保留备查。

跑完弛豫后、`run_saturation_mutagenesis` 之前，`disorder_check` 节点原设计用 ESM-2 t12 35M 给每个残基打分（≥0.5 视为高信号）。

### 为什么需要

MD 抽取的结构（如 `best_relaxed_98840.pdb` 来自 1MC9 经 MD）会把无序区域"强行稳定"成有构象的样子，**但这是 MD 模拟的人为结果**。在这些区域跑 Rosetta ΔΔG 预测无意义。

### 双策略自动回退

- **优先 ESM-2**：本地推理，需要 `fair-esm` + `torch`
- **失败回退**：B-factor 归一化 + 残基序号 gap 启发式（无外部依赖）

不装 fair-esm 也能跑，精度低一些。

---

## 6. 任务目录结构

```text
output/
└── task_<日期>_<时分秒+随机后缀>_<标签>/   # 同标签同秒重复提交不会再互相覆盖；
    │                                      # <标签> 为清洗后的文件名/PDB 代码（真实 PDB 代码结尾才能触发实验对照）
    ├── protein.pdb
    ├── ligand.pdb / ligand.mol2
    ├── task_status.json             # 任务持久状态：status(created/running/completed/failed/interrupted)、
    │                                #   阶段/错误/产物路径/runner 进程号/manifest（输入结构、位点、参数、
    │                                #   整合边界；服务重启后仍可查询，GROMACS 整合读取此文件）
    ├── parameterize_ligand/         # .params / .mol2 / *_0001.pdb
    ├── cartesian_relax/             # wt_relaxed.pdb / score.sc / *.log / run_relax_parallel.py
    ├── saturation_mutagenesis/      # .ddg / run_ddg.py / rosetta_tasks.txt(JSON 参数列表任务行+编号审计注释)
    ├── disorder/                    # disorder_scores.csv（ESM-2 全量分数，仅显式开启时）
    └── summary/
        ├── ddg_results.txt          # ΔΔG(REU, min(MUT)-min(WT))，按 链→残基→数值 排序
        ├── ddg_chart.png            # 结果条形图（7779 平台生成）
        └── binding_eval_report.txt  # SKEMPI + PDBbind 对照（默认「未评测」数据表）
```

---

## 7. 安装

### 7.1 系统依赖

- **Rosetta**（编译含 `relax` 与 `cartesian_ddg`）
- **PyMOL**（配体加氢 / mol2 转换；未上传复合物时自动分离配体）

### 7.2 Python 环境

```bash
conda env create -f environment.yml
conda activate rosetta-agent
```

### 7.3 可选：ESM-2 disorder（推荐）

```bash
pip install fair-esm torch --index-url https://download.pytorch.org/whl/cpu
```

首次跑会从 Meta 服务器下载 ESM-2 t12 35M 模型（~60MB）到 `~/.cache/torch/hub/`。

### 7.4 GPU 服务器（Blackwell，PRO 5000）

```bash
bash setup_blackwell.sh    # 卸 CPU torch，装 cu128 wheel，跑 ESM-2 测试
```

### 7.5 配置

复制 [.env.example](.env.example) 为 `.env` 并填入：

```env
LLM_API_KEY="..."          # 兼容 OpenAI 协议即可（第三方 / Ollama / vLLM）
LLM_BASE_URL="..."         # 例如 https://api.openai.com/v1 或 http://127.0.0.1:11434/v1
LLM_MODEL="..."            # 例如 gpt-4o 或 qwen2.5:32b

ROSETTA_BIN_DIR=/opt/software/rosetta/main/source/bin
ROSETTA_SCRIPTS_DIR=/opt/software/rosetta/main/source/scripts/python/public
PYMOL_BIN=/usr/local/bin/pymol
```

---

## 8. 使用

### 8.1 Web 控制台

```bash
python rosetta_agent.py
```

浏览器开 `http://0.0.0.0:7777`，上传 PDB / 配体 / 突变位点，点提交。后台跑完会自动轮询播报，完成后可一键打包下载 `.zip`。

### 8.2 CLI 单跑

```bash
python rosetta_agent.py --mode cli \
  --protein complex.pdb \
  --mutation 76,73 \
  --params "并发数 4, 延迟 3"
```

### 8.3 CLI 批量

```bash
mkdir -p inputs/pdbs && cp target*.pdb inputs/pdbs/
bash run_cli.sh
```

`run_cli.sh` 内部就是上面这条命令的循环。

### 8.4 MCP（接入 Claude Desktop / Cursor / Trae）

```bash
pip install mcp    # 已列入 environment.yml
python rosetta_mcp_server.py                                  # stdio 模式（本地接入默认）
python rosetta_mcp_server.py --transport sse --port 8000     # SSE 模式（远程接入）
```

客户端配置（Claude Desktop 的 `claude_desktop_config.json` 或 Cursor 的 `.cursor/mcp.json`）：

```json
{
  "mcpServers": {
    "rosetta-agent": {
      "command": "/home/xxx/.conda/envs/rosetta-agent/bin/python",
      "args": ["/abs/path/to/rosetta_mcp_server.py"]
    }
  }
}
```

暴露内容：**16 个原子工具**（与 LangGraph 共用同一实现，零逻辑重复）+ **3 个管理工具**（`create_task` 建任务、`list_tasks` 看产物状态与持久状态、`set_workspace` 切工作区）。每个原子工具支持**显式传 `task_id`** 单次绑定任务（多任务交错时推荐）；不传则作用于"当前工作区"（兼容旧编排）。客户端 LLM 直接编排三段流水线：

`create_task → parameterize_ligand → run_cartesian_relax → predict_disorder → run_saturation_mutagenesis → evaluate_against_skempi / evaluate_against_pdbbind`

### 8.5 平台（FastAPI+SSE，推荐日常使用）

```bash
python rosetta_server.py            # 0.0.0.0:7779，浏览器打开 http://<服务器IP>:7779
```

上传 PDB(+可选配体)+位点 → 自动预检（位点不存在/多链歧义立即 400）→ 三段流水线 → SSE 实时进度 → 结果解读图表 → 一键打包下载。历史任务列表含持久状态（completed/failed/running/interrupted，服务重启后仍可查询：`GET /api/tasks/{id}/status`）。重启后 SSE 事件流不可回放（内存态），LangGraph 对话历史也不可恢复——续聊接口会在新上下文中明示这一点。

### 8.6 Workbench（已迁出）

Protein Design Workbench（CLI / GROMACS MD / MM/PBSA / 网页智能体）已于 2026-11-14 迁出至独立仓库 **`~/projects/ros-gro`**（含全部 runs 数据与文档）。本仓库保留旧 Rosetta-Agent 流水线（§8.1–8.5）。访问智能体网页：`http://<服务器IP>:7860`（由 ros-gro 提供）。

---

## 9. 排错与状态定位

| 现象 | 排查方向 |
|---|---|
| 任务状态不明 | 先看 `output/<task>/task_status.json`（status/status_note/runner_pids/manifest）；平台任务再查 `GET /api/tasks/{id}/status` |
| 服务重启后任务还能查吗 | 能查状态与产物（状态文件在任务目录内）；SSE 事件与对话历史不可回放；`status=running` 时系统按 runner 进程存活+产物自动判 running/interrupted，判不了会标「待核实」 |
| 位点预检 400 | 报错会列出链/残基信息与逐位点原因；用 `get_rosetta_numbering` 核对；多链同号必须带链（A_76）；插入码写 100A |
| `.ddg` 文件 0 字节 | 看 `saturation_mutagenesis/run_launcher.log` 与对应 `mut_*.log` 末尾的 `[exit code N]`，多半并发过大打挂 Rosetta |
| 弛豫中途 `score.sc` 写不出来 | `cartesian_relax/` 下 `relax_run_*.log` 末尾有各路退出码；`score_*.sc` 是否都在 |
| 报"找不到 .params" | 确认 `parameterize_ligand/` 里有 `.params` 落盘 + 目录权限 |
| SKEMPI 报"未找到 PDB XXX" | 该 PDB 不在 SKEMPI 收录范围（SKEMPI 只有蛋白-蛋白相互作用） |
| SKEMPI 报"位点重叠 0" | `ddg_results.txt` 里的 key 格式与 SKEMPI 突变解析对不上（链不参与匹配） |
| ESM-2 报"fair_esm 未安装" | 跑 `pip install fair-esm torch`，或接受 B-factor 启发式回退（均默认不参与过滤） |
| ΔΔG 是什么单位 | **REU**（Rosetta 能量单位），min(MUT)-min(WT)；是计算筛选指标，不是实验结合自由能，不等于实验验证 |

---

## 10. 致谢

- [Rosetta](https://www.rosettacommons.org/) — `relax` / `cartesian_ddg` / `molfile_to_params.py`
- [ESM-2 (Meta FAIR)](https://github.com/facebookresearch/esm) — 蛋白质语言模型 disorder 预测
- [SKEMPI 2.0](https://life.bsc.es/pid/skempi2) — 蛋白-蛋白 ΔΔG 实验数据库
- [PDBbind v2020](http://www.pdbbind.org.cn/) — 蛋白-小分子 Kd 实验数据库
- [Leak-Proof PDBBind](https://github.com/THGLab/LP-PDBBind) — `pdbbind_v2020.csv` 的数据来源（重新整理的 PDBbind 2020 亲和力）
- [LangChain / LangGraph](https://langchain-ai.github.io/langgraph/) — Agent 框架