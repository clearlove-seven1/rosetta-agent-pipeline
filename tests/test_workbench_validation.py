"""workbench 纯逻辑单元测试（不依赖 Rosetta / langchain / 网络）

运行：python tests/test_workbench_validation.py
覆盖：PDB 解析、目标校验（链/编号/WT/插入码/多MODEL）、编号映射、
     mutfile 文本、.ddg 解析与标签核对。
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from workbench.validation import parse_pdb, validate_target
from workbench.numbering import build_numbering, mutfile_text, save_mapping_csv
from workbench.ddgparse import parse_ddg, label_matches

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(f"{'✅ PASS' if cond else '❌ FAIL'}  {name}" + (f"  | {detail}" if detail and not cond else ""))


# ---- 最小 PDB：链 A 残基 1-5（无 gap），链 B 残基 1-3 ----
# 列布局(1-based): 1-6 "ATOM  " | 7-11 serial | 12 sp | 13-16 " CA " | 17 altloc |
# 18-20 resname | 21 sp | 22 chain | 23-26 resnum | 27 icode | 28-30 sp | 31+ 坐标
ATOM = ("ATOM  {seq:>5}  CA  {res:>3} {chain}{resnum:>4}{icode}   XXX{i:>7}"
        "                       C\n")

def pdb_line(seq, aa3="ALA", chain="A", resnum=None, icode=" "):
    return ATOM.format(seq=seq, res=aa3, chain=chain,
                       resnum=resnum if resnum else seq, icode=icode, i=1)

MINI_PDB = (
    pdb_line(1, "ALA", "A", 1) + pdb_line(2, "GLY", "A", 2) + pdb_line(3, "LYS", "A", 3)
    + pdb_line(4, "ALA", "A", 4) + pdb_line(5, "VAL", "A", 5)
    + pdb_line(6, "ALA", "B", 1) + pdb_line(7, "ALA", "B", 2) + pdb_line(8, "ALA", "B", 3)
)

tmp = tempfile.mkdtemp(prefix="wb_unit_")
mini = os.path.join(tmp, "mini.pdb")
with open(mini, "w") as f:
    f.write(MINI_PDB)

# ---- 1. 解析 ----
s = parse_pdb(mini)
check("解析: 残基总数 8", len(s.residues) == 8)
check("解析: 两条链 A=5 B=3", len(s.chains["A"]) == 5 and len(s.chains["B"]) == 3)
check("解析: 顺序编号 1..8", s.residues[-1].seq_index == 8)
check("解析: WT 氨基酸映射", s.chains["A"][0].aa3 == "ALA" and s.chains["A"][0].aa1 == "A")

# ---- 2. 目标校验 ----
ok, msg, r = validate_target(s, "A", 3)
check("校验: 正常目标通过", ok and r.seq_index == 3, msg)
ok, msg, r = validate_target(s, "C", 1)
check("校验: 链不存在拒绝", not ok and "不存在" in msg)
ok, msg, r = validate_target(s, "A", 99)
check("校验: 残基号不存在拒绝", not ok and "99" in msg)
ok, msg, r = validate_target(s, "A", 3, expected_wt="E")
check("校验: WT 不匹配拒绝", not ok and "不匹配" in msg)
ok, msg, r = validate_target(s, "A", 3, expected_wt="K")
check("校验: WT 匹配通过", ok)

# 多 MODEL 拒绝
multi = os.path.join(tmp, "multi.pdb")
with open(multi, "w") as f:
    f.write("MODEL        1\n" + MINI_PDB + "ENDMDL\nMODEL        2\n" + MINI_PDB + "ENDMDL\n")
s2 = parse_pdb(multi)
ok, msg, r = validate_target(s2, "A", 3)
check("校验: 多 MODEL 拒绝", not ok and "MODEL" in msg)

# 插入码拒绝（残基 3 与 3A 都在链 A，编号重复 + icode 命中）
ic = os.path.join(tmp, "icode.pdb")
with open(ic, "w") as f:
    f.write(pdb_line(1, "ALA", "A", 3)
            + "ATOM      2  CA  ALA A   3A      1.000  1.000  1.000  1.000  1.000           C\n")
s3 = parse_pdb(ic)
ok, msg, r = validate_target(s3, "A", 3)
check("校验: 插入码拒绝", not ok and "插入码" in msg, msg)

# HETATM 警告
het = os.path.join(tmp, "het.pdb")
with open(het, "w") as f:
    f.write(MINI_PDB + "HETATM    1  O   HOH A 200      1.0  1.0  1.0  1.0  1.0           O\n"
            "HETATM    2  C1  XYLA 300      1.0  1.0  1.0  1.0  1.0           C\n")
s4 = parse_pdb(het)
check("解析: HETATM 统计（水 vs 非水）", s4.n_water == 1 and s4.hetatm_resnames.get("XYL") == 1 or s4.hetatm_resnames.get("XYLA") == 1,
      str(dict(s4.hetatm_resnames)))
check("解析: HETATM 触发警告", len(s4.warnings) == 1)

# ---- 3. 编号映射 / mutfile ----
residues, by_key, by_seq = build_numbering(mini)
check("编号: by_key 查询", by_key[("B", 2)].seq_index == 7)
check("编号: by_seq 查询", by_seq[4].chain == "A" and by_seq[4].resnum == 4)
csvp = save_mapping_csv(residues, os.path.join(tmp, "map.csv"))
check("编号: 映射表落盘", os.path.exists(csvp) and os.path.getsize(csvp) > 50)
check("mutfile: 文本格式", mutfile_text(69, "I", "A") == "total 1\n1\nI 69 A\n")

# ---- 4. .ddg 解析（用服务器实测格式）----
ddg = os.path.join(tmp, "sample.ddg")
DDG_TEXT = (
    "COMPLEX:   Round1: WT:  -393.944  fa_atr:  -708.723\n"
    "COMPLEX:   Round2: WT:  -393.944  fa_atr:  -708.719\n"
    "COMPLEX:   Round3: WT:  -393.944  fa_atr:  -708.723\n"
    "COMPLEX:   Round1: MUT_69ALA:  -390.646  fa_atr:  -698.756\n"
    "COMPLEX:   Round2: MUT_69ALA:  -390.383  fa_atr:  -698.776\n"
    "COMPLEX:   Round3: MUT_69ALA:  -388.928  fa_atr:  -701.060\n"
)
with open(ddg, "w") as f:
    f.write(DDG_TEXT)
info = parse_ddg(ddg)
check("ddg: WT 3 轮", len(info["wt_scores"]) == 3 and abs(info["wt_avg"] + 393.944) < 1e-6)
check("ddg: MUT 3 轮", len(info["mut_scores"]) == 3)
expected_ddg = ((-390.646 - 390.383 - 388.928) / 3) + 393.944
check("ddg: ΔΔG 数值正确", abs(info["ddg"] - expected_ddg) < 1e-6, f"{info['ddg']} vs {expected_ddg}")
ok, msg = label_matches(info, 69, "A")
check("ddg: 标签核对通过", ok)
ok, msg = label_matches(info, 72, "A")
check("ddg: 标签错位被识破", not ok and "错位" in msg or "不一致" in msg, msg)

# 空文件/坏格式
bad = os.path.join(tmp, "bad.ddg")
with open(bad, "w") as f:
    f.write("no content here\n")
try:
    parse_ddg(bad)
    check("ddg: 坏格式抛错", False)
except ValueError:
    check("ddg: 坏格式抛错", True)

# 0 字节 .ddg 应抛错（README 记载的典型故障）
zero = os.path.join(tmp, "zero.ddg")
open(zero, "w").close()
try:
    parse_ddg(zero)
    check("ddg: 0字节抛错", False)
except ValueError:
    check("ddg: 0字节抛错", True)

# ---- 汇总 ----
fails = [n for n, ok in RESULTS if not ok]
print(f"\n========== 结果汇总 ==========")
print(f"通过 {len(RESULTS) - len(fails)} / {len(RESULTS)}")
sys.exit(1 if fails else 0)
