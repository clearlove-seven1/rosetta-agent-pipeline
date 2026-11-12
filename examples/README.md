# examples — 最小示例结构

本目录提供两个**单体**测试结构（均来自 RCSB PDB，原件未改动），供 workbench CLI 冒烟与科研试用。

| 目录 | PDB ID | 说明 | 用途 |
|---|---|---|---|
| `1ubq/1ubq.pdb` | [1UBQ](https://www.rcsb.org/structure/1UBQ) | 泛素，1-76，单链，无配体 | 冒烟测试（小、快、经典稳定性基准） |
| `1mc9/1mc9.pdb` | [1MC9](https://www.rcsb.org/structure/1MC9) | 内切-β-1,4-木聚糖酶 A（CBM 相关），单链酶 | 科研实际案例（本组蛋白体系） |

引用要求：使用结果时请引用对应 PDB 的原始文献（RCSB 页面列出一作与 DOI）。

示例命令（在仓库根目录、rosetta-agent env 下）：

```bash
# 1UBQ 冒烟: A76 是链末端的甘氨酸（GLY76，C 端），G76E 是一个明显的稳定性扰动
python -m workbench.cli --protein examples/1ubq/1ubq.pdb --chain A --resnum 76 --to E \
  --name ubq_smoke --note "冒烟测试"

# 或用文献常用的疏水核心位点 L73→A:
python -m workbench.cli --protein examples/1ubq/1ubq.pdb --chain A --resnum 73 --to A \
  --name ubq_L73A --note "疏水核心经典位点"

# 1MC9 科研案例: 先用映射表确认目标位点的链与编号
python -m workbench.cli --protein examples/1mc9/1mc9.pdb --chain A --resnum <N> --to <X> \
  --name mc9_case1 --note "CBM 体系单点突变"
```

注意：`runs/` 目录为运行产物，默认不上传（见 .gitignore）。
