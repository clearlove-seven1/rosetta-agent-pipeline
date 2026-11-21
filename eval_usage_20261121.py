"""成本/耗时可见化验收(2026-11-21):token 回调/jsonl/收尾上屏/预计耗时/实际耗时。
纯单元,不调 LLM。"""
import json
import os
import sys
import tempfile

PROJ = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJ)
import task_runtime as tr  # noqa: E402
import rosetta_tools as rt  # noqa: E402

TMP = tempfile.mkdtemp(prefix="usage_eval_", dir=os.path.join(PROJ, "output"))
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(("✅ " if ok else "❌ ") + name + (f"  [{detail}]" if detail and not ok else ""))


# ============ A. 源码检查 ============
print("\n----- A. 源码检查 -----")
ag = open(os.path.join(PROJ, "rosetta_agent.py"), encoding="utf-8").read()
rt_src = open(os.path.join(PROJ, "rosetta_tools.py"), encoding="utf-8").read()
check("A1 回调挂到 llm(callbacks=[_token_cb])", "callbacks=[_token_cb]" in ag)
check("A2 usage_stats.jsonl 逐条记录纪律(异常吞掉不影响对话)",
      "usage_stats.jsonl" in ag and "except Exception:" in ag.split("_TokenUsageCallback")[1][:1600])
check("A3 summarize_node 收尾上屏 token 用量", "〔token 用量〕" in ag and "_turn_thread_usage" in ag)
check("A4 suggest_plan 含预计耗时", "【预计耗时】" in rt_src and "以任务实际为准" in rt_src)
check("A5 summarize_results 含实际耗时", "实际耗时" in rt_src and "创建 → 最近更新" in rt_src)

# ============ B. suggest_plan 预计耗时(真实判定) ============
print("\n----- B. suggest_plan 预计耗时 -----")
def write_pdb(name, chains, hetatm=()):
    lines = []
    serial = 0
    for chain, nums in chains:
        for n in nums:
            for nm in (" N  ", " CA ", " C  "):
                serial += 1
                lines.append("ATOM  %5d %s ALA %s%4d    0.000    0.000    0.000  1.00  0.00           C"
                             % (serial, nm, chain, n))
    for rn in hetatm:
        for _ in range(3):
            serial += 1
            lines.append("HETATM %5d XX %s X   1    0.000    0.000    0.000  1.00  0.00          X"
                         % (serial, rn))
    p = os.path.join(TMP, name)
    open(p, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    return p


p1 = write_pdb("single.pdb", [("A", list(range(1, 41)))])
out = rt.suggest_plan.func(protein_pdb_path=p1)
check("B1 单链计划含预计耗时(10-15 分钟量级)", "【预计耗时】" in out and "10-15 分钟" in out, out[-260:])
big = write_pdb("big.pdb", [("A", list(range(1, 401)))])
out = rt.suggest_plan.func(protein_pdb_path=big)
check("B2 大体系(400 残基)提示耗时 ×2-3", "×2-3" in out, out[-260:])
p2 = write_pdb("lig.pdb", [("A", list(range(1, 41)))], hetatm=("LIG",))
out = rt.suggest_plan.func(protein_pdb_path=p2)
check("B3 配体体系 15-22 分钟量级", "15-22 分钟" in out, out[-260:])

# ============ C. summarize_results 实际耗时 ============
print("\n----- C. summarize_results 实际耗时 -----")
ws = os.path.join(TMP, "task_rep")
os.makedirs(os.path.join(ws, "summary"), exist_ok=True)
with open(os.path.join(ws, "summary", "ddg_results.txt"), "w", encoding="utf-8") as f:
    f.write("突变\tΔΔG\nA_A1_G\t-1.0\n")
tr.state_write(ws, status="completed", stage="mutagenesis")
_stf = os.path.join(ws, "task_status.json")
_d = json.load(open(_stf, encoding="utf-8"))
_d["created_at"], _d["updated_at"] = "2026-11-21T10:00:00", "2026-11-21T10:21:30"
json.dump(_d, open(_stf, "w", encoding="utf-8"), ensure_ascii=False)
out = rt.summarize_results.func(task_dir=ws)
check("C1 报告含实际耗时 21.5 分钟", "21.5 分钟" in out, out[:300])

# ============ D. token 统计单元 ============
print("\n----- D. token 统计单元 -----")
import rosetta_agent as ra  # noqa: E402(启动链路导入守卫顺带复验)
from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402
msgs = [
    HumanMessage(content="hi"),
    AIMessage(content="", usage_metadata={"input_tokens": 10, "output_tokens": 4, "total_tokens": 14}),
    AIMessage(content="", usage_metadata={"input_tokens": 30, "output_tokens": 6, "total_tokens": 36}),
    HumanMessage(content="again"),
    AIMessage(content="", usage_metadata={"input_tokens": 20, "output_tokens": 5, "total_tokens": 25}),
]
turn, thread = ra._turn_thread_usage(msgs)
check("D1 本轮/线程 token 拆分正确", turn == (20, 5, 25) and thread == (60, 15, 75),
      f"turn={turn} thread={thread}")

cb = ra._token_cb
ra.USAGE_LOG_PATH = os.path.join(TMP, "usage_stats.jsonl")  # 路径为导入时常量,测试改模块属性
from langchain_core.outputs import ChatGeneration, LLMResult  # noqa: E402
msg = AIMessage(content="", usage_metadata={"input_tokens": 7, "output_tokens": 3, "total_tokens": 10})
cb.on_llm_end(LLMResult(generations=[[ChatGeneration(message=msg)]]))
cb.on_llm_end(LLMResult(generations=[[ChatGeneration(message=AIMessage(
    content="", usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}))]]))
recs = [json.loads(l) for l in open(ra.USAGE_LOG_PATH, encoding="utf-8")]
check("D2 回调逐条写 jsonl(2 条,字段齐)", len(recs) == 2
      and all({"ts", "model", "prompt", "completion", "total"} <= set(r) for r in recs),
      str(recs))
cb.on_llm_end(LLMResult(generations=[[ChatGeneration(message=AIMessage(content=""))]]))
recs = [json.loads(l) for l in open(ra.USAGE_LOG_PATH, encoding="utf-8")]
check("D3 无用量消息不写行(仍 2 条)", len(recs) == 2)

# 汇总
failures = [name for name, ok, _ in RESULTS if not ok]
print("\n========== 结果汇总 ==========")
print(f"通过 {len(RESULTS) - len(failures)} / {len(RESULTS)}")
if failures:
    print("未通过:")
    for f_ in failures:
        print("  -", f_)
    sys.exit(1)
print("全部通过 ✅")
