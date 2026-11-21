"""TMol 引擎集成验收(2026-11-21):worker 真实 GPU 扫描(1MC9)+ 引擎字段/挂载/口径检查。"""
import json
import os
import subprocess
import sys
import tempfile

PROJ = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJ)
os.chdir(PROJ)
import task_runtime as tr  # noqa: E402

TMP = tempfile.mkdtemp(prefix="tmol_eval_", dir=os.path.join(PROJ, "output"))
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(("✅ " if ok else "❌ ") + name + (f"  [{detail}]" if detail and not ok else ""))


TMOL_PY = "/opt/software/tmol/bin/python"
PYPATH = os.path.expanduser("~/.tmol_torch")
PDB = os.path.join(PROJ, "inputs", "pdbs", "1MC9_protein.pdb")

# ============ A. worker 真实 GPU 扫描(1MC9, 位点 25) ============
print("\n----- A. worker 真实 GPU 扫描 -----")
env = dict(os.environ)
env["PYTHONPATH"] = PYPATH
env["CUDA_VISIBLE_DEVICES"] = "0"
out_json = os.path.join(TMP, "tmol_scan.json")
proc = subprocess.run([TMOL_PY, os.path.join(PROJ, "tmol_scan_worker_20261121.py"),
                       PDB, "25", out_json],
                      capture_output=True, text=True, timeout=600, env=env)
check("A1 worker 跑完(TMOL_SCAN_DONE)", proc.returncode == 0 and "TMOL_SCAN_DONE" in proc.stdout,
      (proc.stderr or "")[-200:] or proc.stdout[-200:])
if proc.returncode == 0 and os.path.exists(out_json):
    data = json.load(open(out_json, encoding="utf-8"))
    r = data["results"].get("A_25", {})
    check("A2 19 个突变全量(排除 WT)", len(r.get("mutations", {})) == 19,
          f"{len(r.get('mutations', {}))}")
    check("A3 GPU 上(beta_nov2016_cart)", data["device"] == "cuda"
          and data["weights"] == "beta_nov2016_cart", data["device"])
    check("A4 top_stable 排序正确(升序前 3)",
          len(r["top_stable"]) == 3
          and r["top_stable"] == sorted(r["top_stable"], key=lambda x: x["ddG"])
          and [x["ddG"] for x in r["top_stable"]] == sorted(x["ddG"] for x in r["mutations"].values())[:3],
          str(r.get("top_stable"))[:150])
    check("A5 单突变耗时 <3s(GPU)", max(m["sec"] for m in r["mutations"].values()) < 3.0,
          f"max={max(m['sec'] for m in r['mutations'].values())}")
else:
    check("A1 worker 跑完(TMOL_SCAN_DONE)", False, (proc.stderr or "")[-300:])

# ============ B. run_tmol_scan 工具(工作区绑定+落盘) ============
print("\n----- B. run_tmol_scan 工具 -----")
import rosetta_tools as rt  # noqa: E402
ws = os.path.join(TMP, "task_tmol_eval")
os.makedirs(ws, exist_ok=True)
import shutil  # noqa: E402

shutil.copy(PDB, os.path.join(ws, "protein.pdb"))
tr.state_write(ws, status="created", stage="created", manifest={"engine": "tmol"},
               input_summary={"protein": "1MC9_protein.pdb", "engine": "tmol", "entry": "eval"})
rt.current_workspace.set(ws)
out = rt.run_tmol_scan.func(target_resnum="25")
check("B1 工具返回完成标记与 top3", "【TMol GPU 扫描完成】" in out and "top3" in out, out[:240])
check("B2 tmol_scan.json 落盘且契约工件登记",
      os.path.exists(os.path.join(ws, "summary", "tmol_scan.json"))
      and (tr.state_read(ws).get("manifest") or {}).get("tmol_scan") == "summary/tmol_scan.json")
out2 = rt.run_tmol_scan.func(target_resnum="A_9999")
check("B3 不存在位点 → 明确报错", "【执行报错】" in out2)

# ============ C. 引擎字段/挂载/口径(源码级) ============
print("\n----- C. 源码级检查 -----")
srv = open(os.path.join(PROJ, "rosetta_server.py"), encoding="utf-8").read()
mcp = open(os.path.join(PROJ, "rosetta_mcp_server.py"), encoding="utf-8").read()
ag = open(os.path.join(PROJ, "rosetta_agent.py"), encoding="utf-8").read()
html = open(os.path.join(PROJ, "static", "index.html"), encoding="utf-8").read()
js = open(os.path.join(PROJ, "static", "app.js"), encoding="utf-8").read()
rt_src = open(os.path.join(PROJ, "rosetta_tools.py"), encoding="utf-8").read()
check("C1 POST engine Form+校验", 'engine: str = Form("rosetta")' in srv
      and 'engine 仅支持 rosetta 或 tmol' in srv)
check("C2 MCP create_task engine 参数+校验", "engine: str = \"rosetta\"" in mcp
      and "engine 仅支持 rosetta 或 tmol" in mcp)
check("C3 前端下拉+提交体 engine", 'id="engine"' in html and 'fd.append("engine"' in js)
check("C4 run_tmol_scan 为 @tool 且挂载(7779 GENERAL_TOOLS + MCP 清单)",
      "@tool\ndef run_tmol_scan" in rt_src and mcp.count("run_tmol_scan") >= 2
      and "GENERAL_TOOLS = [run_tmol_scan," in ag)
check("C5 提示词引擎路由(reply 1 处 + 三处 sys_rule)", ag.count("【引擎路由】") == 4)
check("C6 suggest_plan 含引擎口径说明", "【打分引擎】" in rt_src)
check("C7 summarize_results 含引擎行", "TMol beta_nov2016_cart 口径" in rt_src)
check("C8 质量守卫:无残留占位代码", "if False else None" not in rt_src)

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
