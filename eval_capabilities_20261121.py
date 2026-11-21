"""能力提升验收(2026-11-21):①提示词 ②RAG 评测集 ③start_all/运维手册 ④结果报告
⑤检查点持久化 ⑥playbook D。纯检索+纯 Python,不跑 Rosetta 计算(⑤跨重启实证在验收阶段做)。"""
import json
import os
import subprocess
import sys
import tempfile

PROJ = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJ)
import task_runtime as tr  # noqa: E402
import rosetta_tools as rt  # noqa: E402

TMP = tempfile.mkdtemp(prefix="cap_eval_", dir=os.path.join(PROJ, "output"))
print(f"夹具根: {TMP}")
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(("✅ " if ok else "❌ ") + name + (f"  [{detail}]" if detail and not ok else ""))


# ============ A. ② RAG 检索评测集(24 题) ============
print("\n----- A. RAG 检索评测 -----")
RAG_SET = [
    # (query, 任一命中关键词, 类别 plan/param/error/tool)
    ("单链蛋白怎么做突变扫描计划", ["Playbook A", "突变扫描"], "plan"),
    ("蛋白加配体的计划", ["Playbook B", "配体"], "plan"),
    ("多链复合物计划", ["Playbook C", "复合物"], "plan"),
    ("定点设计几个位点", ["playbook D", "fixbb"], "plan"),
    ("这个体系有什么停止条件", ["停止条件"], "plan"),
    ("突变的默认参数是什么", ["nstruct", "iterations"], "plan"),
    ("cartesian_ddg 的 iterations 参数", ["cartesian"], "param"),
    ("relax 弛豫怎么跑", ["relax"], "param"),
    ("ref2015_cart 打分函数", ["ref2015", "score"], "param"),
    ("fixbb 的 resfile 格式", ["resfile", "fixbb"], "param"),
    ("Residue params 参数文件格式", ["params"], "param"),
    ("cannot find residue 报错", ["residue", "Residue"], "error"),
    ("缺失 CA 原子怎么处理", ["CA", "missing"], "error"),
    ("怎么读 score 文件打分", ["score"], "error"),
    ("怎么导出突变体结构", ["export_structures", "mutant_structures"], "tool"),
    ("ΔΔG 结果怎么解读", ["ΔΔG", "REU"], "tool"),
    ("位点编号 76 A_76 格式", ["A_76", "编号"], "tool"),
    ("插入码 100A 怎么处理", ["插入码", "100A"], "tool"),
    ("多链同号怎么办", ["链"], "tool"),
    ("非标准残基 MSE 处理", ["MSE", "残基"], "tool"),
    ("contract_check 契约是什么", ["contract", "契约"], "tool"),
    ("接力 GROMACS 需要什么", ["GROMACS", "gromacs_ready", "GROMACS 会话"], "tool"),
    ("弛豫 nstruct 含义", ["nstruct"], "param"),
    ("结果怎么排序找最稳定", ["ΔΔG", "ddg_results", "排序"], "tool"),
]
hit = plan_hit = plan_total = 0
fails = []
for q, kws, cat in RAG_SET:
    out = rt.search_rosetta_docs.func(q)
    ok = any(k.lower() in out.lower() for k in kws)
    hit += ok
    if cat == "plan":
        plan_total += 1
        plan_hit += ok
    if not ok:
        fails.append((q, cat))
rate = hit / len(RAG_SET)
check(f"A1 RAG 命中率 {hit}/{len(RAG_SET)}（阈值 ≥21）", rate >= 21 / len(RAG_SET),
      f"未命中: {fails}")
check("A2 计划类 6/6 命中(优先层生效)", plan_hit == plan_total, f"{plan_hit}/{plan_total}")
print(f"[RAG 明细] 总命中 {hit}/{len(RAG_SET)}, 计划类 {plan_hit}/{plan_total}")

# ============ B. ④ summarize_results ============
print("\n----- B. summarize_results -----")
ws = os.path.join(TMP, "task_report")
os.makedirs(os.path.join(ws, "summary"), exist_ok=True)
os.makedirs(os.path.join(ws, "saturation_mutagenesis", "mutant_structures", "A_A_67_G"),
            exist_ok=True)
with open(os.path.join(ws, "summary", "ddg_results.txt"), "w", encoding="utf-8") as f:
    f.write("突变\tΔΔG(REU, min(MUT)-min(WT))\n")
    for n, v in [("A_A67_G", -3.1), ("A_A67_V", 2.2), ("A_A67_L", -1.0),
                 ("A_A67_F", 5.5), ("A_A67_I", 0.4)]:
        f.write(f"{n}\t{v}\n")
with open(os.path.join(ws, "saturation_mutagenesis", "mutant_structures", "A_A_67_G",
                       "MUT_64ALA_bj1.pdb"), "w") as f:
    f.write("ATOM\n")
tr.state_write(ws, status="completed", stage="mutagenesis",
               manifest={"contract_version": "pdw-task-v0.1",
                         "ddg_program": "cartesian_ddg.linuxgccrelease",
                         "mutations_planned": 5},
               artifacts={"ddg_results": "summary/ddg_results.txt"})
out = rt.summarize_results.func(task_dir=ws)
check("B1 报告含终态/计划字段/突变数", "completed" in out and "cartesian_ddg" in out
      and "5 个突变" in out, out[:240])
check("B2 排序正确(最稳定=G=-3.1 / 最去稳定=F=5.5)",
      "A_A67_G=-3.100" in out and "A_A67_F=+5.500" in out, out[:300])
check("B3 口径与局限声明在", "REU" in out and "不是结合自由能" in out and "局限" in out)
check("B4 接力建议映射到已导出结构目录", "A_A_67_G" in out and "接力 MD" in out, out[:400])
empty = os.path.join(TMP, "task_empty")
os.makedirs(empty, exist_ok=True)
out2 = rt.summarize_results.func(task_dir=empty)
check("B5 无汇总 → 如实说明不编造", "尚无汇总" in out2)
bad = rt.summarize_results.func(task_dir=os.path.join(TMP, "nope"))
check("B6 目录不存在 → 执行报错", "【执行报错】" in bad)

# ============ C. ①⑤⑥③ 源码/文件检查 ============
print("\n----- C. 源码/文件检查 -----")
ag = open(os.path.join(PROJ, "rosetta_agent.py"), encoding="utf-8").read()
check("C1 REPLY_SYSTEM 含 suggest_plan/计划纪律/停止条件",
      "suggest_plan" in ag and "【计划纪律】" in ag and "【停止条件】" in ag)
check("C2 检查点已换 SqliteSaver(MemorySaver() 实例化移除)",
      "SqliteSaver(sqlite3.connect" in ag and "memory = MemorySaver()" not in ag)
check("C3 GENERAL_TOOLS 含 suggest_plan+summarize_results",
      "GENERAL_TOOLS = [suggest_plan, search_rosetta_docs, summarize_results," in ag)
mcp = open(os.path.join(PROJ, "rosetta_mcp_server.py"), encoding="utf-8").read()
check("C4 MCP 两处清单含 summarize_results", mcp.count("summarize_results") == 2)
rt_src = open(os.path.join(PROJ, "rosetta_tools.py"), encoding="utf-8").read()
check("C5 summarize_results @tool 且声明只读",
      "@tool\ndef summarize_results" in rt_src and "只读" in rt_src)
sa = os.path.join(PROJ, "start_all.sh")
check("C6 start_all.sh 存在且语法通过(bash -n)",
      os.path.exists(sa) and subprocess.run(["bash", "-n", sa]).returncode == 0)
sa_txt = open(sa, encoding="utf-8").read()
check("C7 start_all.sh 覆盖 7779+7782 且幂等(skip 分支)",
      "7779" in sa_txt and "7782" in sa_txt and "ensure_port" in sa_txt)
check("C8 OPS-RUNBOOK.md 存在且含故障定位/备份", "OPS" in open(os.path.join(PROJ, "OPS-RUNBOOK.md"),
      encoding="utf-8").read() and "备份" in open(os.path.join(PROJ, "OPS-RUNBOOK.md"),
      encoding="utf-8").read())
pbd = os.path.join(PROJ, "docs", "playbooks", "playbook_D_fixbb_design.md")
check("C9 playbook D 存在(fixbb 口径三条 limitation)",
      os.path.exists(pbd) and "三条 limitation" in open(pbd, encoding="utf-8").read())
pba = open(os.path.join(PROJ, "docs", "playbooks", "playbook_A_single_chain.md"),
           encoding="utf-8").read()
check("C10 playbook A 含 D 交叉引用", "playbook_D" in pba)

# ============ 汇总 ============
failures = [name for name, ok, _ in RESULTS if not ok]
print("\n========== 结果汇总 ==========")
print(f"通过 {len(RESULTS) - len(failures)} / {len(RESULTS)}")
if failures:
    print("未通过:")
    for f_ in failures:
        print("  -", f_)
    sys.exit(1)
print("全部通过 ✅")

# ============ F. 7779 启动链路导入守卫(运行时 NameError 类回归) ============
try:
    import rosetta_agent as ra  # noqa: E402
    _ra_ok = (ra.agent_executor is not None
              and {getattr(t, "name", "") for t in ra.GENERAL_TOOLS} >= {"suggest_plan", "summarize_results"})
    _ra_err = ""
except Exception as _e:
    _ra_ok, _ra_err = False, repr(_e)[:200]
check("F1 7779 启动链路可导入且两新工具在 GENERAL_TOOLS", _ra_ok, _ra_err)
failures = [name for name, ok, _ in RESULTS if not ok]
print("\n========== 启动链路守卫 ==========")
print(f"追加后: 通过 {len(RESULTS) - len(failures)} / {len(RESULTS)}")
if failures:
    sys.exit(1)
print("全部通过 ✅")
