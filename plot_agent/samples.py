"""内置示例数据集（可复现，seed 固定）：覆盖全部图类型的演示数据。"""
from __future__ import annotations

import numpy as np
import pandas as pd

AA20 = "ACDEFGHIKLMNPQRSTVWY"
RNG = np.random.default_rng(42)


def _rand_seq(n, p_conserved=0.0, motif=""):
    letters = list(AA20)
    seq = "".join(RNG.choice(letters, n))
    if motif:
        i = RNG.integers(0, max(1, n - len(motif)))
        seq = seq[:i] + motif + seq[i + len(motif):]
    return seq


def toy_generic() -> pd.DataFrame:
    n = 120
    g = RNG.choice(["WT", "MUT-A", "MUT-B"], n, p=[0.5, 0.3, 0.2])
    x = RNG.normal(10, 2, n)
    return pd.DataFrame({
        "group": g, "x": x.round(3),
        "y": (x * 0.8 + RNG.normal(0, 1.5, n) + np.where(g == "MUT-A", 2, 0)).round(3),
        "size": RNG.uniform(1, 20, n).round(2),
        "value": RNG.normal(0, 1, n).round(3),
    })


def funnel_decoy() -> pd.DataFrame:
    n = 600
    decoy = RNG.choice(["design_v1", "design_v2", "control"], n, p=[0.45, 0.35, 0.2])
    rmsd = np.abs(RNG.normal(0, 3.5, n)) + 0.5
    score = rmsd * 8 + RNG.normal(-22, 6, n) + np.where(decoy == "design_v1", -6, 0)
    return pd.DataFrame({"decoy": decoy, "rmsd": rmsd.round(3), "score": score.round(2)})


def perres_energy() -> pd.DataFrame:
    n = 80
    res = RNG.choice(list("ALVIFWYGSTNQDEKRHMP"), n)
    return pd.DataFrame({
        "resi": np.arange(1, n + 1), "resname": res,
        "fa_atr": -RNG.gamma(2, 1.6, n).round(2),
        "fa_rep": RNG.gamma(1.2, 0.9, n).round(2),
        "hbond_sc": -np.abs(RNG.normal(0, 1.4, n)).round(2),
        "fa_elec": RNG.normal(0, 1.2, n).round(2),
        "rama": RNG.normal(0, 0.5, n).round(2),
    })


def ddg_scan() -> pd.DataFrame:
    n = 24
    pos = RNG.integers(30, 260, n)
    aa = RNG.choice(list(AA20), n)
    mut = RNG.choice(list(AA20), n)
    return pd.DataFrame({
        "mutation": [f"{a}{p}{m}" for a, p, m in zip(aa, pos, mut)],
        "ddg": np.concatenate([RNG.normal(2.2, 1.2, 9), RNG.normal(-0.6, 0.8, 8), RNG.normal(0.3, 0.4, 7)]).round(3),
    })


def ddg_matrix() -> pd.DataFrame:
    n = 18
    muts = [f"{a}{i}{m}" for i, a, m in zip(RNG.integers(40, 90, n), RNG.choice(list(AA20), n), RNG.choice(list(AA20), n))]
    return pd.DataFrame({
        "mutation": muts,
        "bound": RNG.normal(1.5, 1.4, n).round(3),
        "unbound": RNG.normal(0.6, 0.9, n).round(3),
        "repack": RNG.normal(1.1, 1.0, n).round(3),
        "design2": RNG.normal(0.2, 0.8, n).round(3),
        "flexbb": RNG.normal(1.8, 1.2, n).round(3),
    })


def interfaces() -> pd.DataFrame:
    n = 50
    dsasa = np.abs(RNG.normal(900, 380, n)) + 120
    return pd.DataFrame({
        "complex": [f"pair_{i:02d}" for i in range(1, n + 1)],
        "dSASA": dsasa.round(1),
        "dG_cross": (-dsasa * 0.011 + RNG.normal(-3, 3.2, n)).round(2),
    })


def plddt_curve() -> pd.DataFrame:
    n = 260
    base = np.concatenate([RNG.uniform(88, 97, 90), RNG.uniform(45, 75, 60), RNG.uniform(85, 96, 110)])
    return pd.DataFrame({"pos": np.arange(1, n + 1), "plddt": np.clip(base + RNG.normal(0, 3, n), 20, 99).round(1)})


def md_rmsd() -> pd.DataFrame:
    n = 2000
    t = np.linspace(0, 100, n)
    rmsd = 1.2 + 1.6 * (1 - np.exp(-t / 18)) + RNG.normal(0, 0.16, n)
    return pd.DataFrame({"frame": t.round(3), "rmsd": rmsd.round(4),
                         "sd": (0.2 + RNG.uniform(0, 0.08, n)).round(3)})


def md_traj() -> pd.DataFrame:
    n = 3000
    which = RNG.random(n) < 0.45
    rmsd = np.where(which, RNG.normal(1.2, 0.35, n), RNG.normal(3.4, 0.6, n))
    rg = np.where(which, RNG.normal(14.2, 0.4, n), RNG.normal(15.4, 0.55, n))
    return pd.DataFrame({"rmsd": np.abs(rmsd).round(3), "rg": rg.round(3)})


def msa_seqs() -> pd.DataFrame:
    motif = "GKSTLILAF"
    rows = []
    for i in range(70):
        s = _rand_seq(60, motif=motif if RNG.random() < 0.85 else "")
        # 在非 motif 区引入家族共有的弱保守残基
        s = list(s)
        for j in range(60):
            if j % 7 == 3 and RNG.random() < 0.5:
                s[j] = "L"
        rows.append("".join(s))
    return pd.DataFrame({"seq_id": [f"seq_{i:02d}" for i in range(70)], "sequence": rows})


def deg_results() -> pd.DataFrame:
    n = 400
    genes = [f"GENE{i:03d}" for i in range(1, n + 1)]
    lfc = RNG.normal(0, 1.2, n)
    sig = RNG.random(n) < 0.18
    lfc[sig] = RNG.normal(0, 2.6, sig.sum())
    pval = np.clip(RNG.beta(0.6, 8, n), 1e-12, 1)
    pval[sig] = RNG.uniform(1e-9, 1e-3, sig.sum())
    return pd.DataFrame({"gene": genes, "log2fc": lfc.round(3), "pval": pval,
                         "padj": np.clip(pval * 3, 1e-12, 1)})


def expr_matrix() -> pd.DataFrame:
    n = 80
    genes = [f"GENE{i:03d}" for i in range(1, n + 1)]
    base = RNG.lognormal(3, 1, n)
    eff = RNG.normal(0, 0.5, n)
    d = {"gene": genes, "mean_ctrl": base.round(2)}
    for i in (1, 2, 3):
        d[f"treat_{i}"] = (base * np.exp(eff) * RNG.lognormal(0, 0.25, n)).round(2)
    for i in (1, 2, 3):
        d[f"ctrl_{i}"] = (base * RNG.lognormal(0, 0.25, n)).round(2)
    d["mean_treat"] = (base * np.exp(eff)).round(2)
    return pd.DataFrame(d)


def go_enrich() -> pd.DataFrame:
    terms = ["protein folding", "ATP binding", "membrane transport", "oxidation-reduction",
             "signal transduction", "DNA repair", "cell adhesion", "proteolysis",
             "lipid metabolic process", "immune response", "translation elongation",
             "chromatin remodeling", "vesicle trafficking", "apoptotic process",
             "carbohydrate binding", "ion channel activity", "ubiquitin cycle",
             "cell cycle arrest", "response to stress", "peptidase activity",
             "ribosome biogenesis", "autophagy"]
    n = len(terms)
    pval = np.clip(RNG.beta(0.5, 6, n), 1e-14, 1)
    return pd.DataFrame({"term": terms, "count": RNG.integers(4, 220, n),
                         "fold": RNG.uniform(1.2, 8, n).round(2), "pval": pval.round(10)})


def gsea_rank() -> pd.DataFrame:
    n = 800
    genes = [f"GENE{i:03d}" for i in range(1, n + 1)]
    score = RNG.normal(0, 1, n)
    inset = np.zeros(n)
    top = RNG.choice(np.arange(120), 38, replace=False)
    inset[top] = 1
    score[top] += RNG.normal(1.8, 0.4, 38)
    return pd.DataFrame({"gene": genes, "score": score.round(4), "in_set": inset.astype(int)})


def gwas_snp() -> pd.DataFrame:
    n = 3000
    chrom = RNG.integers(1, 6, n)
    pos = np.zeros(n)
    for c in range(1, 6):
        m = chrom == c
        pos[m] = RNG.integers(1, 2_400_000, m.sum())
    pval = np.clip(RNG.beta(0.4, 12, n), 1e-16, 1)
    hits = RNG.random(n) < 0.01
    pval[hits] = RNG.uniform(1e-11, 1e-7, hits.sum())
    return pd.DataFrame({"chrom": [f"chr{c}" for c in chrom], "pos": pos.astype(int),
                         "p": pval, "rsid": [f"rs{i}" for i in RNG.integers(100000, 999999, n)]})


def training_log() -> pd.DataFrame:
    n = 60
    ep = np.arange(1, n + 1)
    tl = 2.4 * np.exp(-ep / 12) + 0.25 + RNG.normal(0, 0.02, n)
    vl = 2.4 * np.exp(-ep / 12) + 0.42 + ep ** 2 * 0.00012 + RNG.normal(0, 0.03, n)
    return pd.DataFrame({"epoch": ep, "train_loss": tl.round(4), "val_loss": vl.round(4),
                         "train_acc": (1 - tl / 3).round(4), "val_acc": (1 - vl / 3.1).round(4)})


def model_scores() -> pd.DataFrame:
    n = 400
    y = RNG.integers(0, 2, n)
    return pd.DataFrame({
        "y_true": y,
        "model_A": np.clip(y * 0.6 + RNG.normal(0.2, 0.5, n), 0, 1).round(4),
        "model_B": np.clip(y * 0.45 + RNG.normal(0.35, 0.65, n), 0, 1).round(4),
    })


def predictions() -> pd.DataFrame:
    n = 300
    true = RNG.choice(["helix", "strand", "coil"], n, p=[0.4, 0.25, 0.35])
    conf = np.array([0.75, 0.6, 0.8])
    mapping = {"helix": 0, "strand": 1, "coil": 2}
    ti = np.array([mapping[t] for t in true])
    pred = ti.copy()
    flip = RNG.random(n) < 1 - conf[ti]
    pred[flip] = RNG.integers(0, 3, flip.sum())
    inv = {0: "helix", 1: "strand", 2: "coil"}
    return pd.DataFrame({"y_true": true, "y_pred": [inv[p] for p in pred]})


def ppi_edges() -> pd.DataFrame:
    hubs = ["P53", "MDM2", "ATXN1", "BRCA1", "EGFR", "AKT1", "MYC", "MAX"]
    others = [f"P{i:03d}" for i in range(1, 33)]
    nodes = hubs + others
    rows = []
    for i in range(90):
        if RNG.random() < 0.55:
            a, b = RNG.choice(hubs, 2, replace=False)
        else:
            a, b = RNG.choice(nodes, 2, replace=False)
        rows.append((a, b, RNG.uniform(0.3, 1.0)))
    return pd.DataFrame(rows, columns=["src", "dst", "weight"]).round(3)


SAMPLES = {
    "toy_generic": ("通用演示数据", "分组×多数值列，适合散点/柱状/箱线/直方/密度/相关性", toy_generic),
    "funnel_decoy": ("Rosetta 对接 decoy", "score vs RMSD，三群体（漏斗/Score分布/界面散点）", funnel_decoy),
    "perres_energy": ("每残基能量分解", "5 个能量项 × 80 残基", perres_energy),
    "ddg_scan": ("突变 ΔΔG 扫描", "24 个突变的 ΔΔG（ddG 条形图）", ddg_scan),
    "ddg_matrix": ("ΔΔG 矩阵", "18 突变 × 5 协议（矩阵热图）", ddg_matrix),
    "interfaces": ("蛋白复合物界面", "dSASA vs ΔG_cross（界面散点）", interfaces),
    "plddt_curve": ("AlphaFold pLDDT", "260 残基置信度曲线", plddt_curve),
    "md_rmsd": ("MD RMSD 轨迹", "2000 帧 RMSD±SD（时间序列带）", md_rmsd),
    "md_traj": ("MD 轨迹二维分布", "RMSD vs Rg 两构象态（自由能面/二维密度）", md_traj),
    "msa_seqs": ("蛋白家族 MSA", "70 条 × 60 aa，含保守 motif（Logo/保守性/组成/疏水）", msa_seqs),
    "deg_results": ("差异表达结果", "400 基因 log2fc + p（火山图/MA）", deg_results),
    "expr_matrix": ("表达矩阵", "80 基因 × 6 样本（聚类热图/PCA）", expr_matrix),
    "go_enrich": ("GO 富集结果", "22 通路（富集气泡/条形）", go_enrich),
    "gsea_rank": ("GSEA 排序表", "800 基因分数 + 集合标记（GSEA 曲线）", gsea_rank),
    "gwas_snp": ("GWAS 汇总", "3000 SNP（曼哈顿图）", gwas_snp),
    "training_log": ("训练日志", "60 epoch 4 指标（训练曲线）", training_log),
    "model_scores": ("模型打分对比", "y_true + 双模型分数（ROC/PR）", model_scores),
    "predictions": ("分类预测", "三分类真实/预测（混淆矩阵）", predictions),
    "ppi_edges": ("PPI 边表", "40 节点 90 边（网络图/桑基）", ppi_edges),
}
