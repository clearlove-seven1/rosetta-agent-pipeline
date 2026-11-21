"""阶段3契约验收(2026-11-21):pdw-task-v0.1 + partial 状态语义。
覆盖:partial 三处一致(state_effective/settle/runner 收尾源)、契约必填校验、
legacy 宽容、哈希核对、manifest_patch 注入、真实旧任务只读校验。
纯 Python,不跑 Rosetta/LLM,不修改任何生产文件。
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile

import task_runtime as tr
import rosetta_tools as rt

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(("✅ " if ok else "❌ ") + name + (f"  [{detail}]" if detail and not ok else ""))


TMP = tempfile.mkdtemp(prefix="contract_eval_", dir=os.path.join(os.path.dirname(
    os.path.abspath(__file__)), "output"))
print(f"夹具根: {TMP}")


def dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(0)"])
    p.wait()
    return p.pid


def mk_task(name):
    d = os.path.join(TMP, name)
    os.makedirs(os.path.join(d, "summary"), exist_ok=True)
    os.makedirs(os.path.join(d, "saturation_mutagenesis"), exist_ok=True)
    with open(os.path.join(d, "summary", "ddg_results.txt"), "w", encoding="utf-8") as f:
        f.write("突变\tΔΔG\nA_A67_G\t-1.5\n")
    return d


def add_ddg(d, n):
    for i in range(n):
        with open(os.path.join(d, "saturation_mutagenesis", f"s{i}.ddg"), "w", encoding="utf-8") as f:
            f.write("WT\nMUT\n")


FULL_MANIFEST = {
    "contract_version": tr.CONTRACT_VERSION,
    "input_structure_path": "protein.pdb",
    "chains": ["A"],
    "numbering_basis": "cartesian_relax/complex_rosetta_ready.pdb",
    "target_sites": ["A_67(WT=G)"],
    "mutations_planned": 19,
    "ddg_program": "cartesian_ddg.linuxgccrelease",
    "ddg_params": {"iterations": 3, "cartesian": True, "bbnbrs": 1,
                   "weights": "ref2015_cart", "min_type": "lbfgs_armijo_nonmonotone",
                   "extra_res_fa": False},
    "rosetta_version": "rosetta.source.release-280 r280 2021.16+release",
    "gromacs_ready": False,
}

# ============ A. partial 状态语义(三处一致) ============
print("\n----- A. partial 状态 -----")
check("A0 STATUS_PARTIAL 常量存在", tr.STATUS_PARTIAL == "partial")

d_a = mk_task("a_partial")
add_ddg(d_a, 5)
dead = dead_pid()
tr.state_write(d_a, status="running", runner_pids={"ddg": dead},
               manifest={"mutations_planned": 19})
eff = tr.state_effective(d_a)
check("A1 state_effective 5/19 → partial", eff["status"] == "partial"
      and "5/19" in eff["note"], json.dumps(eff, ensure_ascii=False)[:160])

d_b = mk_task("b_full")
add_ddg(d_b, 19)
tr.state_write(d_b, status="running", runner_pids={"ddg": dead},
               manifest={"mutations_planned": 19})
check("A2 state_effective 19/19 → completed", tr.state_effective(d_b)["status"] == "completed")

d_c = mk_task("c_legacy")
add_ddg(d_c, 3)
tr.state_write(d_c, status="running", runner_pids={"ddg": dead})  # 无 manifest
check("A3 旧任务无计划数 → 保持 completed(legacy 宽容)",
      tr.state_effective(d_c)["status"] == "completed")

d_e = mk_task("e_zero")
tr.state_write(d_e, status="running", runner_pids={"ddg": dead},
               manifest={"mutations_planned": 19})
eff = tr.state_effective(d_e)
check("A4 0/19 → failed(空壳不虚报,回归护栏)",
      eff["status"] == "failed" and "0 个成功突变" in eff["note"])

out = tr.settle_task_after_stream(d_a, had_error=False)
check("A5 settle 5/19 → partial", out == "partial"
      and tr.state_read(d_a)["status"] == "partial")

# ============ B. runner 收尾源码(_RUNNER_STATUS_SRC)行为 ============
print("\n----- B. runner 收尾行为 -----")
ns = {}
exec(rt._RUNNER_STATUS_SRC, ns)
_fz = ns["_finalize_status"]

d_f = mk_task("f_manifest_patch")
tr.state_write(d_f, status="running", runner_pids={"ddg": 424242},
               manifest={"mutations_planned": 19})
pid = os.getpid()
_fz(d_f, 424242, status="completed", stage="mutagenesis",
    manifest_patch={"mutations_succeeded": 5, "mutations_failed": 14,
                    "rosetta_version": "rosetta.source.release-280"})
st = tr.state_read(d_f)
check("B1 manifest_patch 并入 manifest 子节点",
      st["manifest"].get("mutations_succeeded") == 5
      and st["manifest"].get("rosetta_version", "").startswith("rosetta.source")
      and "manifest_patch" not in st, json.dumps(st.get("manifest"), ensure_ascii=False)[:120])
check("B2 pid 守卫仍生效(相符才收口)", st.get("status") == "completed")

d_g = mk_task("g_stale")
tr.state_write(d_g, status="running", runner_pids={"ddg": 111111},
               manifest={"mutations_planned": 19})
_fz(d_g, pid, status="completed", manifest_patch={"mutations_succeeded": 1})
st = tr.state_read(d_g)
check("B3 晚到 runner:状态不改/manifest 不被污染",
      st.get("status") == "running" and "stale_runner_note" in st
      and st.get("manifest", {}).get("mutations_succeeded") is None)

# ============ C. contract_check 校验器 ============
print("\n----- C. contract_check -----")
d_h = mk_task("h_conformant")
with open(os.path.join(d_h, "protein.pdb"), "wb") as f:
    f.write(b"HEADER    TEST\nATOM      1  CA  ALA A   7\n")
mf = dict(FULL_MANIFEST)
mf["input_structure_sha256"] = hashlib.sha256(
    open(os.path.join(d_h, "protein.pdb"), "rb").read()).hexdigest()
mf["mutations_succeeded"], mf["mutations_failed"] = 19, 0
tr.state_write(d_h, status="completed", manifest=mf)
cc = tr.contract_check(d_h)
check("C1 合规 v0.1 任务 → ok=True/legacy=False/无 missing",
      cc["ok"] and not cc["legacy"] and cc["missing"] == []
      and cc["effective_status"] == "completed",
      json.dumps(cc, ensure_ascii=False)[:200])
check("C2 gromacs_ready=False 如实出现在 warnings",
      any("gromacs_ready" in w for w in cc["warnings"]))

with open(os.path.join(d_h, "protein.pdb"), "ab") as f:
    f.write(b"ATOM      2  CA  GLY A   8\n")
cc2 = tr.contract_check(d_h)
check("C3 输入文件被改 → 哈希不一致 warning(ok 不因此翻转)",
      cc2["ok"] and any("不一致" in w for w in cc2["warnings"]))

d_i = mk_task("i_running")
tr.state_write(d_i, status="running", runner_pids={"ddg": dead},
               manifest=dict(FULL_MANIFEST))
cc3 = tr.contract_check(d_i)
check("C4 running 未到终态 → ok=False 且 missing 注明生效状态",
      not cc3["ok"] and any("effective_status" in m for m in cc3["missing"]))

cc4 = tr.contract_check(os.path.join(TMP, "no_such_dir"))
check("C5 任务目录不存在 → ok=False 如实说明",
      not cc4["ok"] and "task_status.json" in cc4["missing"][0])

# 真实旧任务只读校验(留档任务,不修改)
REAL = os.path.join(os.path.dirname(os.path.abspath(tr.__file__)),
                    "output", "task_20261121_105431c07d_1MC9_verify")
if os.path.isdir(REAL):
    cc5 = tr.contract_check(REAL)
    check("C6 真实旧任务(11-21 verify)→ legacy=True,缺 v0.1 字段如实列出",
          cc5["legacy"] and not cc5["ok"]
          and "input_structure_sha256" in cc5["missing"]
          and cc5["effective_status"] == "completed",
          json.dumps(cc5["missing"], ensure_ascii=False)[:160])
    check("C7 contract_check 只读(状态文件未被改)",
          tr.state_read(REAL).get("status") == "completed")
else:
    check("C6 真实旧任务目录缺失(跳过)", False, REAL)

# ============ D. rosetta_tools 源码级检查 ============
print("\n----- D. 源码级检查 -----")
src = open(os.path.join(os.path.dirname(os.path.abspath(tr.__file__)),
                        "rosetta_tools.py"), encoding="utf-8").read()
check("D1 ddg runner 收尾含 partial 分支与计数回填",
      'status="partial"' in src and '"mutations_succeeded": n_ddg' in src
      and "n_planned = len(tasks)" in src)
check("D2 runner 捕获 Rosetta 版本横幅",
      "core.init: Rosetta version:" in src and "_rosetta_version" in src)
check("D3 set_manifest 落契约版本与输入哈希",
      "contract_version=task_runtime.CONTRACT_VERSION" in src
      and "input_structure_sha256=_input_sha" in src)
check("D4 _finalize_status 弹出 manifest_patch(不入顶层键)",
      'mp = updates.pop("manifest_patch", None)' in src)

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
