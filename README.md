# Rosetta Agent

把 Rosetta 三段流水线（**配体参数化 → Cartesian 弛豫 → 饱和突变 ΔΔG**）用 LLM（LangGraph）串起来，跑高通量突变扫描，跑完自动接 SKEMPI / PDBbind 实验数据库做可信度评估。中间还嵌了一个 **ESM-2 disorder** 过滤节点，避免在结构不稳定区域浪费 Rosetta 计算。

> 既不是"套壳 LLM 调 Python 脚本"，也不是"裸 bash 跑 Rosetta"——把"顺序、权限、错误恢复"全部编码进图拓扑，prompt 只负责说明意图。

---

## 1. 仓库结构

```
rosetta-agent/
├── rosetta_agent.py        # 主程序：LangGraph 图 + Gradio Web / CLI 入口
├── rosetta_tools.py        # 底层工具：Rosetta / PyMOL / SKEMPI / PDBbind / RAG
├── rosetta_mcp_server.py   # MCP Server：12 原子工具 + 3 管理工具暴露给 Claude Desktop / Cursor / Trae
├── run_cli.sh              # 批量 CLI 驱动 (循环 inputs/pdbs/*.pdb)
├── setup_blackwell.sh      # Blackwell GPU 服务器上的 torch cu128 一键切换
├── environment.yml         # Conda 环境 (Python 3.10 + langchain/langgraph/gradio …)
├── .env.example            # 配置模板 (复制为 .env 后填入真实 KEY / 路径)
├── .gitignore              # 忽略 .env / 大型 PDB/mol2 / output / __pycache__ / .clinerules / learn_*
├── inputs/                 # 输入 (PDB / 配体)，结构由 .gitkeep 占位
│   ├── pdbs/
│   └── ligand/
├── output/                 # 运行产物 (每个任务一个 task_YYYYMMDD_HHMM_<PDB>/)
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
  START → router ─┬─ pipeline (子图) → binding_eval → summarize → END
                   └─ reply ⇄ reply_tools ───────────────────────────┘

子图 pipeline (build_pipeline_subgraph):
  step1_llm ─(tool_call)→ step1_tool ─(成功)→ disorder_check → step2_llm → step2_tool
       │                     │                    │                 │
   (无tool_call)         (失败→repair)        (≥阈值位点         (失败→repair)
       ▼                     ▼                  注入 step3)           ▼
      END                  repair ◀────────────── repair ◀──────────  repair
                              │
                          (tool_call)
                              ▼
                          repair_tools ─(retry_count 超限 → END / 否则按 failed_step 回退)
```

**关键设计**：

- 每个 step 的 LLM 都 `bind_tools([本步工具])`，把"绝对禁止调用"从 prompt 文字变成 **schema 物理约束**——LLM 想调别的也调不出来。
- `disorder_check` 节点是真正的 ML 推理：用 [ESM-2 t12 35M](https://github.com/facebookresearch/esm) 给每个残基打 disorder 概率，≥0.5 视为无序，结果注入 `step3_llm` 的上下文作为警告。
- 错误恢复走专用 `repair` 分支（调 `check_logs` / `diagnose_and_fix` / `auto_repair`），而不是把 force-fix 写进 prompt。

---

## 3. 流水线三步

| Step | 工具 | 作用 |
|---|---|---|
| 1 | `parameterize_ligand` | 调 Rosetta `molfile_to_params.py` 把 `ligand.mol2/.pdb` 转 `.params`，自动检测 3 字代号 |
| 2 | `run_cartesian_relax` | 物理清洗 PDB + 拼接配体，3 路并发跑 `relax.linuxgccrelease -relax:cartesian -score:weights ref2015_cart` |
| 3 | `run_saturation_mutagenesis` | 调 `cartesian_ddg` 对指定位点跑 19 氨基酸饱和扫描，自动建任务文件夹 + 提交并发 |

### 编号铁律

只接受 **PDB 晶体学编号**（如 `76,73`）。`run_saturation_mutagenesis` 内部通过 `_build_rosetta_numbering` 自动映射到 Rosetta 绝对行号，整条流水线只做一次。

- `get_rosetta_numbering` 只读核对，**别把它的绝对行号回传给 `run_saturation_mutagenesis`**，会双重偏移。

---

## 4. 实验对照

跑完饱和突变后，主图自动接 `binding_eval` 节点，**同时**评估两类结合：

### 4.1 蛋白-蛋白（SKEMPI 2.0）

把 `summary/ddg_results.txt` 的预测 ΔΔG 跟 [SKEMPI 2.0](https://github.com/youpze/ScML/blob/main/skempi_v2.csv) 里同位点的实验 ΔΔG 比对，算 Pearson 相关性。

### 4.2 蛋白-小分子（PDBbind v2020）

把 `cartesian_relax/score.sc` 的 `total_score` 跟 [PDBbind v2020](http://www.pdbbind.org.cn/) 里同 PDB 的实测 Kd 比对，算 Spearman 秩相关（**期望负相关**：Rosetta 越低 ≈ Kd 越小）。

### 4.3 触发条件

1. **真实 PDB ID**：任务目录名必须以真实 4 字符 PDB 代码结尾（`task_YYYYMMDD_HHMM_<PDB>`）
2. **数据库收录**：
   - SKEMPI：蛋白-蛋白相互作用体系（**不包括**单链酶如 1MC9）
   - PDBbind：蛋白-小分子复合物（HIV protease / thrombin / carbonic anhydrase 等有大量配体）
3. **多配体评估（PDBbind）**：需要同一蛋白 ≥3 个不同 PDB ID 才有 Spearman ρ

### 4.4 阈值

| 数据库 | 指标 | 强 | 中 | 弱 |
|---|---|---|---|---|
| SKEMPI | Pearson r | >0.7 | 0.4 – 0.7 | <0.4 |
| PDBbind | Spearman ρ（负相关）| <-0.6 | -0.6 ~ -0.3 | >-0.3 |

### 4.5 不在数据库怎么办

- **优雅降级**：节点返回 `未找到 PDB XXX 的记录`，**不影响后续 summarize**
- **想 demo**：用真实 PDB 重新跑（PDBbind 里 HIV protease / thrombin 都好找）

---

## 5. ESM-2 disorder 过滤

跑完弛豫后、`run_saturation_mutagenesis` 之前，`disorder_check` 节点用 ESM-2 t12 35M 给每个残基打 disorder 概率（≥0.5 视为无序）。

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
└── task_20260827_1934_<pdb_id>/     # <pdb_id> 必须是真实 PDB 代码才能触发 SKEMPI 评估
    ├── protein.pdb
    ├── ligand.pdb / ligand.mol2
    ├── parameterize_ligand/         # .params / .mol2 / *_0001.pdb
    ├── cartesian_relax/             # wt_relaxed.pdb / score.sc / *.log
    ├── saturation_mutagenesis/      # .ddg / run_ddg.py / run_launcher.log
    ├── disorder/                    # disorder_scores.csv（ESM-2 全量分数）
    └── summary/
        ├── ddg_results.txt
        └── binding_eval_report.txt  # SKEMPI + PDBbind 对照
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

暴露内容：**12 个原子工具**（与 LangGraph 共用同一实现，零逻辑重复）+ **3 个管理工具**（`create_task` 建任务、`list_tasks` 看产物状态、`set_workspace` 切工作区）。客户端 LLM 直接编排三段流水线：

`create_task → parameterize_ligand → run_cartesian_relax → predict_disorder → run_saturation_mutagenesis → evaluate_against_skempi / evaluate_against_pdbbind`

---

## 9. 排错

| 现象 | 排查方向 |
|---|---|
| `.ddg` 文件 0 字节 | 看 `saturation_mutagenesis/run_launcher.log`，多半并发过大打挂 Rosetta |
| 弛豫中途 `score.sc` 写不出来 | `cartesian_relax/` 下 `score_*.sc` 是不是 3 个都在 |
| 报"找不到 .params" | 确认 `parameterize_ligand/` 里有 `.params` 落盘 + 目录权限 |
| SKEMPI 报"未找到 PDB XXX" | 该 PDB 不在 SKEMPI 收录范围（SKEMPI 只有蛋白-蛋白相互作用） |
| SKEMPI 报"位点重叠 0" | `ddg_results.txt` 里的 key 格式与 SKEMPI 突变解析对不上（链不参与匹配） |
| ESM-2 报"fair_esm 未安装" | 跑 `pip install fair-esm torch`，或接受 B-factor 启发式回退 |

---

## 10. 致谢

- [Rosetta](https://www.rosettacommons.org/) — `relax` / `cartesian_ddg` / `molfile_to_params.py`
- [ESM-2 (Meta FAIR)](https://github.com/facebookresearch/esm) — 蛋白质语言模型 disorder 预测
- [SKEMPI 2.0](https://life.bsc.es/pid/skempi2) — 蛋白-蛋白 ΔΔG 实验数据库
- [PDBbind v2020](http://www.pdbbind.org.cn/) — 蛋白-小分子 Kd 实验数据库
- [Leak-Proof PDBBind](https://github.com/THGLab/LP-PDBBind) — `pdbbind_v2020.csv` 的数据来源（重新整理的 PDBbind 2020 亲和力）
- [LangChain / LangGraph](https://langchain-ai.github.io/langgraph/) — Agent 框架