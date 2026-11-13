"""workbench GROMACS 执行器的单元测试（fake gmx 注入，不依赖真实 GROMACS / GPU）

运行：python tests/test_workbench_gromacs.py
覆盖：
  1. fake gmx 全链路 → succeeded，manifest/steps/result 齐全
  2. 全阶段种子固定（nvt/npt/md 的 ld-seed、nvt 的 gen-seed）
  3. ns > 5 无 --confirm-long-run → 拒绝（exit 2），不建目录
  4. 同名任务目录不覆盖（追加 _N 后缀）
  5. 输入校验拒绝（HETATM）且从不调用 gmx
  6. gmx 二进制缺失 → 依赖缺失（exit 3）
  7. mdrun 阶段失败 → failed（exit 4）
  8. 断点恢复：失败前各阶段产物哈希复用（fake gmx 不再被调用）
  9. GPU 日志证据解析（纯逻辑）
"""

import json
import os
import stat
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from workbench.gromacs import extract_gpu_evidence

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(f"{'✅ PASS' if cond else '❌ FAIL'}  {name}" + (f"  | {detail}" if detail and not cond else ""))


TMP = tempfile.mkdtemp(prefix="wb_gmx_")
WORKDIR = os.path.join(TMP, "runs")
FAKE_BIN = os.path.join(TMP, "bin")
FAKE_FIX = os.path.join(TMP, "fixtures")
os.makedirs(FAKE_BIN)
os.makedirs(FAKE_FIX)

# ---------------- fake gmx ----------------
FAKE_SCRIPT = r"""#!/usr/bin/env bash
echo "gmx $*" >> "$FAKE_LOG"
cmd="$1"
case "$cmd" in
  --version)
    echo "GROMACS version:     2026.1-fake"
    echo "Precision:           mixed"
    echo "GPU support:         CUDA"
    echo "CUDA runtime:        12.80"
    exit 0 ;;
  pdb2gmx)
    cp "$FAKE_FIX/processed.gro" processed.gro
    cp "$FAKE_FIX/topol.top" topol.top
    cp "$FAKE_FIX/posre.itp" posre.itp ;;
  editconf) cp "$FAKE_FIX/stage.gro" boxed.gro ;;
  solvate)
    cp "$FAKE_FIX/stage.gro" solv.gro
    cp "$FAKE_FIX/topol_solv.top" topol.top ;;
  grompp)
    out=""; prev=""
    for a in "$@"; do [ "$prev" = "-o" ] && out="$a"; prev="$a"; done
    cp "$FAKE_FIX/generic.bin" "$out" ;;
  genion)
    cp "$FAKE_FIX/stage.gro" solv_ions.gro
    cp "$FAKE_FIX/topol_ion.top" topol.top ;;
  mdrun)
    dfn="md"; prev=""
    for a in "$@"; do [ "$prev" = "-deffnm" ] && dfn="$a"; prev="$a"; done
    if [ -n "$FAKE_FAIL_AT" ] && [ "$dfn" = "$FAKE_FAIL_AT" ]; then
      echo "Fatal error: FAKE FAIL at $dfn" >&2
      exit 1
    fi
    cp "$FAKE_FIX/stage.gro" "$dfn.gro"
    cp "$FAKE_FIX/md.log" "$dfn.log"
    cp "$FAKE_FIX/generic.bin" "$dfn.tpr"
    cp "$FAKE_FIX/generic.bin" "$dfn.edr"
    cp "$FAKE_FIX/generic.bin" "$dfn.cpt"
    cp "$FAKE_FIX/md.xtc" "$dfn.xtc" ;;
  rms) mkdir -p analysis; cp "$FAKE_FIX/rmsd_bb.xvg" analysis/rmsd_bb.xvg ;;
  rmsf) mkdir -p analysis; cp "$FAKE_FIX/rmsf_atom.xvg" analysis/rmsf_atom.xvg ;;
  energy) mkdir -p analysis; cp "$FAKE_FIX/energy.xvg" analysis/energy.xvg ;;
  *) echo "unknown cmd: $cmd" >&2; exit 1 ;;
esac
exit 0
"""

GRO = "t\n    3\n    1ALA      N    1   0.000   0.000   0.000\n    1ALA      C    2   0.100   0.000   0.000\n    2GLY      N    3   0.200   0.000   0.000\n   0.000   0.000   0.000\n"
TOPOL = '#include "amber14sb.ff/forcefield.itp"\n#ifdef POSRES\n#include "posre.itp"\n#endif\n\n[ molecules ]\n; Compound   #mols\nProtein      1\n'
MDLOG = """GROMACS:      gmx mdrun, version 2026.1-fake
Using 1 MPI thread
Hardware check: Running on 1 node with 2 compatible GPUs
GPU info:
Number of GPUs detected: 2
1 GPU selected for this run.
PP tasks will do non-perturbed short-ranged interactions on the GPU
PME tasks will do all aspects on the GPU
Performance:  1103.756
"""
XVG_RMSD = '@ s0 legend "RMSD"\n   0.000    0.010\n  10.000    0.050\n 500.000    0.140\n 1000.000   0.150\n'
XVG_RMSF = '@ s0 legend "RMSF"\n     1    0.050\n    13    0.060\n   793    0.220\n'
XVG_ENER = ('@ s0 legend "Temperature"\n@ s1 legend "Pressure"\n@ s2 legend "Potential"\n@ s3 legend "Total Energy"\n'
            "     0.000   300.00     1.00  -190000.0  -190000.0\n"
            "   500.000   302.00     2.00  -190001.0  -190001.0\n"
            "  1000.000   299.00     3.00  -190002.0  -190002.0\n")

with open(os.path.join(FAKE_BIN, "gmx"), "w") as f:
    f.write(FAKE_SCRIPT)
os.chmod(os.path.join(FAKE_BIN, "gmx"), os.stat(os.path.join(FAKE_BIN, "gmx")).st_mode | stat.S_IEXEC)

FIX = {
    "processed.gro": GRO, "stage.gro": GRO, "topol.top": TOPOL,
    "topol_solv.top": TOPOL + "SOL       100\n",
    "topol_ion.top": TOPOL + "SOL       100\nNA           5\nCL           3\n",
    "posre.itp": "#ifdef POSRES\n#endif\n",
    "md.log": MDLOG, "md.xtc": "FAKEXTC" * 16,
    "generic.bin": "FAKEBIN" * 16,
    "rmsd_bb.xvg": XVG_RMSD, "rmsf_atom.xvg": XVG_RMSF, "energy.xvg": XVG_ENER,
}
for name, content in FIX.items():
    with open(os.path.join(FAKE_FIX, name), "w") as f:
        f.write(content)

PDB = "\n".join([
    "ATOM      1  N   ALA A   1      11.104   6.134  -6.244  1.00  0.00           N",
    "ATOM      2  CA  ALA A   1      12.428   6.134  -6.244  1.00  0.00           C",
    "ATOM      3  C   ALA A   1      13.170   6.934  -5.190  1.00  0.00           C",
    "ATOM      4  N   GLY A   2      12.630   7.900  -4.400  1.00  0.00           N",
    "ATOM      5  CA  GLY A   2      11.500   7.900  -3.500  1.00  0.00           C",
    "ATOM      6  N   SER A   3      11.000   8.000  -3.000  1.00  0.00           N",
    "END",
]) + "\n"
PDB_PATH = os.path.join(TMP, "in.pdb")
with open(PDB_PATH, "w") as f:
    f.write(PDB)

os.environ["FAKE_LOG"] = os.path.join(TMP, "fake_invocations.log")
os.environ["FAKE_FIX"] = FAKE_FIX
os.environ["FAKE_FAIL_AT"] = ""

COMMON = ["--protein", PDB_PATH, "--chain", "A", "--label", "tst",
          "--gmx", os.path.join(FAKE_BIN, "gmx"), "--workdir", WORKDIR]


def latest_run_dir():
    runs = sorted(os.listdir(WORKDIR))
    return os.path.join(WORKDIR, runs[-1])


def read_manifest(run_dir):
    return json.load(open(os.path.join(run_dir, "manifest.json"), encoding="utf-8"))


def reset_log():
    open(os.environ["FAKE_LOG"], "w").close()


def fake_calls():
    return [ln for ln in open(os.environ["FAKE_LOG"]).read().splitlines() if ln.strip()]


# ---------------- 1. 全链路成功 ----------------
from workbench.gromacs import main

reset_log()
rc = main([*COMMON, "--ns", "1.0"])
check("全链路: exit 0", rc == 0, f"rc={rc}")
rd = latest_run_dir()
m = read_manifest(rd)
check("全链路: manifest succeeded", m["status"] == "succeeded", str(m["status"]))
check("全链路: 全部阶段 succeeded",
      all(m["steps"][k].get("status") == "succeeded"
          for k in ("pdb2gmx", "editconf", "solvate", "genion", "em", "nvt", "npt",
                    "grompp_md", "mdrun_md", "analysis")),
      str({k: v.get("status") for k, v in m["steps"].items()}))
check("全链路: result 汇总存在", m["result"] is not None and m["result"]["prod_ns"] == 1.0)
check("全链路: GPU 证据记录", "1 GPU selected" in json.dumps(m["gpu_evidence_mdrun_log"]))
check("全链路: scope 为 MD 口径", "不代表动力学收敛" in m["scope_and_limits"] or "收敛" in m["scope_and_limits"])

# ---------------- 2. 种子固定 ----------------
def has_seed(txt, seed):
    return any(ln.strip().startswith("ld-seed") and str(seed) in ln for ln in txt.splitlines())


mdp_md = open(os.path.join(rd, "mdps", "md.mdp")).read()
mdp_nvt = open(os.path.join(rd, "mdps", "nvt.mdp")).read()
mdp_npt = open(os.path.join(rd, "mdps", "npt.mdp")).read()
check("种子: md.mdp 固定 ld-seed", has_seed(mdp_md, 20261113), mdp_md)
check("种子: nvt.mdp 固定 ld-seed + gen-seed",
      has_seed(mdp_nvt, 20261113) and "gen-seed" in mdp_nvt and "20261113" in mdp_nvt)
check("种子: npt.mdp 固定 ld-seed", has_seed(mdp_npt, 20261113), mdp_npt)
check("种子: ions.mdp 用 cut-off（净电荷规避）", "cut-off" in open(os.path.join(rd, "mdps", "ions.mdp")).read())

# ---------------- 3. 长运行护栏 ----------------
n_dirs = len(os.listdir(WORKDIR))
rc = main([*COMMON, "--ns", "10"])
check("护栏: ns>5 无确认 → exit 2", rc == 2, f"rc={rc}")
check("护栏: 未创建新目录", len(os.listdir(WORKDIR)) == n_dirs)

# ---------------- 4. 目录不覆盖 ----------------
before = set(os.listdir(WORKDIR))
main([*COMMON, "--ns", "0.2"])
after = set(os.listdir(WORKDIR))
new_dirs = after - before
check("防覆盖: 新任务进新目录", len(new_dirs) >= 1 and before <= after, str(new_dirs))
first_dir = os.path.join(WORKDIR, sorted(before)[-1])
check("防覆盖: 旧目录原封不动", os.path.isfile(os.path.join(first_dir, "manifest.json")))

# ---------------- 5. 输入校验拒绝（HETATM），gmx 不被调用 ----------------
PDB_HET = PDB.replace("END", "HETATM  900 XYX A 900       1.0 1.0 1.0\nEND")
pdb_het = os.path.join(TMP, "het.pdb")
open(pdb_het, "w").write(PDB_HET)
reset_log()
rc = main(["--protein", pdb_het, "--chain", "A", "--label", "het",
           "--gmx", os.path.join(FAKE_BIN, "gmx"), "--workdir", WORKDIR])
check("校验: HETATM → exit 2", rc == 2, f"rc={rc}")
check("校验: gmx 从未被调用", os.path.getsize(os.environ["FAKE_LOG"]) == 0)

# ---------------- 6. 依赖缺失 ----------------
rc = main([*COMMON[:-2], "--gmx", os.path.join(TMP, "no_such_gmx"), "--workdir", WORKDIR])
check("依赖: gmx 缺失 → exit 3", rc == 3, f"rc={rc}")

# ---------------- 7. mdrun 失败 → failed ----------------
os.environ["FAKE_FAIL_AT"] = "npt"
reset_log()
rc = main([*COMMON, "--ns", "0.2", "--label", "failnpt"])
check("失败: mdrun npt 失败 → exit 4", rc == 4, f"rc={rc}")
rd_fail = latest_run_dir()
mf_ = read_manifest(rd_fail)
check("失败: manifest 状态 failed", mf_["status"] == "failed", str(mf_["status"]))
check("失败: nvt 已成功而 npt 未成功",
      mf_["steps"]["nvt"]["status"] == "succeeded" and mf_["steps"]["npt"]["status"] != "succeeded")
fail_run_dir = rd_fail
os.environ["FAKE_FAIL_AT"] = ""

# ---------------- 8. 断点恢复：复用失败前阶段 ----------------
log_before_resume = os.path.join(TMP, "fake_invocations_resume.log")
os.environ["FAKE_LOG"] = log_before_resume
open(log_before_resume, "w").close()
rc = main(["--resume", fail_run_dir, "--gmx", os.path.join(FAKE_BIN, "gmx")])
check("恢复: exit 0", rc == 0, f"rc={rc}")
m2 = read_manifest(fail_run_dir)
check("恢复: 最终 succeeded", m2["status"] == "succeeded", str(m2["status"]))
check("恢复: 失败前阶段不再调用 gmx（全部复用）",
      all(k not in open(log_before_resume).read() for k in
          ("pdb2gmx", "editconf", "solvate", "genion", "gmx mdrun -deffnm em", "gmx mdrun -deffnm nvt")),
      open(log_before_resume).read()[:300])
check("恢复: 后续阶段真实重算（npt/md）",
      "-deffnm npt" in open(log_before_resume).read() and "-deffnm md" in open(log_before_resume).read())

# ---------------- 9. GPU 证据解析（纯逻辑） ----------------
tmp_log = os.path.join(TMP, "sample_md.log")
open(tmp_log, "w").write(MDLOG)
ev = extract_gpu_evidence(tmp_log)
check("GPU 解析: gpu_selected_in_log", ev["gpu_selected_in_log"] is True)
check("GPU 解析: 性能数字", ev["performance_ns_day"] == 1103.756, str(ev["performance_ns_day"]))
check("GPU 解析: 证据行含 PP/PME on GPU",
      any("PP tasks" in ln for ln in ev["matched_lines"]) and any("PME tasks" in ln for ln in ev["matched_lines"]))

# ---------------- 汇总 ----------------
n_pass = sum(1 for _, ok in RESULTS if ok)
print(f"\n=== workbench.gromacs 测试: {n_pass}/{len(RESULTS)} 通过 ===")
sys.exit(0 if n_pass == len(RESULTS) else 1)
