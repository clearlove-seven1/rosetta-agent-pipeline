"""workbench MMPBSA 集成的单元测试（纯解析逻辑，不依赖 gmx_MMPBSA/AMBER）

运行：python tests/test_workbench_mmpbsa.py
覆盖：parse_dat（1.7.0 实测 .dat 格式）、parse_csv_frame_totals（节标题+逐帧）、
build_input（PBRadii 位置/igb 配对）、dashboard.collect_runs 分类（递归深度）。
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from workbench.mmpbsa import build_input, parse_csv_frame_totals, parse_dat
from workbench.dashboard import collect_runs

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(f"{'✅ PASS' if cond else '❌ FAIL'}  {name}" + (f"  | {detail}" if detail and not cond else ""))


DAT_GB = """FINAL RESULTS GENERALIZED BORN

GENERALIZED BORN:

Complex:
Energy Component       Average     SD(Prop.)         SD   SEM(Prop.)        SEM   Block SD  Block SEM
-----------------------------------------------------------------------------------------------------
BOND                    145.46          8.01       8.01         1.13       1.13       2.73       0.97
EEL                   -3878.97         41.85      41.85         5.92       5.92      29.85      10.55
EGB                   -1193.84         40.20      40.20         5.68       5.68      29.71      10.50
ESURF                    22.86          0.72       0.72         0.10       0.10       0.54       0.19

GGAS                    188.84         50.69      41.18         7.17       5.82      23.69       8.38
GSOLV                 -1170.98         40.20      39.77         5.69       5.62      29.24      10.34

TOTAL                  -982.14         64.70      20.93         9.15       2.96       9.93       3.51
"""

DAT_PB = """FINAL RESULTS POISSON BOLTZMANN

POISSON BOLTZMANN:

Complex:
Energy Component       Average     SD(Prop.)         SD
-----------------------------------------------------------------------------------------------------
BOND                    145.46          8.01       8.01
TOTAL                  -964.10         64.16      21.00
"""

CSV_GB = """GENERALIZED BORN:
Frame #,BOND,ANGLE,DIHED,VDWAALS,EEL,1-4 VDW,1-4 EEL,EGB,ESURF,GGAS,GSOLV,TOTAL
101,153.0,387.71,605.64,-327.86,-3902.66,172.13,3073.59,-1172.2,22.22,161.54,-1149.98,-988.44
105,150.0,380.00,600.00,-330.00,-3900.00,170.00,3070.00,-1170.0,22.00,160.00,-1148.00,-985.00
"""

# ---------------- parse_dat ----------------
d = parse_dat(DAT_GB)
check("dat: 识别 GB 节", list(d.keys()) == ["GENERALIZED BORN"], str(list(d.keys())))
check("dat: TOTAL 均值", d["GENERALIZED BORN"]["total"] == -982.14, str(d["GENERALIZED BORN"]))
check("dat: TOTAL SD", d["GENERALIZED BORN"]["sd"] == 64.70)
check("dat: 分量 EEL", d["GENERALIZED BORN"]["components"]["EEL"] == -3878.97)

d2 = parse_dat(DAT_PB)
check("dat: 识别 PB 节", list(d2.keys()) == ["POISSON BOLTZMANN"], str(list(d2.keys())))
check("dat: PB TOTAL", d2["POISSON BOLTZMANN"]["total"] == -964.10)

try:
    parse_dat("no sections here")
    check("dat: 空内容报错", False)
except ValueError:
    check("dat: 空内容报错", True)

# ---------------- parse_csv_frame_totals ----------------
tmp = tempfile.mkdtemp(prefix="wb_mmpbsa_")
csv_path = os.path.join(tmp, "t.csv")
open(csv_path, "w").write(CSV_GB)
c = parse_csv_frame_totals(csv_path)
check("csv: 帧数", c["n_frames"] == 2, str(c))
check("csv: TOTAL 均值", abs(c["total_mean"] - (-988.44 + -985.00) / 2) < 1e-9, str(c))
check("csv: TOTAL SD>0", c["total_sd"] > 0)

bad = os.path.join(tmp, "bad.csv")
open(bad, "w").write("no,header,here\n1,2,3\n")
try:
    parse_csv_frame_totals(bad)
    check("csv: 缺表头报错", False)
except ValueError:
    check("csv: 缺表头报错", True)

# ---------------- build_input ----------------
gb_in = build_input("gb", 101, 300, 4, 0.15, 8, 4)
check("input: PBRadii 在 &general", "verbose=2, PBRadii=4," in gb_in)
check("input: &gb 含 igb=8 与 saltcon", "igb=8" in gb_in and "saltcon=0.15" in gb_in)
pb_in = build_input("pb", 1, 10, 1, 0.15, 8, 4)
check("input: &pb 含 istrng", "&pb" in pb_in and "istrng=0.15" in pb_in)
check("input: pb 无 &gb", "&gb" not in pb_in)

# ---------------- dashboard.collect_runs 分类（递归） ----------------
tmp2 = tempfile.mkdtemp(prefix="wb_dash_")
os.makedirs(os.path.join(tmp2, "ros1"))
os.makedirs(os.path.join(tmp2, "gromacs", "md1"))
os.makedirs(os.path.join(tmp2, "mmpbsa", "mm1"))


def w(path, obj):
    json.dump(obj, open(path, "w"), ensure_ascii=False)


w(os.path.join(tmp2, "ros1", "manifest.json"), {
    "run_id": "ros1", "status": "succeeded",
    "task": {"calc_type": "单体单点突变稳定性计算 (cartesian_ddg 常规模式)",
             "protein_path": "x.pdb", "chain": "A", "resnum_pdb": 1, "mut_aa1": "E"},
    "result": {"ddg_reu": 1.086}})
w(os.path.join(tmp2, "gromacs", "md1", "manifest.json"), {
    "run_id": "md1", "status": "succeeded",
    "task": {"calc_type": "GROMACS MD 流程（EM→NVT→NPT→生产 MD）", "label": "t"},
    "gpu_evidence_mdrun_log": {"performance_ns_day": 100.0}})
w(os.path.join(tmp2, "mmpbsa", "mm1", "manifest.json"), {
    "run_id": "mm1", "status": "succeeded",
    "task": {"calc_type": "MM-GB/PB 端点能量分析（stability 模式, 探索性）", "md_run": "md1"},
    "result": {"sections": {"GENERALIZED BORN": {"total": -1.0, "sd": 0.1}},
               "frame_totals": {"n_frames": 2}}})
d = collect_runs(tmp2)
check("dash: 三类各 1（递归扫描）", len(d["rosetta"]) == 1 and len(d["md"]) == 1 and len(d["mmpbsa"]) == 1,
      str({k: len(v) for k, v in d.items() if isinstance(v, list)}))
check("dash: MMPBSA TOTAL 格式化", "±" in d["mmpbsa"][0][4], str(d["mmpbsa"][0]))

# ---------------- 汇总 ----------------
n_pass = sum(1 for _, ok in RESULTS if ok)
print(f"\n=== workbench mmpbsa/dashboard 测试: {n_pass}/{len(RESULTS)} 通过 ===")
sys.exit(0 if n_pass == len(RESULTS) else 1)
