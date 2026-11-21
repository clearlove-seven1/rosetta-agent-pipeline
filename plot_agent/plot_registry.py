"""图类型注册表：36 种图的元数据、参数 schema、自动填参与智能推荐。"""
from __future__ import annotations

import re

import renderers_basic as B
import renderers_bio as BIO
import renderers_omics as O

CATS = [
    ("basic", "通用统计"),
    ("structure", "结构与Rosetta"),
    ("md", "分子动力学"),
    ("seq", "序列分析"),
    ("omics", "组学与差异"),
    ("ml", "机器学习"),
    ("net", "网络与通路"),
]


# ---------- 参数 schema 构建 ----------
def p_col(key, label, accept, required=False, hint=""):
    return {"key": key, "label": label, "kind": "col", "accept": accept, "required": required, "hint": hint}


def p_multi(key, label, accept=("numeric",), hint=""):
    return {"key": key, "label": label, "kind": "cols_multi", "accept": list(accept), "hint": hint}


def p_num(key, label, default=None, hint=""):
    return {"key": key, "label": label, "kind": "number", "default": default, "hint": hint}


def p_sel(key, label, options, default=None, hint=""):
    return {"key": key, "label": label, "kind": "select", "options": options,
            "default": default or (options[0] if options else None), "hint": hint}


def p_bool(key, label, default=False, hint=""):
    return {"key": key, "label": label, "kind": "bool", "default": default, "hint": hint}


def T(pid, cat, name, desc, params, mpl=None, ply=None, ins=None, fill=None, sug=None):
    return {"id": pid, "cat": cat, "name": name, "desc": desc, "params": params,
            "mpl": mpl, "ply": ply, "ins": ins, "fill": fill, "sug": sug}


ANYCAT = ["categorical", "binary", "text"]
NUM = ["numeric"]
SEQ = ["sequence"]


# ---------- 列名模式匹配（填参/推荐共用） ----------
def colnames(profile):
    return [col["name"] for col in profile["columns"]]


def pick(profile, patterns, types=None):
    """按正则模式（不区分大小写）选第一个匹配列；patterns 不匹配则 None。"""
    for pat in patterns:
        rx = re.compile(pat, re.I)
        for col in profile["columns"]:
            if types and col["dtype"] not in types:
                continue
            if rx.search(col["name"]):
                return col["name"]
    return None


def first_of_type(profile, types, exclude=()):
    for col in profile["columns"]:
        if col["dtype"] in types and col["name"] not in exclude:
            return col["name"]
    return None


def all_of_type(profile, types, exclude=(), cap=8):
    out = [col["name"] for col in profile["columns"]
           if col["dtype"] in types and col["name"] not in exclude]
    return out[:cap]


# ---------- 各类型填参函数 ----------
def _f_scatter(p):
    nums = all_of_type(p, NUM, cap=2)
    return {"x": nums[0] if nums else None, "y": nums[1] if len(nums) > 1 else None,
            "color": first_of_type(p, ANYCAT) or None, "trend": "线性"}


def _f_line(p):
    x = pick(p, [r"epoch|step|iter|time|frame|^t$|pos|index|num"], NUM) or first_of_type(p, NUM)
    return {"x": x, "y": first_of_type(p, NUM, exclude=(x,) if x else ()),
            "group": first_of_type(p, ANYCAT) or None}


def _f_bar(p):
    x = first_of_type(p, ANYCAT) or first_of_type(p, NUM)
    return {"x": x, "y": first_of_type(p, NUM, exclude=(x,) if x else ()),
            "agg": "均值", "sort": "按数值降序"}


def _f_box(p):
    x = first_of_type(p, ANYCAT)
    return {"x": x, "y": first_of_type(p, NUM, exclude=(x,) if x else ())}


def _f_hist(p):
    x = first_of_type(p, NUM)
    hue = first_of_type(p, ANYCAT)
    return {"x": x, "bins": 30, "hue": hue}


def _f_kde2d(p):
    nums = all_of_type(p, NUM, cap=2)
    return {"x": nums[0] if nums else None, "y": nums[1] if len(nums) > 1 else None,
            "bins": 90, "filled": True, "points": True}


def _f_corrheat(p):
    return {"method": "pearson", "annot": True}


def _f_funnel(p):
    x = pick(p, [r"rmsd|dist|decoy|rank|irms"], NUM)
    y = pick(p, [r"score|total_score|energy|dg|g_"], NUM)
    nums = all_of_type(p, NUM, cap=4)
    return {"x": x or (nums[1] if len(nums) > 1 else nums[0] if nums else None),
            "y": y or (nums[0] if nums else None),
            "group": first_of_type(p, ANYCAT) or None}


def _f_scorehist(p):
    x = pick(p, [r"score|energy"], NUM) or first_of_type(p, NUM)
    hue = first_of_type(p, ANYCAT)
    return {"x": x, "bins": 40, "hue": hue if hue else None}


def _f_perres(p):
    x = pick(p, [r"resi|resid|^pos|position|^num|^idx|^id$|^n$"], NUM) or first_of_type(p, NUM)
    vals = all_of_type(p, NUM, exclude=(x,) if x else (), cap=8)
    return {"x": x, "values": vals}


def _f_ddgbar(p):
    label = pick(p, [r"mut|res|descrip|variant|name|label|^id$|^aa$|突变"], ANYCAT) or first_of_type(p, ANYCAT)
    value = pick(p, [r"ddg|d_dg|delta_dg|delta|dd_g|ΔΔG"], NUM) or first_of_type(p, NUM)
    return {"label": label, "value": value, "cut": 2.0, "top_n": 60}


def _f_matheat(p):
    index = first_of_type(p, ANYCAT) or (colnames(p)[0] if colnames(p) else None)
    return {"index": index, "values": all_of_type(p, NUM, cap=40),
            "cmap": "发散 (RdBu_r)", "annot": False}


def _f_iface(p):
    x = pick(p, [r"sasa|area|buried|interface"], NUM)
    y = pick(p, [r"dg|g_cross|energy|score"], NUM)
    nums = all_of_type(p, NUM, cap=3)
    return {"x": x or (nums[0] if nums else None), "y": y or (nums[1] if len(nums) > 1 else None)}


def _f_plddt(p):
    pos = pick(p, [r"^pos|resi|position|^num|^idx|^i$|^n$|index"], NUM) or first_of_type(p, NUM)
    val = pick(p, [r"plddt|b_factor|bfactor|conf|score"], NUM)
    nums = all_of_type(p, NUM, exclude=(pos,) if pos else (), cap=3)
    return {"pos": pos, "val": val or (nums[0] if nums else None)}


def _f_tsband(p):
    t = pick(p, [r"time|frame|step|^t$|^idx|^x$"], NUM) or first_of_type(p, NUM)
    rest = all_of_type(p, NUM, exclude=(t,) if t else (), cap=3)
    mean = pick(p, [r"rmsd|rmsf|mean|value|^y$"], NUM)
    mean = mean if mean in rest else (rest[0] if rest else None)
    err = pick(p, [r"std|sd|sem|err|dev"], NUM)
    err = err if (err and err in rest) else None  # 只按名字认误差列，别错拿相邻数值列
    return {"t": t, "mean": mean, "err": err}


def _f_felandscape(p):
    nums = all_of_type(p, NUM, cap=2)
    return {"x": nums[0] if nums else None, "y": nums[1] if len(nums) > 1 else None,
            "bins": 50, "temp": 298.15, "fmax": 10}


def _f_seq(p):
    return {"seq": first_of_type(p, SEQ) or first_of_type(p, ["text"])}


def _f_hydro(p):
    return {"seq": first_of_type(p, SEQ) or first_of_type(p, ["text"]), "window": 9}


def _f_volc(p):
    effect = pick(p, [r"log2fc|logfc|^fc$|effect|ddg|delta|beta|diff"], NUM) or first_of_type(p, NUM)
    pval = pick(p, [r"padj|p_adj|pvalue|p_value|^p$|pval|qval"], NUM)
    nums = all_of_type(p, NUM, cap=4)
    if not pval:
        pval = next((n for n in nums if n != effect), None)
    lab = pick(p, [r"gene|name|^id$|symbol|term|descrip|variant|mut"], ANYCAT) or first_of_type(p, ANYCAT)
    return {"effect": effect, "pval": pval, "effect_cut": 1.0, "p_cut": 0.05,
            "label_top": 8, "label_col": lab}


def _f_clustermap(p):
    index = first_of_type(p, ANYCAT) or (colnames(p)[0] if colnames(p) else None)
    return {"index": index, "values": all_of_type(p, NUM, cap=40), "zscore": "按行"}


def _f_pca(p):
    return {"group": first_of_type(p, ANYCAT) or None, "standardize": True, "loadings": False}


def _f_enrichdot(p):
    term = pick(p, [r"term|descrip|pathway|^name$|^id$|^go"], ANYCAT) or first_of_type(p, ANYCAT)
    pval = pick(p, [r"padj|p_adj|pvalue|p_value|^p$|pval|qval"], NUM) or first_of_type(p, NUM)
    x = pick(p, [r"fold|ratio|percent|rich|enrich|score"], NUM)
    size = pick(p, [r"count|size|ngene|number"], NUM)
    return {"term": term, "pval": pval, "x": x, "size": size, "p_cut": 0.05}


def _f_enrichbar(p):
    term = pick(p, [r"term|descrip|pathway|^name$|^id$|^go"], ANYCAT) or first_of_type(p, ANYCAT)
    pval = pick(p, [r"padj|p_adj|pvalue|p_value|^p$|pval|qval"], NUM) or first_of_type(p, NUM)
    return {"term": term, "pval": pval, "top_n": 20, "count": pick(p, [r"count|ngene|number"], NUM), "p_cut": 0.05}


def _f_gsea(p):
    score = pick(p, [r"score|metric|stat|^logfc|fc"], NUM) or first_of_type(p, NUM)
    inset = pick(p, [r"in_set|inset|hit|member|^set$|is_"], NUM) or first_of_type(p, NUM, exclude=(score,) if score else ())
    return {"score": score, "inset": inset}


def _f_maplot(p):
    a = pick(p, [r"^a$|ctrl|control|base|cond1|wt|ref|b$"], NUM) or first_of_type(p, NUM)
    b = pick(p, [r"^b$|treat|case|cond2|mut|ko|a$"], NUM) or first_of_type(p, NUM, exclude=(a,) if a else ())
    return {"a": a, "b": b, "cut": 1.0}


def _f_manh(p):
    chrom = pick(p, [r"chrom|chr|^contig"], ANYCAT) or first_of_type(p, ANYCAT)
    pos = pick(p, [r"^pos|bp|loc|^end$"], NUM) or first_of_type(p, NUM)
    pval = pick(p, [r"padj|pvalue|p_value|^p$|pval"], NUM) or first_of_type(p, NUM, exclude=(pos,) if pos else ())
    lab = pick(p, [r"rs|snp|gene|^id$|name"], ANYCAT)
    return {"chrom": chrom, "pos": pos, "pval": pval, "p_cut": 5e-8, "label_col": lab}


def _f_traincurve(p):
    x = pick(p, [r"epoch|step|iter|^t$|^n$"], NUM) or first_of_type(p, NUM)
    return {"x": x, "values": all_of_type(p, NUM, exclude=(x,) if x else (), cap=8), "logy": False}


def _f_scores(p):
    ytrue = pick(p, [r"^y_true$|y_true|label|truth|target|^y$|^class$"], ANYCAT) or \
        first_of_type(p, ["binary", "categorical", "text"])
    scores = pick_multi_scores(p, exclude=(ytrue,) if ytrue else ())
    return {"ytrue": ytrue, "scores": scores}


def pick_multi_scores(p, exclude=()):
    named = [col["name"] for col in p["columns"]
             if col["dtype"] == "numeric" and re.search(r"prob|score|pred|proba|conf", col["name"], re.I)
             and col["name"] not in exclude]
    if named:
        return named[:4]
    return [col["name"] for col in p["columns"]
            if col["dtype"] == "numeric" and col["name"] not in exclude][:4]


def _f_cm(p):
    ytrue = pick(p, [r"^y_true$|y_true|label|truth|target|^y$|^actual|^real"], ANYCAT) or first_of_type(p, ANYCAT)
    ypred = pick(p, [r"pred|predict|call|classified|^yhat"], ANYCAT) or first_of_type(p, ANYCAT, exclude=(ytrue,) if ytrue else ())
    return {"ytrue": ytrue, "ypred": ypred}


def _f_net(p):
    src = pick(p, [r"src|source|node1|^from|protein1|gene1|^a$"], ANYCAT) or first_of_type(p, ANYCAT)
    dst = pick(p, [r"dst|target|node2|^to$|protein2|gene2|^b$"], ANYCAT) or first_of_type(p, ANYCAT, exclude=(src,) if src else ())
    if dst == src:
        dst = None
    return {"src": src, "dst": dst, "weight": pick(p, [r"weight|score|combined|value"], NUM) or None,
            "top_n": 120, "label_top": 14}


def _f_sankey(p):
    src = pick(p, [r"src|source|node1|^from|protein1|^a$|stage|category"], ANYCAT) or first_of_type(p, ANYCAT)
    dst = pick(p, [r"dst|target|node2|^to$|protein2|^b$|next|dest"], ANYCAT) or first_of_type(p, ANYCAT, exclude=(src,) if src else ())
    return {"src": src, "dst": dst, "value": pick(p, [r"value|count|weight|flow"], NUM) or None, "top_n": 25}


# ---------- 推荐规则 ----------
def _n_type(p, t):
    return sum(1 for col in p["columns"] if col["dtype"] == t)


def _has(p, patterns, types=None):
    return pick(p, patterns, types) is not None


def suggest_plots(profile):
    p = profile
    n_num = _n_type(p, "numeric")
    n_cat = _n_type(p, "categorical") + _n_type(p, "binary")
    has_seq = _n_type(p, "sequence") > 0
    sugs = []

    def add(tid, score, reason):
        sugs.append({"type": tid, "score": score, "reason": reason})

    if _has(p, [r"rmsd|irms|dist|decoy"], NUM) and _has(p, [r"score|energy"], NUM):
        add("funnel", 0.95, "同时存在 score 与 RMSD/距离类数值 → 能量漏斗是构象采样的标准检验图")
    if _has(p, [r"ddg|d_dg|delta"], NUM):
        add("ddgbar", 0.9, "发现 ΔΔG/能量差数值列 → 单突变影响条形图")
    if _has(p, [r"padj|p_adj|pvalue|p_value|^p$|pval|qval"], NUM) and _has(p, [r"log2fc|logfc|^fc$|effect|ddg|delta|beta|diff"], NUM):
        add("volc", 0.92, "效应量 + p 值同现 → 经典火山图")
    if _has(p, [r"chrom|chr"], ANYCAT) and _has(p, [r"^pos|bp|loc"], NUM) and _has(p, [r"pval|^p$|pvalue"], NUM):
        add("manhattan", 0.9, "染色体 + 位置 + p 值 → 曼哈顿图")
    if has_seq:
        add("logo", 0.88, "识别到序列列 → 序列 Logo 展示保守 motif")
        add("aacomp", 0.6, "序列列可统计氨基酸组成")
        add("entropy", 0.55, "序列列可计算逐位置保守性")
    if _has(p, [r"src|source|node1|^from|protein1"], ANYCAT) and _has(p, [r"dst|target|node2|^to$|protein2"], ANYCAT):
        add("network", 0.85, "source/target 成对列 → 互作网络")
        if _has(p, [r"weight|score|value|count"], NUM):
            add("sankey", 0.62, "带权重的流向数据 → 桑基图")
    if _has(p, [r"epoch|step|iter"], NUM) and _has(p, [r"loss|acc|metric|auc|f1"], NUM):
        add("traincurve", 0.88, "epoch/step + loss/acc → 训练曲线")
    if _has(p, [r"^y_true$|label|truth|target"], ANYCAT) and _has(p, [r"prob|score|pred|proba"], NUM):
        add("roc", 0.85, "真实标签 + 预测分数 → ROC 曲线")
        add("prc", 0.7, "同一组列也可画 PR 曲线")
    if _has(p, [r"^y_true$|label|truth"], ANYCAT) and _has(p, [r"pred|predict|call"], ANYCAT):
        add("cm", 0.85, "真实/预测标签成对 → 混淆矩阵")
    if _has(p, [r"time|frame"], NUM) and _has(p, [r"rmsd|rmsf|value|mean"], NUM):
        add("tsband", 0.8, "时间帧 + 数值 → 时间序列（可带 ±SD 带）")
    if _has(p, [r"time|^t_|_ps$|frame|step|epoch|iter|^t$|^x$"], NUM) and n_num >= 2:
        add("traincurve", 0.64, "时间 + 多个数值列 → 多线看随时间演化")
        add("line", 0.6, "时间 × 数值 → 折线走势")
    if _has(p, [r"term|descrip|pathway|^go"], ANYCAT) and _has(p, [r"pval|^p$|pvalue|padj"], NUM):
        if _has(p, [r"count"], NUM):
            add("enrichdot", 0.85, "通路名 + p 值 + 基因数 → 富集气泡图")
        add("enrichbar", 0.7, "通路 + 显著性 → 富集条形图")
    if _has(p, [r"score|metric|stat"], NUM) and _has(p, [r"in_set|inset|hit|member|^set$"], NUM):
        add("gsea", 0.85, "排序分数 + 集合成员标记 → GSEA 富集曲线")
    if n_num >= 4:
        add("corrheat", 0.72, f"{n_num} 个数值列 → 相关性矩阵总览")
    if n_num >= 6:
        add("pca", 0.68, f"{n_num} 个数值列 → PCA 降维看样本分组")
    if len(p["columns"]) >= 3 and 3 <= len(p["columns"]) <= 40 and p["n_rows"] >= 4:
        add("matheat", 0.62, "宽表数值矩阵 → 矩阵热图（ddG/接触图/DCCM 通用）")
    if n_num >= 2 and p["n_rows"] > 300:
        add("kde2d", 0.55, "大量双数值点 → 二维密度分布")
        add("felandscape", 0.5, "大量双数值点（轨迹类数据）→ 自由能面")
    if n_num >= 2:
        add("scatter", 0.5, "两个以上数值列 → 散点图考察关联")
    if n_num >= 1 and n_cat >= 1:
        add("box", 0.52, "数值 × 分组 → 箱线图比较分布")
        add("violin", 0.48, "数值 × 分组 → 小提琴图看分布形状")
    if n_num >= 1:
        add("histogram", 0.45, "数值列分布直方图")
    if n_cat >= 1:
        add("bar", 0.42, "分类列计数/聚合柱状图")

    sugs.sort(key=lambda s: -s["score"])
    top = [s for s in sugs if s["score"] >= 0.5][:6]
    return top if top else sugs[:3]


# ---------- 注册表 ----------
PLOTS = {}


def _reg(*args, **kw):
    t = T(*args, **kw)
    PLOTS[t["id"]] = t


_reg("scatter", "basic", "散点图", "两列数值的关联，可按类别着色、按数值定大小、加线性趋势线",
     [p_col("x", "X 轴", NUM, True), p_col("y", "Y 轴", NUM, True),
      p_col("color", "颜色（分组/渐变）", ANYCAT + NUM), p_col("size", "点大小", NUM),
      p_sel("trend", "趋势线", ["无", "线性"])],
     mpl=B.mpl_scatter, ply=B.ply_scatter, ins=B.ins_scatter, fill=_f_scatter)

_reg("line", "basic", "折线图", "沿 x 的数值走势，支持分组多线；x 重复时按均值聚合",
     [p_col("x", "X 轴", NUM + ["datetime", "categorical", "binary"], True),
      p_col("y", "Y 轴", NUM, True), p_col("group", "分组（多线）", ANYCAT)],
     mpl=B.mpl_line, ply=B.ply_line, ins=B.ins_line, fill=_f_line)

_reg("bar", "basic", "柱状图", "类别计数或数值聚合，支持分组/堆叠/横向/排序",
     [p_col("x", "类别轴", ANYCAT, True), p_col("y", "数值（空=计数）", NUM),
      p_sel("agg", "聚合方式", ["均值", "总和", "中位数", "计数"]),
      p_col("group", "分组", ANYCAT), p_bool("stacked", "堆叠"),
      p_bool("horizontal", "横向"), p_sel("sort", "排序", ["默认", "按数值降序", "按数值升序"])],
     mpl=B.mpl_bar, ply=B.ply_bar, ins=B.ins_bar, fill=_f_bar)

_reg("box", "basic", "箱线图", "按组展示中位数/四分位/离群点",
     [p_col("y", "数值", NUM, True), p_col("x", "分组（可选）", ANYCAT), p_bool("notch", "凹口")],
     mpl=B.mpl_box, ply=B.ply_box, ins=B.ins_box, fill=_f_box)

_reg("violin", "basic", "小提琴图", "分布形状 + 箱线信息（每组≥3个值）",
     [p_col("y", "数值", NUM, True), p_col("x", "分组（可选）", ANYCAT)],
     mpl=B.mpl_violin, ply=B.ply_violin, ins=B.ins_box, fill=_f_box)

_reg("histogram", "basic", "直方图", "单列数值分布，可按类别叠加概率密度",
     [p_col("x", "数值", NUM, True), p_num("bins", "分箱数", 30), p_col("hue", "叠加分组（≤8类）", ANYCAT)],
     mpl=B.mpl_histogram, ply=B.ply_histogram, ins=B.ins_histogram, fill=_f_hist)

_reg("kde2d", "basic", "二维密度", "两列数值的核密度等高线 + 原始点",
     [p_col("x", "X 轴", NUM, True), p_col("y", "Y 轴", NUM, True),
      p_num("bins", "网格数", 90), p_bool("filled", "填充等高线", True), p_bool("points", "叠加数据点", True)],
     mpl=B.mpl_kde2d, ply=B.ply_kde2d, ins=B.ins_kde2d, fill=_f_kde2d)

_reg("corrheat", "basic", "相关性热图", "全部数值列两两相关（自动识别数值列）",
     [p_sel("method", "相关系数", ["pearson", "spearman"]), p_bool("annot", "标注数值", True)],
     mpl=B.mpl_corrheat, ply=B.ply_corrheat, ins=B.ins_corrheat, fill=_f_corrheat,
     sug=lambda p: (0.7, f"{_n_type(p, 'numeric')} 个数值列的总览") if _n_type(p, "numeric") >= 4 else None)

_reg("funnel", "structure", "能量漏斗图", "score vs RMSD 散点 + 分位桶前沿线 + 最优点标注",
     [p_col("x", "X（通常 RMSD）", NUM, True), p_col("y", "Y（通常 score）", NUM, True),
      p_col("group", "分组（多 decoy 群体）", ANYCAT)],
     mpl=BIO.mpl_funnel, ply=BIO.ply_funnel, ins=BIO.ins_funnel, fill=_f_funnel)

_reg("scorehist", "structure", "Score 分布", "多 decoy 群体的 score 概率密度阶梯叠加",
     [p_col("x", "Score", NUM, True), p_num("bins", "分箱数", 40),
      p_col("hue", "群体分组", ANYCAT)],
     mpl=BIO.mpl_scorehist, ply=BIO.ply_scorehist, ins=BIO.ins_scorehist, fill=_f_scorehist)

_reg("perres", "structure", "每残基能量分解", "能量项正负堆叠柱，标出合计最优残基",
     [p_col("x", "残基/位置", NUM + ["categorical"], True),
      p_multi("values", "能量项（可多选）", NUM),
      ],
     mpl=BIO.mpl_perres, ply=BIO.ply_perres, ins=BIO.ins_perres, fill=_f_perres)

_reg("ddgbar", "structure", "ΔΔG 条形图", "突变/残基的 ΔΔG 排序条形，红去稳定蓝稳定",
     [p_col("label", "突变标签", ANYCAT, True), p_col("value", "ΔΔG", NUM, True),
      p_num("cut", "阈值线", 2.0), p_num("top_n", "展示条数（|ΔΔG| 最大的前 N）", 60)],
     mpl=BIO.mpl_ddgbar, ply=BIO.ply_ddgbar, ins=BIO.ins_ddgbar, fill=_f_ddgbar)

_reg("matheat", "structure", "矩阵热图", "行索引 × 多数值列矩阵（ddG 矩阵/接触图/DCCM 通用）",
     [p_col("index", "行索引列", ANYCAT + NUM, True),
      p_multi("values", "数值列（可多选）", NUM),
      p_sel("cmap", "配色", ["发散 (RdBu_r)", "顺序 (YlOrRd)", "蓝 (Blues)", "Viridis"]),
      p_bool("annot", "标注数值（仅小矩阵）", False)],
     mpl=BIO.mpl_matheat, ply=BIO.ply_matheat, ins=BIO.ins_matheat, fill=_f_matheat)

_reg("iface", "structure", "界面能量散点", "ΔG vs 埋藏面积，中位线分象限评估界面质量",
     [p_col("x", "X（如 dSASA）", NUM, True), p_col("y", "Y（如 dG）", NUM, True)],
     mpl=BIO.mpl_iface, ply=BIO.ply_iface, ins=BIO.ins_iface, fill=_f_iface)

_reg("plddt", "structure", "pLDDT 曲线", "置信度沿序列分布，AF 风格四段着色（50/70/90 阈值）",
     [p_col("pos", "位置", NUM, True), p_col("val", "pLDDT/B-factor", NUM, True)],
     mpl=BIO.mpl_plddt, ply=BIO.ply_plddt, ins=BIO.ins_plddt, fill=_f_plddt)

_reg("tsband", "md", "时间序列 ± 带", "RMSD/RMSF/能量随帧变化，可叠 ±SD 阴影",
     [p_col("t", "时间/帧", NUM, True), p_col("mean", "数值", NUM, True),
      p_col("err", "SD/SEM（可选）", NUM)],
     mpl=BIO.mpl_tsband, ply=BIO.ply_tsband, ins=BIO.ins_tsband, fill=_f_tsband)

_reg("felandscape", "md", "自由能面", "两坐标（如 RMSD vs Rg）直方图 → -kT·lnP 等高线",
     [p_col("x", "X 坐标", NUM, True), p_col("y", "Y 坐标", NUM, True),
      p_num("bins", "分箱数", 50), p_num("temp", "温度 (K)", 298.15),
      p_num("fmax", "ΔG 截断 (kcal/mol)", 10)],
     mpl=BIO.mpl_felandscape, ply=BIO.ply_felandscape, ins=BIO.ins_felandscape, fill=_f_felandscape)

_reg("logo", "seq", "序列 Logo", "多序列比对的信息量 logo（化学分类着色）",
     [p_col("seq", "序列列", SEQ + ["text"], True)],
     mpl=BIO.mpl_logo, ply=BIO.ply_logo, ins=BIO.ins_logo, fill=_f_seq)

_reg("entropy", "seq", "保守性曲线", "逐位置保守度（bits），标注最保守位",
     [p_col("seq", "序列列", SEQ + ["text"], True)],
     mpl=BIO.mpl_entropy, ply=BIO.ply_entropy, ins=BIO.ins_entropy, fill=_f_seq)

_reg("aacomp", "seq", "氨基酸组成", "全部序列的 AA 频率排序，按化学分类着色",
     [p_col("seq", "序列列", SEQ + ["text"], True)],
     mpl=BIO.mpl_aacomp, ply=BIO.ply_aacomp, ins=BIO.ins_aacomp, fill=_f_seq)

_reg("hydro", "seq", "疏水性曲线", "Kyte-Doolittle 滑窗疏水指数（标跨膜阈值）",
     [p_col("seq", "序列列", SEQ + ["text"], True), p_num("window", "滑窗宽度（奇数）", 9)],
     mpl=BIO.mpl_hydro, ply=BIO.ply_hydro, ins=BIO.ins_hydro, fill=_f_hydro)

_reg("volc", "omics", "火山图", "效应量 vs -log10(p)，阈值线 + Top 显著点标签",
     [p_col("effect", "效应量 (log2FC/ΔΔG)", NUM, True), p_col("pval", "p 值", NUM, True),
      p_num("effect_cut", "效应阈值", 1.0), p_num("p_cut", "p 阈值", 0.05),
      p_num("label_top", "标签数量", 8), p_col("label_col", "标签列（基因名等）", ANYCAT)],
     mpl=O.mpl_volcano, ply=O.ply_volcano, ins=O.ins_volcano, fill=_f_volc)

_reg("clustermap", "omics", "聚类热图", "行/列层次聚类 + z-score 标准化热图",
     [p_col("index", "行索引列", ANYCAT + NUM, True),
      p_multi("values", "数值列", NUM),
      p_sel("zscore", "标准化", ["无", "按行", "按列"])],
     mpl=O.mpl_clustermap, ply=O.ply_clustermap, ins=O.ins_clustermap, fill=_f_clustermap)

_reg("pca", "omics", "PCA 主成分", "数值列自动降维，可按组着色 + 载荷箭头",
     [p_col("group", "分组（可选）", ANYCAT), p_bool("standardize", "标准化", True),
      p_bool("loadings", "叠加载荷箭头（PNG）", False)],
     mpl=O.mpl_pca, ply=O.ply_pca, ins=O.ins_pca, fill=_f_pca)

_reg("enrichdot", "omics", "富集气泡图", "通路气泡：x=富集倍率，y=-log10 p，大小=基因数",
     [p_col("term", "通路/条目", ANYCAT, True), p_col("pval", "p 值", NUM, True),
      p_col("x", "X（倍率/比例，可选）", NUM), p_col("size", "气泡大小（数量）", NUM),
      p_num("p_cut", "p 阈值", 0.05)],
     mpl=O.mpl_enrichdot, ply=O.ply_enrichdot, ins=O.ins_enrichdot, fill=_f_enrichdot)

_reg("enrichbar", "omics", "富集条形图", "Top N 通路按 -log10(p) 横向条形",
     [p_col("term", "通路/条目", ANYCAT, True), p_col("pval", "p 值", NUM, True),
      p_num("top_n", "展示条数", 20), p_col("count", "基因数（可选标注）", NUM),
      p_num("p_cut", "p 阈值", 0.05)],
     mpl=O.mpl_enrichbar, ply=O.ply_enrichbar, ins=O.ins_enrichbar, fill=_f_enrichbar)

_reg("gsea", "omics", "GSEA 富集曲线", "按分数排序的 running enrichment score + 命中位",
     [p_col("score", "排序分数", NUM, True), p_col("inset", "集合成员 (1/0)", NUM, True)],
     mpl=O.mpl_gsea, ply=O.ply_gsea, ins=O.ins_gsea, fill=_f_gsea)

_reg("maplot", "omics", "MA 图", "两组信号的 A-M 散点 + 分箱趋势线",
     [p_col("a", "组 A 数值", NUM, True), p_col("b", "组 B 数值", NUM, True),
      p_num("cut", "log2M 阈值", 1.0)],
     mpl=O.mpl_maplot, ply=O.ply_maplot, ins=O.ins_maplot, fill=_f_maplot)

_reg("manhattan", "omics", "曼哈顿图", "染色体位置 vs -log10(p)，交替染色体着色 + 显著线",
     [p_col("chrom", "染色体", ANYCAT, True), p_col("pos", "位置", NUM, True),
      p_col("pval", "p 值", NUM, True), p_num("p_cut", "显著阈值", 5e-8),
      p_col("label_col", "标签列（rsID/基因）", ANYCAT)],
     mpl=O.mpl_manhattan, ply=O.ply_manhattan, ins=O.ins_manhattan, fill=_f_manh)

_reg("traincurve", "ml", "训练曲线", "多个 loss/指标曲线对比，可对数轴，末段回升提示过拟合",
     [p_col("x", "Epoch/Step", NUM, True), p_multi("values", "指标列（可多选）", NUM),
      p_bool("logy", "对数 Y 轴", False)],
     mpl=O.mpl_traincurve, ply=O.ply_traincurve, ins=O.ins_traincurve, fill=_f_traincurve)

_reg("roc", "ml", "ROC 曲线", "多模型 ROC + AUC（正类自动识别）",
     [p_col("ytrue", "真实标签", ANYCAT + NUM, True), p_multi("scores", "预测分数列（可多选）", NUM)],
     mpl=O.mpl_roc, ply=O.ply_roc, ins=O.ins_roc, fill=_f_scores)

_reg("prc", "ml", "PR 曲线", "精确率-召回率曲线 + AP（不平衡数据更灵敏）",
     [p_col("ytrue", "真实标签", ANYCAT + NUM, True), p_multi("scores", "预测分数列（可多选）", NUM)],
     mpl=O.mpl_prc, ply=O.ply_prc, ins=O.ins_prc, fill=_f_scores)

_reg("cm", "ml", "混淆矩阵", "真实 × 预测计数 + 行占比标注",
     [p_col("ytrue", "真实标签", ANYCAT, True), p_col("ypred", "预测标签", ANYCAT, True)],
     mpl=O.mpl_cm, ply=O.ply_cm, ins=O.ins_cm, fill=_f_cm)

_reg("network", "net", "网络图", "边表 → 力导向布局，节点大小/颜色=连接度，hub 自动标签",
     [p_col("src", "来源节点", ANYCAT, True), p_col("dst", "目标节点", ANYCAT, True),
      p_col("weight", "边权重（可选）", NUM), p_num("top_n", "最大节点数", 120),
      p_num("label_top", "标签节点数", 14)],
     mpl=O.mpl_network, ply=O.ply_network, ins=O.ins_network, fill=_f_net)

_reg("sankey", "net", "桑基图", "source → target 流量分股（按权重聚合取 Top N）",
     [p_col("src", "来源", ANYCAT, True), p_col("dst", "去向", ANYCAT, True),
      p_col("value", "流量值（可选）", NUM), p_num("top_n", "最大流量数", 25)],
     mpl=O.mpl_sankey, ply=O.ply_sankey, ins=O.ins_sankey, fill=_f_sankey)


# ---------- 渲染调度 ----------
def plot_meta():
    out = []
    for cat_id, cat_name in CATS:
        out.append({"cat": cat_id, "cat_name": cat_name,
                    "plots": [{"id": t["id"], "name": t["name"], "desc": t["desc"],
                               "params": [dict(p) for p in t["params"]],
                               "png_ok": bool(t["mpl"]), "html_ok": bool(t["ply"])}
                              for t in PLOTS.values() if t["cat"] == cat_id]})
    return out


def autofill(profile, pid, params):
    """合并用户参数与自动填参默认值；返回清洗后的参数 dict。"""
    pdef = PLOTS[pid]
    filled = dict(pdef["fill"](profile)) if pdef["fill"] else {}
    # 通用兜底：col 参数缺失时按 accept 取第一个
    for schema in pdef["params"]:
        key = schema["key"]
        if filled.get(key) in (None, [], ""):
            if schema["kind"] == "col" and schema.get("accept"):
                filled[key] = first_of_type(profile, schema["accept"])
            elif schema["kind"] == "number" and schema.get("default") is not None:
                filled[key] = schema["default"]
            elif schema["kind"] == "bool":
                filled[key] = schema.get("default", False)
            elif schema["kind"] == "select" and schema.get("default") is not None:
                filled[key] = schema["default"]
    P = dict(filled)
    P.update({k: v for k, v in (params or {}).items() if v not in (None, "", [])})
    # 类型清洗
    valid_keys = {s["key"] for s in pdef["params"]} | {"_title"}
    P = {k: v for k, v in P.items() if k in valid_keys}
    for schema in pdef["params"]:
        key = schema["key"]
        if key not in P:
            continue
        v = P[key]
        if schema["kind"] == "number":
            try:
                P[key] = float(v) if not float(v).is_integer() or key == "top_n" else int(float(v))
            except (TypeError, ValueError):
                P.pop(key)
        if schema["kind"] in ("col",) and isinstance(v, (list, tuple)):
            P[key] = v[0] if v else None
        if schema["kind"] == "cols_multi":
            if isinstance(v, str):
                P[key] = [v]
            P[key] = [x for x in (P.get(key) or []) if x in colnames(profile)]
    names = set(colnames(profile))
    for schema in pdef["params"]:
        if schema["kind"] == "col" and P.get(schema["key"]) is not None and P[schema["key"]] not in names:
            P.pop(schema["key"], None)
    if P.get("_title"):
        P["_title"] = str(P["_title"])[:80]
    return P


def render_plot(df, profile, pid, params, embed_js=False):
    """返回 (png_bytes|None, html_str|None, insight, warnings, P)。"""
    import io
    import matplotlib
    matplotlib.use("Agg")
    import common as c

    pdef = PLOTS.get(pid)
    if pdef is None:
        raise KeyError(f"未知图类型 {pid}")
    P = autofill(profile, pid, params)
    warnings = []
    # 必填列检查
    for schema in pdef["params"]:
        if schema.get("required") and not P.get(schema["key"]):
            raise ValueError(f"缺少必填参数「{schema['label']}」")
    for schema in pdef["params"]:
        if schema["kind"] == "cols_multi" and schema["key"] in ("values", "scores") and not P.get(schema["key"]):
            warnings.append(f"未选择「{schema['label']}」，图可能为空")

    png_bytes = None
    if pdef["mpl"]:
        import matplotlib.pyplot as plt
        fig = pdef["mpl"](df, P)
        buf = io.BytesIO()
        fig.tight_layout()
        fig.savefig(buf, dpi=c.SAVE_DPI, bbox_inches="tight")
        plt.close(fig)
        png_bytes = buf.getvalue()

    html_str = None
    if pdef["ply"]:
        figp = pdef["ply"](df, P)
        title = P.get("_title") or pdef["name"]
        html_str = c.wrap_html(figp, title, embed_js=embed_js)

    insight = ""
    if pdef["ins"]:
        try:
            insight = pdef["ins"](df, P)
        except Exception:
            insight = ""
    return png_bytes, html_str, insight, warnings, P
