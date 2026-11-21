"""
eval_review_followup_20261121.py — 复核轮补充验证（状态并发 / PID 复用 / 晚到 runner）
================================================================================

不调 LLM、不跑 Rosetta。验证 2026-11-21 复核修复：
  R1 跨进程并发 state_write 不丢更新（flock 互斥 + 原子替换；无锁实现必丢），
     且写入全程文件始终是合法 JSON（读不炸）；
  R2 PID 复用防护：存活但非本 runner 的 PID 按"已退出"处理；带 marker 的存活
     runner 判活；进程退出后判死；
  R3 晚到 runner 守卫：_finalize_status 在 runner_pids.ddg 与自身 pid 不符时
     不得改写状态（只留 stale 说明）；相符时正常收口；
  R4 空壳汇总（0 个成功突变）→ failed，不虚报成功（settle 与 state_effective 一致）。

运行（rosetta-agent env，仓库根目录）：
  python eval_review_followup_20261121.py
"""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

import task_runtime as tr  # noqa: E402
import rosetta_tools as rt  # noqa: E402  (仅为取 _RUNNER_STATUS_SRC 源码)

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(f"{'✅ PASS' if cond else '❌ FAIL'}  {name}" + (f"  | {detail}" if detail and not cond else ""))


TMP = Path(tempfile.mkdtemp(prefix="wb_review_"))

# ===============================================================
# R1 跨进程并发写
# ===============================================================
conc = TMP / "task_conc"
os.makedirs(conc, exist_ok=True)
tr.state_write(str(conc), status="created", stage="created")

WORKER = textwrap.dedent("""
    import sys, time
    sys.path.insert(0, sys.argv[1])
    import task_runtime as tr
    task_dir, field = sys.argv[2], sys.argv[3]
    for i in range(40):
        tr.state_write(task_dir, **{field: i, "status": ("running" if i % 2 else "completed")})
    print("OK", field)
""")
worker_file = TMP / "state_worker.py"
worker_file.write_text(WORKER, encoding="utf-8")

N_WORKERS = 6
env = dict(os.environ)
procs = [subprocess.Popen(
    [sys.executable, str(worker_file), str(PROJECT_ROOT), str(conc), f"field_{i}"],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    for i in range(N_WORKERS)]

# 并发期间反复读取：原子替换保证每次都是完整合法 JSON
parse_ok = True
last_err = ""
for _ in range(15):
    time.sleep(0.15)
    try:
        json.loads((conc / "task_status.json").read_text(encoding="utf-8"))
    except Exception as e:
        parse_ok = False
        last_err = str(e)

outs = [p.communicate(timeout=120) for p in procs]
rcs = [p.returncode for p in procs]
final = tr.state_read(str(conc))

check("R1a 所有并发 worker 正常退出", all(r == 0 for r in rcs), str(rcs) + str([o[1][-200:] for o in outs if o[1]]))
check("R1b 并发期间文件始终合法 JSON", parse_ok, last_err)
check("R1c 无丢更新：每个进程的字段最终值都在",
      all(final.get(f"field_{i}") == 39 for i in range(N_WORKERS)),
      json.dumps({k: final.get(k) for k in list(final)[:12]}, ensure_ascii=False))
check("R1d 最终状态是某次真实写入(非撕裂值)", final.get("status") in ("running", "completed", "created"))

# ===============================================================
# R2 PID 复用防护
# ===============================================================
# R2a 存活但与本 runner 无关的 PID → 判"已退出"
p_unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
time.sleep(0.5)
check("R2a 存活无关进程+marker → 按 PID 复用处理(判死)",
      tr._pid_alive(p_unrelated.pid, marker="run_ddg.py") is False)
check("R2b 同一 PID 无 marker → 判活(基础探活)",
      tr._pid_alive(p_unrelated.pid) is True)

# R2c 真正的 run_ddg.py runner 存活 → 判活
stub_dir = TMP / "stub"
os.makedirs(stub_dir, exist_ok=True)
(stub_dir / "run_ddg.py").write_text("import time; time.sleep(30)\n", encoding="utf-8")
p_runner = subprocess.Popen([sys.executable, "run_ddg.py"], cwd=str(stub_dir))
time.sleep(0.6)
check("R2c 带正确 marker 的存活 runner → 判活",
      tr._pid_alive(p_runner.pid, marker="run_ddg.py") is True)
st_alive = TMP / "task_r2_alive"
os.makedirs(st_alive, exist_ok=True)
tr.state_write(str(st_alive), status="running", runner_pids={"ddg": p_runner.pid})
check("R2d runner 存活 → 生效状态 running", tr.state_effective(str(st_alive))["status"] == "running")

# R2e 退出后 → 判死
p_runner.kill()
p_runner.wait()
check("R2e runner 退出 → 判死", tr._pid_alive(p_runner.pid, marker="run_ddg.py") is False)

p_unrelated.kill()
p_unrelated.wait()

# ===============================================================
# R3 晚到 runner 守卫（执行 runner 内嵌状态代码的副本）
# ===============================================================
ns: dict = {}
exec(rt._RUNNER_STATUS_SRC, ns)  # noqa: S102 - 受控源码片段
grd = TMP / "task_guard"
os.makedirs(grd, exist_ok=True)
tr.state_write(str(grd), status="running", stage="mutagenesis",
               runner_pids={"relax": None, "ddg": 999999})

ns["_finalize_status"](str(grd), 424242, status="completed", stage="mutagenesis",
                       status_note="旧 runner 的完成标记")
st = tr.state_read(str(grd))
check("R3a 晚到旧 runner(pid 不符)不改写状态", st.get("status") == "running"
      and "旧 runner" in (st.get("stale_runner_note") or ""))

ns["_finalize_status"](str(grd), 999999, status="completed", stage="mutagenesis",
                       status_note="当前 runner 正常收口")
st = tr.state_read(str(grd))
check("R3b 当前 runner(pid 相符)正常收口", st.get("status") == "completed")

# ===============================================================
# R4 空壳汇总不虚报成功
# ===============================================================
shell = TMP / "task_shell"
os.makedirs(shell / "summary", exist_ok=True)
(shell / "summary" / "ddg_results.txt").write_text("突变\tΔΔG(REU)\n", encoding="utf-8")
out = tr.settle_task_after_stream(str(shell), had_error=False)
check("R4a settle: 空壳汇总 → failed(NoSuccessfulMutants)",
      out == "failed" and tr.state_read(str(shell)).get("error_type") == "NoSuccessfulMutants")
tr.state_write(str(shell), status="running", runner_pids={"ddg": 999999})
eff = tr.state_effective(str(shell))
check("R4b state_effective: 空壳汇总+进程已退 → failed",
      eff["status"] == "failed" and "0 个成功突变" in eff["note"])
# 有 ≥1 个 ddg → completed
(shell / "saturation_mutagenesis").mkdir(exist_ok=True)
(shell / "saturation_mutagenesis" / "x.ddg").write_text("WT\nMUT\n", encoding="utf-8")
eff = tr.state_effective(str(shell))
check("R4c 补上真实 ddg 产物后 → completed", eff["status"] == "completed")

# ---------------------------------------------------------------
failures = [name for name, ok, _ in RESULTS if not ok]
print("\n========== 结果汇总 ==========")
print(f"通过 {len(RESULTS) - len(failures)} / {len(RESULTS)}")
if failures:
    print("失败项：", failures)
    sys.exit(1)
print("全部通过 ✅")
