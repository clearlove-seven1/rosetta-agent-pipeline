# 05 · LangGraph 架构与三段式流水线

本章详细拆解 LangGraph 主图与流水子图的拓扑设计。

## 1. 主图架构

主图 = `rosetta_agent.py:10` 章节 10 构建的 `workflow = StateGraph(AgentState)`。

### 1.1 主图节点清单

| 节点 | 类型 | 职责 |
|---|---|---|
| `router` | LLM | 意图分类（pipeline / query / chat） |
| `pipeline` | subgraph | 三段式流水线子图 |
| `reply` | LLM | 普通对话 + 文档检索 |
| `reply_tools` | ToolNode | 执行 reply 调用的工具 |
| `skempi_eval` | 确定性 | 实验对照（SKEMPI + PDBbind） |
| `summarize` | LLM | 统一收尾（bind_tools([]) 禁止再调工具） |

### 1.2 主图拓扑

```
START
  │
  ▼
router
  │
  ├─ intent == "pipeline" ──► pipeline ──► skempi_eval ──► summarize ──► END
  │
  └─ else ──► reply ──┬─ tool_call ──► reply_tools ──┐
                      │                                │
                      └─ no tool_call ──► summarize ◄──┘
                                              │
                                              ▼
                                             END
```

### 1.3 主图代码

```python
workflow = StateGraph(AgentState)

workflow.add_node("router", router_node)
workflow.add_node("pipeline", build_pipeline_subgraph())  # 整个子图当一个节点
workflow.add_node("reply", reply_node)
workflow.add_node("reply_tools", reply_tools_node)
workflow.add_node("skempi_eval", skempi_eval_node)
workflow.add_node("summarize", summarize_node)

workflow.add_edge(START, "router")
workflow.add_conditional_edges("router", main_route_after_router,
    {"pipeline": "pipeline", "reply": "reply"})
workflow.add_edge("pipeline", "skempi_eval")      # 流水线无论成败都进入评估
workflow.add_edge("skempi_eval", "summarize")
workflow.add_conditional_edges("reply", reply_route,
    {"reply_tools": "reply_tools", "summarize": "summarize"})
workflow.add_edge("reply_tools", "reply")          # 工具结果回给 reply 继续生成
workflow.add_edge("summarize", END)

memory = MemorySaver()
agent_executor = workflow.compile(checkpointer=memory)
```

### 1.4 router 节点的快速分类

```python
pipeline_markers = ["突变位点", "目标突变位点", "【系统强制规则】", "【系统强制批量流水线】"]
if any(m in str(last_user) for m in pipeline_markers):
    return {"intent": "pipeline"}
```

通过启发式关键字匹配，**多数情况下不需要 LLM 分类**，节省 token + 加快响应。

## 2. 流水子图架构（核心）

流水子图 = `build_pipeline_subgraph()` 函数，是整个项目最复杂的部分。

### 2.1 子图节点清单（10 个）

| 节点 | 类型 | 职责 |
|---|---|---|
| `step1_llm` | LLM | step1 参数化意图理解（bind_tools=[parameterize_ligand]） |
| `step1_tool` | TaskAwareToolNode | 执行 parameterize_ligand |
| `step2_llm` | LLM | step2 弛豫意图理解（bind_tools=[run_cartesian_relax]） |
| `step2_tool` | TaskAwareToolNode | 执行 run_cartesian_relax |
| `disorder_check` | 确定性 | ESM-2 disorder 预测 |
| `step3_llm` | LLM | step3 突变意图理解（bind_tools=[run_saturation_mutagenesis, get_rosetta_numbering]） |
| `step3_tool` | TaskAwareToolNode | 执行 run_saturation_mutagenesis |
| `progress_poll` | 确定性 | 后台任务轮询 |
| `repair` | LLM | 错误诊断（bind_tools=[check_logs, diagnose_and_fix, auto_repair]） |
| `repair_tools` | TaskAwareToolNode | 执行修复工具 |

### 2.2 子图拓扑（关键）

```
START
  │
  ▼
step1_llm ──(tool_call)──► step1_tool ──(成功)──► step2_llm ──(tool_call)──► step2_tool ──(成功)──► disorder_check
   │                          │                          │                          │
(无tool_call→END)         (失败→repair)             (无tool_call→END)          (失败→repair)
                              │                          │                          │
                              ▼                          ▼                          ▼
                            repair ◄────────────────── repair ◄────────────────── repair
                              │
                          (tool_call)
                              │
                              ▼
                          repair_tools
                              │
                          (retry_count > MAX_RETRIES → END)
                              │
                              ▼
              (按 failed_step 路由：step1/2/3_llm → 回到对应步骤)
                              │
                              ▼
                              ◄──────────── 回到失败步骤重试
```

### 2.3 子图源代码骨架

```python
def build_pipeline_subgraph():
    sg = StateGraph(AgentState)

    # ===== Step 1：参数化 =====
    sg.add_node("step1_llm", _make_step_llm_node(1, STEP1_SYSTEM, STEP1_TOOLS))
    sg.add_node("step1_tool", _make_step_tool_node(1, STEP1_TOOLS))

    # ===== Step 2：弛豫 =====
    sg.add_node("step2_llm", _make_step_llm_node(2, STEP2_SYSTEM, STEP2_TOOLS))
    sg.add_node("step2_tool", _make_step_tool_node(2, STEP2_TOOLS))

    # ===== Step 3：饱和突变 =====
    sg.add_node("step3_llm", _make_step_llm_node(3, STEP3_SYSTEM, STEP3_TOOLS))
    sg.add_node("step3_tool", _make_step_tool_node(3, STEP3_TOOLS))

    # ===== Repair 分支 =====
    repair_llm = llm.bind_tools(REPAIR_TOOLS)
    def repair_node(state):
        sys_prompt = "你是【自动修复 Agent】。当前流水线某一步刚报错..."
        response = repair_llm.invoke([SystemMessage(content=sys_prompt)] + list(state["messages"]))
        return {"messages": [response]}
    repair_node.__name__ = "repair_node"

    def repair_route(state):
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and last.tool_calls:
            return "repair_tools"
        return "__end__"

    def repair_back_route(state):
        if state.get("retry_count", 0) > MAX_RETRIES:
            return "__end__"
        return {1: "step1_llm", 2: "step2_llm", 3: "step3_llm"}.get(state.get("failed_step", 1), "__end__")

    sg.add_node("repair", repair_node)
    sg.add_node("repair_tools", TaskAwareToolNode(REPAIR_TOOLS))

    # ===== 连边 =====
    sg.add_edge(START, "step1_llm")

    # step1 闭环
    sg.add_conditional_edges("step1_llm", _llm_has_tool_call,
        {"tool": "step1_tool", "__next__": "__end__"})
    sg.add_conditional_edges("step1_tool", _post_tool_route("step2_llm"),
        {"step2_llm": "step2_llm", "repair": "repair"})

    # step2 闭环
    sg.add_conditional_edges("step2_llm", _llm_has_tool_call,
        {"tool": "step2_tool", "__next__": "__end__"})
    sg.add_conditional_edges("step2_tool", _post_tool_route("disorder_check"),
        {"disorder_check": "disorder_check", "repair": "repair"})

    # disorder_check → step3
    sg.add_node("disorder_check", disorder_check_node)
    sg.add_edge("disorder_check", "step3_llm")

    # step3 闭环
    sg.add_conditional_edges("step3_llm", _llm_has_tool_call,
        {"tool": "step3_tool", "__next__": "__end__"})
    sg.add_conditional_edges("step3_tool", _post_tool_route("progress_poll"),
        {"progress_poll": "progress_poll", "repair": "repair"})

    # progress_poll 自循环
    sg.add_node("progress_poll", progress_poll_node)
    sg.add_conditional_edges("progress_poll", progress_poll_route,
        {"progress_poll": "progress_poll", "__end__": "__end__"})

    # repair 闭环
    sg.add_conditional_edges("repair", repair_route,
        {"repair_tools": "repair_tools", "__end__": "__end__"})
    sg.add_conditional_edges("repair_tools", repair_back_route,
        {"step1_llm": "step1_llm", "step2_llm": "step2_llm", "step3_llm": "step3_llm", "__end__": "__end__"})

    return sg.compile()
```

### 2.4 路由函数详解

#### 2.4.1 `_llm_has_tool_call`

```python
def _llm_has_tool_call(state: AgentState) -> Literal["tool", "__next__"]:
    last = state["messages"][-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return "tool"
    return "__next__"
```

每个 step_llm 节点出口必须产生 tool_call，否则视为异常退出（去 `__end__`）。

#### 2.4.2 `_post_tool_route`

```python
def _post_tool_route(next_node: str) -> Callable:
    def _route(state):
        last = state["messages"][-1]
        if _is_tool_error(last):
            return "repair"
        return next_node
    return _route
```

每个 step_tool 节点出口：成功 → 下一个节点，失败 → repair。

#### 2.4.3 `progress_poll_route`

```python
def progress_poll_route(state):
    if state.get("progress_done"):
        return "__end__"
    if state.get("progress_polls", 0) >= MAX_PROGRESS_POLLS:
        return "__end__"
    return "progress_poll"
```

- `progress_done=True`：ddg_results.txt 已生成，结束
- `progress_polls >= 60`：超过 30 分钟（每次 30 秒），强制退出让用户手动 fetch

#### 2.4.4 `repair_back_route`

```python
def repair_back_route(state):
    if state.get("retry_count", 0) > MAX_RETRIES:
        return "__end__"
    return {1: "step1_llm", 2: "step2_llm", 3: "step3_llm"}.get(state.get("failed_step", 1), "__end__")
```

按 `failed_step` 路由回对应步骤。

## 3. 三段式流水线详解

### 3.1 Step 1：参数化（parameterize_ligand）

**目的**：把 `ligand.pdb/mol2` 转为 Rosetta 可识别的 `.params` 拓扑文件。

**Rosetta 命令**：

```bash
$ROSETTA_BIN/molfile_to_params.py \
    --name LIG \
    --pdb ligand.pdb \
    --out-dir output/task_*/parameterize_ligand/
```

**Step 1 系统提示**：

```python
STEP1_SYSTEM = (
    "你是【参数化阶段】执行 Agent。"
    "任务：调用 parameterize_ligand 处理配体文件（ligand.pdb 或 ligand.mol2）。"
    "权限：你只能调用系统为你绑定的工具；调用完成后请直接结束本轮。"
    "若工具返回【执行报错】，系统会自动进入修复分支，不要自行尝试别的工具。"
)
```

### 3.2 Step 2：弛豫（run_cartesian_relax）

**目的**：物理清洗 PDB + 拼接配体 + 3 路并发 Cartesian 弛豫。

**Rosetta 命令**：

```bash
relax.linuxgccrelease \
    -s protein_ligand_complex.pdb \
    -relax:cartesian \
    -score:weights ref2015_cart \
    -nstruct 3 \
    -out:prefix S1_
```

**关键设计**：nstruct=3 固定，**严禁读取或使用用户参数中的「并发数/延迟」字段**——那是给 step3 用的。

### 3.3 disorder_check 节点

**目的**：用 ESM-2 给每个残基打 disorder 分数，≥ 阈值视为无序，结果注入 step3 警告。

**实现**：

```python
def disorder_check_node(state, config=None, **kwargs):
    report = predict_disorder.func()        # 人读报告
    scores = predict_disorder_struct()      # 机器读 + 落盘 CSV

    sites = [
        f"{c}_{r}"
        for c, d in scores.items()
        for r, s in d.items()
        if s >= DISORDER_THRESHOLD
    ]
    msg = HumanMessage(content=f"🧬 {report}", name="DisorderDirector")
    return {
        "messages": [msg],
        "disordered_sites": sites,
        "disorder_scores": scores,
    }
```

**双策略回退**：

| 优先级 | 方法 | 精度 | 依赖 |
|---|---|---|---|
| 1 | ESM-2 t12 35M | 高 | `fair-esm + torch` |
| 2 | B-factor + gap 启发式 | 低 | 无 |

### 3.4 Step 3：饱和突变（run_saturation_mutagenesis）

**目的**：对指定位点跑 19 氨基酸饱和扫描。

**Rosetta 命令**：

```bash
cartesian_ddg.linuxgccrelease \
    -s wt_relaxed.pdb \
    -ddg:mut_file mutations.txt \
    -ddg:iterations 3 \
    -score:weights ref2015_cart
```

**mutations.txt 格式**：

```
1 A 71 19
1 A 72 19
```

- 第 1 列：链（Rosetta 内部编号）
- 第 2 列：原氨基酸
- 第 3 列：Rosetta 绝对行号
- 第 4 列：扫描的突变体数（19 = 全部 19 种氨基酸）

### 3.5 progress_poll 节点（后台轮询）

```python
MAX_PROGRESS_POLLS = 60      # 最多轮询 60 次
PROGRESS_INTERVAL = 30       # 每次 30 秒 = 最长 30 分钟

def progress_poll_node(state, config=None, **kwargs):
    task_id = config["configurable"].get("thread_id", ".")
    mut_dir = os.path.join("output", task_id, "saturation_mutagenesis")
    sum_dir = os.path.join("output", task_id, "summary")

    ddg_count = len(glob.glob(f"{mut_dir}/*.ddg")) if os.path.exists(mut_dir) else 0
    summary_exists = os.path.exists(os.path.join(sum_dir, "ddg_results.txt"))

    polls = state.get("progress_polls", 0) + 1

    if summary_exists:
        progress_text = f"✅ 后台计算全部完成！已生成 ddg_results.txt，共 {ddg_count} 个 .ddg 文件"
        done = True
    elif ddg_count > 0:
        progress_text = f"⏳ 后台计算中… 第 {polls} 次轮询，已完成 {ddg_count} 个 .ddg"
        done = False
    else:
        progress_text = f"⏳ 后台计算准备中… 第 {polls} 次轮询"
        done = False

    time.sleep(PROGRESS_INTERVAL)

    msg = HumanMessage(
        content=progress_text,
        name="ProgressReporter",
        id="progress_poll_status",  # 固定 id：add_messages reducer 自动覆盖
    )

    return {"messages": [msg], "progress_polls": polls, "progress_done": done, "pipeline_step": 4}
```

**关键技巧**：`id="progress_poll_status"` 固定 id，让 LangGraph 的 `add_messages` reducer 自动覆盖，**避免 60 条轮询消息累积爆 context window**。

## 4. 关键设计哲学

### 4.1 控制流职责下沉到图结构

**传统 Agent**：

```python
prompt = """
你是 Rosetta Agent。
1. 先调用 parameterize_ligand
2. 然后调用 run_cartesian_relax
3. 然后调用 run_saturation_mutagenesis
绝对禁止调其他工具！
"""
```

问题：LLM 可能不遵守，全靠 prompt 文字。

**本项目**：

```python
# 工具权限：每步只 bind_tools 自己那一步的工具
STEP1_TOOLS = [parameterize_ligand]
STEP2_TOOLS = [run_cartesian_relax]
STEP3_TOOLS = [run_saturation_mutagenesis, get_rosetta_numbering]

# 执行顺序：写在图边里
sg.add_edge(START, "step1_llm")
sg.add_conditional_edges("step1_tool", _post_tool_route("step2_llm"), ...)
```

LLM 想调别的工具也调不出来，**schema 层物理约束**。

### 4.2 错误恢复用专用分支而非 prompt 注入

**传统方式**：

```python
prompt = """
如果你上一步报错，请尝试：
1. 重新调用同一工具
2. 或者使用其他工具
...
"""
```

问题：LLM 可能死循环重复同一个错误。

**本项目**：

```python
# repair 分支专门调诊断工具
REPAIR_TOOLS = [check_logs, diagnose_and_fix, auto_repair]

def repair_node(state):
    sys_prompt = "你是【自动修复 Agent】。当前流水线某一步刚报错..."
    return repair_llm.invoke(...)

def repair_back_route(state):
    if state.get("retry_count", 0) > MAX_RETRIES:
        return "__end__"  # 重试超限强制退出
    return {1: "step1_llm", ...}.get(state.get("failed_step"), "__end__")
```

**专工具 + 重试上限 + 按失败步骤回退**，三道防线。

### 4.3 状态字段编码进度与恢复信息

```python
class AgentState(TypedDict):
    pipeline_step: int      # 当前步骤（UI 观察）
    failed_step: int        # 最近失败步骤（路由用）
    retry_count: int        # 重试次数（防死循环）
    progress_polls: int     # 轮询次数（防无限等）
    progress_done: bool     # 后台完成标记
```

这些**结构化字段**让图路由完全确定，不依赖 LLM 决策。

### 4.4 子图封装为可复用节点

```python
workflow.add_node("pipeline", build_pipeline_subgraph())
```

主图把子图当一个节点用，子图内部的所有细节对主图透明。**主图只关心"pipeline 是否完成"，子图自己管内部步骤**。

## 5. 一次完整的执行追踪

假设用户上传 `1abc.pdb` + `ligand.mol2`，突变位点 `76,73`，参数 `并发数 4, 延迟 3`：

```
1. Gradio submit_task 触发
2. 创建 task_id = "task_20260913_1500_1abc"
3. PyMOL 提取配体（如果 ligand_file 为空）
4. 拼装 final_prompt
5. agent_executor.stream() 启动

6. 主图 START → router
7. router 检测到 "【系统强制规则】" → intent="pipeline"（启发式快判）
8. router → pipeline（子图入口）

9. 子图 START → step1_llm
10. step1_llm 调 parameterize_ligand（schema 限制只能调这个）
11. → step1_tool
12. → 成功 → step2_llm
13. step2_llm 调 run_cartesian_relax
14. → step2_tool
15. → 成功 → disorder_check
16. disorder_check 调 ESM-2（确定性，不调 LLM）
17. → step3_llm
18. step3_llm 调 run_saturation_mutagenesis（jobs=4, delay=3）
19. → step3_tool
20. → 成功 → progress_poll
21. progress_poll 每 30 秒查一次后台任务
22. 等到 ddg_results.txt 生成 → progress_done=True → __end__

23. 子图出口 → 主图 skempi_eval
24. skempi_eval 调 evaluate_against_skempi + evaluate_against_pdbbind
25. → summarize
26. summarize 输出最终答复（bind_tools([]) 禁止再调工具）
27. → END

28. 用户在 Gradio 点 "刷新并打包下载结果"
29. package_results 打包 → 下载 zip
```

整个流程**零人工介入**。