"""组学差异、机器学习、网络与通路渲染器（14 种），mpl + plotly 双实现。"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import plotly.graph_objects as go

import common as c
from renderers_basic import ply_style, mpl_title, _titled


# ================= 火山图 =================
def _volc_xy(df, P):
    x = c.num(df[P["effect"]]).to_numpy(float)
    p = c.num(df[P["pval"]]).to_numpy(float)
    ok = np.isfinite(x) & np.isfinite(p)
    p = np.clip(p[ok], 1e-300, 1)
    return x[ok], -np.log10(p), ok.sum()


def _volc_masks(x, y, P):
    ec = float(P.get("effect_cut") or 1.0)
    pc = float(P.get("p_cut") or 0.05)
    ycut = -np.log10(pc)
    up = (x > ec) & (y > ycut)
    dn = (x < -ec) & (y > ycut)
    return up, dn, ycut


def mpl_volcano(df, P):
    x, y, _ = _volc_xy(df, P)
    up, dn, ycut = _volc_masks(x, y, P)
    fig, ax = c.prep_fig()
    ax.scatter(x[~(up | dn)], y[~(up | dn)], s=16, c="#C3CCDB", alpha=0.8, lw=0)
    ax.scatter(x[up], y[up], s=22, c="#C62828", alpha=0.85, lw=0, label=f"上调 {int(up.sum())}")
    ax.scatter(x[dn], y[dn], s=22, c="#2E6FB7", alpha=0.85, lw=0, label=f"下调 {int(dn.sum())}")
    ec = float(P.get("effect_cut") or 1.0)
    ax.axhline(ycut, color="#94A3B8", ls="--", lw=1)
    for v in (-ec, ec):
        ax.axvline(v, color="#94A3B8", ls="--", lw=1)
    lab_col = P.get("label_col")
    n_top = int(P.get("label_top") or 8)
    idx = np.argsort(-y)[:n_top]
    for k, i in enumerate(idx):
        name = c.short(df[lab_col].iloc[i], 16) if lab_col and lab_col in df.columns else f"#{i}"
        ax.annotate(name, (x[i], y[i]), textcoords="offset points",
                    xytext=(6, 5 if k % 2 == 0 else -11), fontsize=8, color="#1F2937")
    ax.legend(loc="best")
    mpl_title(ax, _titled(df, P, "火山图"), P["effect"], f"-log10({P['pval']})")
    return fig


def ply_volcano(df, P):
    x, y, _ = _volc_xy(df, P)
    up, dn, ycut = _volc_masks(x, y, P)
    lab_col = P.get("label_col")
    names = df[lab_col].astype(str).to_numpy() if lab_col and lab_col in df.columns else np.array([""] * len(x))
    fig = go.Figure()
    base = ~(up | dn)
    fig.add_trace(go.Scatter(x=x[base], y=y[base], mode="markers", name="不显著",
                             marker=dict(size=5, color="#C3CCDB"),
                             hovertemplate="%{text}<br>x=%{x:.2f}, -log10p=%{y:.2f}<extra></extra>",
                             text=names[base]))
    for m, colr, nm in ((up, "#C62828", "上调"), (dn, "#2E6FB7", "下调")):
        fig.add_trace(go.Scatter(x=x[m], y=y[m], mode="markers", name=f"{nm} {int(m.sum())}",
                                 marker=dict(size=7, color=colr), text=names[m],
                                 hovertemplate="%{text}<br>x=%{x:.2f}, -log10p=%{y:.2f}<extra></extra>"))
    ec = float(P.get("effect_cut") or 1.0)
    fig.add_hline(y=ycut, line=dict(color="#94A3B8", dash="dash", width=1))
    for v in (-ec, ec):
        fig.add_vline(x=v, line=dict(color="#94A3B8", dash="dash", width=1))
    return ply_style(fig, _titled(df, P, "火山图"), P["effect"], f"-log10({P['pval']})")


def ins_volcano(df, P):
    x, y, n = _volc_xy(df, P)
    up, dn, _ = _volc_masks(x, y, P)
    lab = P.get("label_col")
    s = f"n={n}；显著上调 {int(up.sum())} 个、下调 {int(dn.sum())} 个（|x|>{P.get('effect_cut') or 1} 且 {P['pval']}<{P.get('p_cut') or 0.05}）。"
    if lab and lab in df.columns:
        idx = np.argsort(-y)[:3]
        s += " 最显著：" + "、".join(str(c.short(df[lab].iloc[i], 14)) for i in idx) + "。"
    return s


# ================= 聚类热图 =================
def _clust_mat(df, P):
    values = list(P.get("values") or [])
    if not values:
        raise ValueError("请在参数中选择至少一个数值列")
    idx = df[P["index"]].astype(str).to_numpy()
    mat = df[values].apply(c.num).to_numpy(float)
    ok_rows = np.isfinite(mat).mean(axis=1) > 0.5
    mat = mat[ok_rows]; idx = idx[ok_rows]
    if len(idx) > 500:
        raise ValueError(f"行数过多（{len(idx)} 行）。聚类热图最多 500 行，请先筛选目标行（如 Top 变化倍数）")
    colmean = np.nanmean(mat, axis=0)
    mat = np.where(np.isfinite(mat), mat, np.broadcast_to(colmean, mat.shape))
    z = P.get("zscore") or "无"
    if z == "按行":
        sd = mat.std(axis=1, keepdims=True); sd[sd == 0] = 1
        mat = (mat - mat.mean(axis=1, keepdims=True)) / sd
    elif z == "按列":
        sd = mat.std(axis=0, keepdims=True); sd[sd == 0] = 1
        mat = (mat - mat.mean(axis=0, keepdims=True)) / sd
    return idx, np.array(values, dtype=object), mat


def _leaf_orders(mat):
    from scipy.cluster.hierarchy import linkage, dendrogram
    rl = dendrogram(linkage(mat, method="average"), no_plot=True)["leaves"]
    cl = dendrogram(linkage(mat.T, method="average"), no_plot=True)["leaves"]
    return rl, cl


def mpl_clustermap(df, P):
    idx, cols, mat = _clust_mat(df, P)
    if len(idx) < 3 or len(cols) < 2:
        raise ValueError("行列数不足，无法聚类")
    rl, cl = _leaf_orders(mat)
    mat, idx, cols = mat[rl], idx[rl], cols[cl]
    fig = plt.figure(figsize=(min(13, 3 + len(cols) * 0.6), min(14, 3.5 + len(idx) * 0.22)))
    gs = fig.add_gridspec(2, 2, width_ratios=[1, 5], height_ratios=[1, 5], wspace=0.01, hspace=0.01)
    from scipy.cluster.hierarchy import dendrogram as _dend, linkage as _link
    axd = fig.add_subplot(gs[0, 1]); axl = fig.add_subplot(gs[1, 0]); axm = fig.add_subplot(gs[1, 1])
    _dend(_link(mat, method="average"), no_labels=True, color_threshold=0, above_threshold_color="#94A3B8", ax=axd)
    _dend(_link(mat.T, method="average"), no_labels=True, color_threshold=0, above_threshold_color="#94A3B8", ax=axl, orientation="left")
    z = P.get("zscore") or "无"
    cmap = "RdBu_r" if (z != "无" or (P.get("cmap") or "").startswith("发散")) else "YlOrRd"
    arr = mat
    if cmap == "RdBu_r":
        vm = np.nanmax(np.abs(arr)) or 1
        im = axm.imshow(arr, aspect="auto", cmap=cmap, vmin=-vm, vmax=vm)
    else:
        im = axm.imshow(arr, aspect="auto", cmap=cmap)
    axm.set_xticks(range(len(cols)), [c.short(v, 12) for v in cols], rotation=45, ha="right", fontsize=8)
    step = max(1, len(idx) // 50)
    axm.set_yticks(np.arange(0, len(idx), step), [c.short(v, 14) for v in idx[::step]], fontsize=7)
    axm.grid(False); axd.axis("off"); axl.axis("off")
    fig.colorbar(im, ax=axd, pad=0.02, shrink=1.4, label=z if z != "无" else "原始值")
    axm.set_title(_titled(df, P, "聚类热图（平均连锁）"), pad=14)
    return fig


def ply_clustermap(df, P):
    idx, cols, mat = _clust_mat(df, P)
    if len(idx) < 3 or len(cols) < 2:
        raise ValueError("行列数不足，无法聚类")
    rl, cl = _leaf_orders(mat)
    mat, idx, cols = mat[rl], idx[rl], cols[cl]
    z = P.get("zscore") or "无"
    fig = go.Figure(go.Heatmap(z=mat, x=[c.short(v, 14) for v in cols], y=[c.short(v, 18) for v in idx],
                               colorscale="RdBu", reversescale=True,
                               zmid=0 if z != "无" else None,
                               colorbar=dict(thickness=13),
                               hovertemplate="%{y} × %{x}<br>%{z:.3f}<extra></extra>"))
    return ply_style(fig, _titled(df, P, "聚类热图（树状图见 PNG 版）"), legend=False)


def ins_clustermap(df, P):
    idx, cols, mat = _clust_mat(df, P)
    z = P.get("zscore") or "无"
    return f"矩阵 {len(idx)} 行 × {len(cols)} 列，行/列按平均连锁层次聚类重排；标准化：{z}。同色聚集块提示表达/能量模式相似。"


# ================= PCA =================
def _pca_calc(df, P):
    nums = df.select_dtypes(include=[np.number])
    nums = nums.loc[:, nums.apply(lambda s: s.notna().mean() > 0.5 and s.nunique() > 1, axis=0)]
    if nums.shape[1] < 2:
        raise ValueError("有效数值列不足 2（PCA 需要至少两列非全空、非常数数值列）")
    X = nums.to_numpy(float)
    colmean = np.nanmean(X, axis=0)
    X = np.where(np.isfinite(X), X, colmean[None, :])
    if P.get("standardize", True):
        sd = X.std(axis=0); sd[sd == 0] = 1
        X = (X - X.mean(axis=0)) / sd
    else:
        X = X - X.mean(axis=0)
    U, S, Vt = np.linalg.svd(X, full_matrices=False)
    scores = U[:, :2] * S[:2]
    ev = S ** 2 / max((S ** 2).sum(), 1e-12)
    return scores, ev, list(nums.columns), Vt


def mpl_pca(df, P):
    scores, ev, feats, Vt = _pca_calc(df, P)
    fig, ax = c.prep_fig()
    gcol = P.get("group")
    if gcol and gcol in df.columns:
        g = df[gcol].astype(str)
        for v, colr in c.cat_colors(g.dropna().unique()).items():
            m = (g == v).to_numpy()
            ax.scatter(scores[m, 0], scores[m, 1], s=26, alpha=0.75, color=colr, label=c.short(v, 18), edgecolors="white", lw=0.4)
        ax.legend(title=str(gcol), title_fontsize=9)
    else:
        ax.scatter(scores[:, 0], scores[:, 1], s=26, alpha=0.75, color="#4C6FE8", edgecolors="white", lw=0.4)
    if P.get("loadings"):
        k = min(6, len(feats))
        order = np.argsort(-(Vt[0] ** 2 + Vt[1] ** 2))[:k]
        span_x = np.ptp(scores[:, 0]) or 1; span_y = np.ptp(scores[:, 1]) or 1
        for i in order:
            ax.annotate("", xy=(Vt[0, i] * span_x * 0.42, Vt[1, i] * span_y * 0.42), xytext=(0, 0),
                        arrowprops=dict(arrowstyle="->", color="#E8743C", lw=1.4))
            ax.text(Vt[0, i] * span_x * 0.46, Vt[1, i] * span_y * 0.46, c.short(feats[i], 14),
                    fontsize=8, color="#B45309")
    mpl_title(ax, _titled(df, P, "PCA 主成分分析"), f"PC1 ({ev[0] * 100:.1f}%)", f"PC2 ({ev[1] * 100:.1f}%)")
    return fig


def ply_pca(df, P):
    scores, ev, feats, Vt = _pca_calc(df, P)
    fig = go.Figure()
    gcol = P.get("group")
    if gcol and gcol in df.columns:
        g = df[gcol].astype(str)
        for v, colr in c.cat_colors(g.dropna().unique()).items():
            m = (g == v).to_numpy()
            fig.add_trace(go.Scatter(x=scores[m, 0], y=scores[m, 1], mode="markers", name=c.short(v, 18),
                                     marker=dict(size=7, color=colr, line=dict(width=0.5, color="white"))))
    else:
        fig.add_trace(go.Scatter(x=scores[:, 0], y=scores[:, 1], mode="markers", name="样本",
                                 marker=dict(size=7, color="#4C6FE8", line=dict(width=0.5, color="white"))))
    return ply_style(fig, _titled(df, P, "PCA 主成分分析"),
                     f"PC1 ({ev[0] * 100:.1f}%)", f"PC2 ({ev[1] * 100:.1f}%)")


def ins_pca(df, P):
    scores, ev, feats, Vt = _pca_calc(df, P)
    s = f"{len(feats)} 个数值变量 → 前 2 主成分累计解释 {ev[:2].sum() * 100:.1f}%（标准化={'是' if P.get('standardize', True) else '否'}）。"
    if P.get("group") and P.get("group") in df.columns:
        g = df[P["group"]].astype(str)
        parts = [f"{c.short(v, 12)}(n={int((g == v).sum())})" for v in pd.unique(g.dropna())[:5]]
        s += " 分组：" + "、".join(parts) + "；组间在 PC 平面上分离说明该分组与整体变异模式一致。"
    return s


# ================= 富集气泡图 =================
def _enrich_xy(df, P):
    y = -np.log10(np.clip(c.num(df[P["pval"]]).to_numpy(float), 1e-300, 1))
    x = c.num(df[P["x"]]).to_numpy(float) if P.get("x") else y.copy()
    ok = np.isfinite(x) & np.isfinite(y)
    return x[ok], y[ok], ok


def mpl_enrichdot(df, P):
    x, y, ok = _enrich_xy(df, P)
    s = c.size_scale(df[P["size"]], 18, 320)[ok] if P.get("size") else np.full(ok.sum(), 60)
    fig, ax = c.prep_fig()
    sc = ax.scatter(x, y, s=np.asarray(s), c=y, cmap="viridis", alpha=0.8, edgecolors="white", lw=0.6)
    fig.colorbar(sc, ax=ax, pad=0.01, label=f"-log10({P['pval']})")
    ycut = -np.log10(float(P.get("p_cut") or 0.05))
    ax.axhline(ycut, color="#94A3B8", ls="--", lw=1)
    idx = np.argsort(-y)[:min(6, len(y))]
    terms = df[P["term"]].astype(str).to_numpy()[ok]
    for k, i in enumerate(idx):
        ax.annotate(c.short(terms[i], 22), (x[i], y[i]), textcoords="offset points",
                    xytext=(7, 4 if k % 2 == 0 else -12), fontsize=8)
    mpl_title(ax, _titled(df, P, "富集气泡图"), P.get("x") or f"-log10({P['pval']})", f"-log10({P['pval']})")
    return fig


def ply_enrichdot(df, P):
    x, y, ok = _enrich_xy(df, P)
    terms = df[P["term"]].astype(str).to_numpy()[ok]
    s_raw = c.num(df[P["size"]]).to_numpy(float)[ok] if P.get("size") else np.ones(ok.sum())
    sizes = c.size_scale(s_raw, 6, 34)
    fig = go.Figure(go.Scatter(x=x, y=y, mode="markers", text=terms, customdata=s_raw,
                               marker=dict(size=sizes, color=y, colorscale="Viridis", showscale=True,
                                           colorbar=dict(title="-log10 p", thickness=13),
                                           line=dict(width=0.7, color="white")),
                               hovertemplate="%{text}<br>x=%{x:.3g}<br>-log10p=%{y:.2f}<br>数量=%{customdata:.0f}<extra></extra>"))
    fig.add_hline(y=-np.log10(float(P.get("p_cut") or 0.05)), line=dict(color="#94A3B8", dash="dash", width=1))
    return ply_style(fig, _titled(df, P, "富集气泡图"), P.get("x") or f"-log10({P['pval']})", f"-log10({P['pval']})", legend=False)


def ins_enrichdot(df, P):
    x, y, ok = _enrich_xy(df, P)
    terms = df[P["term"]].astype(str).to_numpy()[ok]
    i = int(np.argmax(y))
    ycut = -np.log10(float(P.get("p_cut") or 0.05))
    nsig = int((y > ycut).sum())
    return f"n={len(y)}；显著通路 {nsig} 个。最显著：{c.short(terms[i], 26)}（-log10 p = {y[i]:.2f}）。气泡越大代表基因数越多。"


# ================= 富集条形图 =================
def mpl_enrichbar(df, P):
    y = -np.log10(np.clip(c.num(df[P["pval"]]).to_numpy(float), 1e-300, 1))
    terms = df[P["term"]].astype(str).to_numpy()
    ok = np.isfinite(y)
    y, terms = y[ok], terms[ok]
    topn = int(P.get("top_n") or 20)
    order = np.argsort(y)[::-1][:topn]
    y, terms = y[order][::-1], terms[order][::-1]
    fig_h = min(max(4.2, len(y) * 0.34), 15)
    fig, ax = plt.subplots(figsize=(8.8, fig_h))
    cnt = c.num(df[P.get("count")]).to_numpy(float)[ok][order][::-1] if P.get("count") else None
    norm = plt.Normalize(np.nanmin(y), np.nanmax(y) or 1)
    ax.barh(range(len(y)), y, color=plt.get_cmap("viridis")(norm(y)), height=0.72)
    ax.set_yticks(range(len(y)), [c.short(t, 34) for t in terms], fontsize=8.5)
    if cnt is not None:
        for i, (v, cn) in enumerate(zip(y, cnt)):
            if np.isfinite(cn):
                ax.text(v + 0.05, i, f"{cn:.0f} genes", va="center", fontsize=7.5, color="#64748B")
    ycut = -np.log10(float(P.get("p_cut") or 0.05))
    ax.axvline(ycut, color="#94A3B8", ls="--", lw=1)
    ax.set_xlabel(f"-log10({P['pval']})")
    ax.set_title(_titled(df, P, f"富集条形图（Top {len(y)}）"), pad=12)
    return fig


def ply_enrichbar(df, P):
    y = -np.log10(np.clip(c.num(df[P["pval"]]).to_numpy(float), 1e-300, 1))
    terms = df[P["term"]].astype(str).to_numpy()
    ok = np.isfinite(y)
    y, terms = y[ok], terms[ok]
    topn = int(P.get("top_n") or 20)
    order = np.argsort(y)[::-1][:topn]
    y, terms = y[order][::-1], terms[order][::-1]
    norm = lambda arr: (arr - min(y)) / max((max(y) - min(y)) or 1, 1e-9)
    viridis = ["rgb(%d,%d,%d)" % tuple(int(v * 255) for v in plt.get_cmap("viridis")(norm(np.array([v])))[0][:3]) for v in y]
    fig = go.Figure(go.Bar(x=y, y=[c.short(t, 40) for t in terms], orientation="h",
                           marker=dict(color=y, colorscale="Viridis"),
                           hovertemplate="%{y}<br>-log10p = %{x:.2f}<extra></extra>"))
    return ply_style(fig, _titled(df, P, f"富集条形图（Top {len(y)}）"), f"-log10({P['pval']})", None, legend=False)


def ins_enrichbar(df, P):
    y = -np.log10(np.clip(c.num(df[P["pval"]]).to_numpy(float), 1e-300, 1))
    terms = df[P["term"]].astype(str).to_numpy()
    ok = np.isfinite(y)
    y, terms = y[ok], terms[ok]
    i = int(np.argmax(y))
    return f"n={len(y)} 条目。最显著：{c.short(terms[i], 30)}（-log10 p = {y[i]:.2f}）；条形颜色与长度均对应显著性。"


# ================= GSEA 富集分数曲线 =================
def _gsea_run(df, P):
    score = c.num(df[P["score"]]).to_numpy(float)
    inset = c.num(df[P["inset"]]).to_numpy(float)
    ok = np.isfinite(score) & np.isfinite(inset)
    score, inset = score[ok], (inset[ok] > 0).astype(float)
    order = np.argsort(-score)
    score, hit = score[order], inset[order]
    w = np.abs(score)
    run = np.where(hit > 0, w, 0.0)
    csum = np.cumsum(run)
    denom_hit = csum[-1] or 1.0
    denom_miss = max((np.arange(1, len(hit) + 1)[hit == 0]).size, 1)
    es = np.where(hit > 0,
                  csum / denom_hit,
                  -np.cumsum(np.ones(len(hit))) / max((~(hit > 0)).sum(), 1))
    i_peak = int(np.argmax(np.abs(es)))
    return score, hit, es, i_peak


def mpl_gsea(df, P):
    score, hit, es, i_peak = _gsea_run(df, P)
    fig, ax = plt.subplots(2, 1, figsize=(9.2, 5.2), height_ratios=[3, 1], sharex=True)
    x = np.arange(1, len(es) + 1)
    ax[0].plot(x, es, color="#4C6FE8", lw=2)
    ax[0].fill_between(x, es, 0, where=es >= 0, color="#4C6FE8", alpha=0.18, interpolate=True)
    ax[0].fill_between(x, es, 0, where=es < 0, color="#E8743C", alpha=0.18, interpolate=True)
    ax[0].axhline(0, color="#64748B", lw=1)
    ax[0].plot(x[i_peak], es[i_peak], "o", ms=7, color="#E8546D", zorder=5)
    ax[0].annotate(f"ES = {es[i_peak]:.3f}\n峰位 #{x[i_peak]}", (x[i_peak], es[i_peak]),
                   textcoords="offset points", xytext=(10, 6), fontsize=9.5)
    ax[0].set_ylabel("Running ES")
    ax[1].vlines(x[hit > 0], 0, 1, color="#1F2937", lw=0.8)
    ax[1].set_ylabel("Hits"); ax[1].set_yticks([])
    ax[1].set_xlabel("基因排序（按分数降序）")
    fig.suptitle(_titled(df, P, "GSEA 富集分数曲线"), y=0.98, fontsize=13)
    fig.tight_layout()
    return fig


def ply_gsea(df, P):
    score, hit, es, i_peak = _gsea_run(df, P)
    x = np.arange(1, len(es) + 1)
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=x, y=es, mode="lines", name="Running ES",
                             line=dict(color="#4C6FE8", width=2.4),
                             hovertemplate="Rank %{x}<br>ES %{y:.3f}<extra></extra>"))
    fig.add_trace(go.Scatter(x=x[hit > 0], y=[es.min() * 0.04] * int((hit > 0).sum()), mode="markers",
                             name=f"集合基因 ({int((hit > 0).sum())})",
                             marker=dict(symbol="line-ns-open", size=9, color="#1F2937", line=dict(width=1))))
    fig.add_annotation(x=x[i_peak], y=es[i_peak], text=f"ES = {es[i_peak]:.3f}", showarrow=True, arrowhead=2)
    fig.add_hline(y=0, line=dict(color="#64748B", width=1))
    return ply_style(fig, _titled(df, P, "GSEA 富集分数曲线"), "基因排序", "Running ES")


def ins_gsea(df, P):
    score, hit, es, i_peak = _gsea_run(df, P)
    n_hit = int((hit > 0).sum())
    frac = i_peak / max(len(es), 1) * 100
    s = f"共 {len(es)} 个、集合成员 {n_hit} 个。ES = {es[i_peak]:.3f}，峰出现在第 {i_peak + 1} 位（前 {frac:.0f}%）。"
    s += " 峰靠前且为正 → 集合基因显著富集在排序高分端。" if es[i_peak] > 0 and frac < 40 else (
        " 峰靠后且为负 → 集合基因富集在低分端。" if es[i_peak] < 0 else " 分布较分散，富集不典型。")
    return s


# ================= MA 图 =================
def _ma_calc(df, P):
    a = c.num(df[P["a"]]).to_numpy(float)
    b = c.num(df[P["b"]]).to_numpy(float)
    ok = np.isfinite(a) & np.isfinite(b) & (a > 0) & (b > 0)
    A = (np.log2(a[ok]) + np.log2(b[ok])) / 2
    M = np.log2(a[ok]) - np.log2(b[ok])
    return A, M, ok


def mpl_maplot(df, P):
    A, M, ok = _ma_calc(df, P)
    cut = float(P.get("cut") or 1.0)
    fig, ax = c.prep_fig()
    sig = np.abs(M) > cut
    ax.scatter(A[~sig], M[~sig], s=14, c="#C3CCDB", alpha=0.8, lw=0, label="不显著")
    ax.scatter(A[sig & (M > 0)], M[sig & (M > 0)], s=18, c="#C62828", alpha=0.85, lw=0, label=f"上调 {int((sig & (M > 0)).sum())}")
    ax.scatter(A[sig & (M < 0)], M[sig & (M < 0)], s=18, c="#2E6FB7", alpha=0.85, lw=0, label=f"下调 {int((sig & (M < 0)).sum())}")
    # 分位数箱平滑线
    qs = np.quantile(A, np.linspace(0, 1, 21))
    mid = [(qs[i] + qs[i + 1]) / 2 for i in range(20)]
    med = [np.median(M[(A >= qs[i]) & (A <= qs[i + 1])]) for i in range(20)]
    ax.plot(mid, med, color="#1F2937", lw=1.8, label="趋势(分箱中位)")
    ax.axhline(0, color="#94A3B8", lw=1)
    for v in (cut, -cut):
        ax.axhline(v, color="#94A3B8", ls="--", lw=1)
    ax.legend(loc="best")
    mpl_title(ax, _titled(df, P, "MA 图"), f"A = (log2{P['a']} + log2{P['b']})/2", f"M = log2{P['a']}/{P['b']}")
    return fig


def ply_maplot(df, P):
    A, M, ok = _ma_calc(df, P)
    cut = float(P.get("cut") or 1.0)
    sig = np.abs(M) > cut
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=A[~sig], y=M[~sig], mode="markers", name="不显著",
                             marker=dict(size=5, color="#C3CCDB"),
                             hovertemplate="A=%{x:.2f}, M=%{y:.2f}<extra></extra>"))
    for m, colr, nm in ((sig & (M > 0), "#C62828", "上调"), (sig & (M < 0), "#2E6FB7", "下调")):
        fig.add_trace(go.Scatter(x=A[m], y=M[m], mode="markers", name=nm,
                                 marker=dict(size=7, color=colr),
                                 hovertemplate="A=%{x:.2f}, M=%{y:.2f}<extra></extra>"))
    fig.add_hline(y=0, line=dict(color="#94A3B8", width=1))
    for v in (cut, -cut):
        fig.add_hline(y=v, line=dict(color="#94A3B8", dash="dash", width=1))
    return ply_style(fig, _titled(df, P, "MA 图"),
                     f"A = (log2{P['a']} + log2{P['b']})/2", f"M = log2{P['a']}/{P['b']}")


def ins_maplot(df, P):
    A, M, ok = _ma_calc(df, P)
    cut = float(P.get("cut") or 1.0)
    up = int((M > cut).sum()); dn = int((M < -cut).sum())
    bias = np.median(M)
    s = f"n={len(M)}；|log2M| > {cut}：上调 {up}、下调 {dn}。"
    if abs(bias) > 0.15:
        s += f" 整体偏移 median M = {bias:.2f}，两组信号强度不均衡（建议先做归一化）。"
    else:
        s += " 整体围绕 M=0 对称，归一化质量尚可。"
    return s


# ================= 曼哈顿图 =================
def _manh_calc(df, P):
    chrom = df[P["chrom"]].astype(str)
    pos = c.num(df[P["pos"]]).to_numpy(float)
    p = np.clip(c.num(df[P["pval"]]).to_numpy(float), 1e-300, 1)
    ok = np.isfinite(pos) & np.isfinite(p)
    chrom, pos, p = chrom[ok], pos[ok], p[ok]
    def key(v):
        m = re.match(r"^(?:chr)?(\d+|[A-Za-z]+)$", v, re.I)
        if not m:
            return (2, v)
        t = m.group(1)
        return (0, int(t)) if t.isdigit() else (1, t.upper())
    order = sorted(pd.unique(chrom), key=key)
    xs, x0, span = [], 0.0, max(np.nanmax(pos), 1) / 100.0
    for ch in order:
        m = chrom == ch
        xs.append(x0 + pos[m] / span)
        x0 += np.nanmax(xs[-1]) if m.any() else 0
        x0 += np.nanmax(pos) / span * 0.4
    return chrom, pos, p, np.concatenate(xs), order


def mpl_manhattan(df, P):
    chrom, pos, p, x, order = _manh_calc(df, P)
    y = -np.log10(p)
    pc = float(P.get("p_cut") or 5e-8)
    fig, ax = c.prep_fig(11, 4.6)
    colors = ["#5A72B5", "#8FA3D0"]
    for i, ch in enumerate(order):
        m = chrom == ch
        ax.scatter(x[m], y[m], s=9, c=colors[i % 2], lw=0)
    sig = y > -np.log10(pc)
    ax.scatter(x[sig], y[sig], s=16, c="#C62828", lw=0)
    ax.axhline(-np.log10(pc), color="#E8834C", ls="--", lw=1.2)
    ax.text(x.max() * 0.99, -np.log10(pc) + 0.2, f"{P['pval']} = {pc:g}", ha="right", fontsize=8.5, color="#E8834C")
    lab_col = P.get("label_col")
    if lab_col and lab_col in df.columns:
        top = np.argsort(-y)[:min(6, len(y))]
        for i in top:
            ax.annotate(c.short(df[lab_col].iloc[i], 12), (x[i], y[i]), textcoords="offset points",
                        xytext=(5, 4), fontsize=8, color="#1F2937")
    ticks, tl = [], []
    cur = 0.0
    for ch in order:
        m = chrom == ch
        ticks.append(cur + (x[m].max() - cur) / 2 if m.any() else cur)
        tl.append(ch)
        cur = x[m].max() if m.any() else cur
    ax.set_xticks(ticks[::max(1, len(ticks) // 30)], tl[::max(1, len(ticks) // 30)], fontsize=8)
    ax.set_xlim(0, x.max() * 1.01)
    mpl_title(ax, _titled(df, P, "曼哈顿图"), "染色体", f"-log10({P['pval']})")
    return fig


def ply_manhattan(df, P):
    chrom, pos, p, x, order = _manh_calc(df, P)
    y = -np.log10(p)
    pc = float(P.get("p_cut") or 5e-8)
    fig = go.Figure()
    colors = ["#5A72B5", "#8FA3D0"]
    for i, ch in enumerate(order):
        m = chrom == ch
        if m.any():
            fig.add_trace(go.Scatter(x=x[m], y=y[m], mode="markers", name=str(ch),
                                     marker=dict(size=4.5, color=colors[i % 2]),
                                     hovertemplate=f"{ch}: {P['pos']}=%{{customdata[0]:.0f}}<br>-log10p=%{{y:.2f}}<extra></extra>",
                                     customdata=np.stack([pos[m]], axis=-1)))
    fig.add_hline(y=-np.log10(pc), line=dict(color="#E8834C", dash="dash", width=1.4))
    return ply_style(fig, _titled(df, P, "曼哈顿图"), "染色体", f"-log10({P['pval']})", legend=len(order) <= 12)


def ins_manhattan(df, P):
    chrom, pos, p, x, order = _manh_calc(df, P)
    y = -np.log10(p)
    pc = float(P.get("p_cut") or 5e-8)
    sig = y > -np.log10(pc)
    i = int(np.argmax(y))
    lab = P.get("label_col")
    name = c.short(df[lab].iloc[i], 14) if lab and lab in df.columns else ""
    return (f"n={len(y)} 个位点；超阈值（{P['pval']}<{pc:g}）{int(sig.sum())} 个。"
            f"最显著：{name + chr(32) if name else ''}{chrom[i]}:{pos[i]:.0f}（-log10 p = {y[i]:.2f}）。")


# ================= 训练曲线 =================
def mpl_traincurve(df, P):
    if not P.get("values"):
        raise ValueError("请在参数中选择至少一个指标列")
    x = c.num(df[P["x"]]).to_numpy(float)
    fig, ax = c.prep_fig()
    colors = c.cat_colors(list(P["values"]))
    for name in P["values"]:
        y = c.num(df[name]).to_numpy(float)
        ok = np.isfinite(x) & np.isfinite(y)
        ax.plot(x[ok], y[ok], lw=2, color=colors[name], label=str(name))
        if P.get("logy"):
            ax.set_yscale("log")
    ax.legend()
    mpl_title(ax, _titled(df, P, "训练曲线"), P["x"], None)
    return fig


def ply_traincurve(df, P):
    if not P.get("values"):
        raise ValueError("请在参数中选择至少一个指标列")
    x = c.num(df[P["x"]]).to_numpy(float)
    fig = go.Figure()
    colors = c.cat_colors(list(P["values"]))
    for name in P["values"]:
        y = c.num(df[name]).to_numpy(float)
        ok = np.isfinite(x) & np.isfinite(y)
        fig.add_trace(go.Scatter(x=x[ok], y=y[ok], mode="lines", name=c.short(str(name), 22),
                                 line=dict(color=colors[name], width=2.2),
                                 hovertemplate=f"{c.short(str(name), 22)}<br>%{{x}}: %{{y:.4g}}<extra></extra>"))
    if P.get("logy"):
        fig.update_yaxes(type="log")
    return ply_style(fig, _titled(df, P, "训练曲线"), P["x"], None)


def ins_traincurve(df, P):
    x = c.num(df[P["x"]]).to_numpy(float)
    parts = []
    for name in P["values"]:
        y = c.num(df[name]).dropna()
        if not len(y):
            continue
        tail = y.iloc[-max(1, len(y) // 10):].mean()
        parts.append(f"{c.short(str(name), 14)}: 最优 {c.fmt(y.min(), 4)} → 末段 {c.fmt(tail, 4)}")
    s = "；".join(parts) + "。"
    vals = [c.num(df[n]).dropna().to_numpy(float) for n in P["values"]]
    if len(vals) >= 2:
        a, b = vals[0], vals[1]
        n = min(len(a), len(b))
        ta, tb = a[-max(1, n // 5):], b[-max(1, n // 5):]
        if len(ta) >= 3 and ta.mean() < a[:max(1, n // 5)].mean() and tb.mean() > b[:max(1, n // 5)].mean():
            s += " 末段训练集继续改善而验证集回升 → 注意过拟合。"
    return s


# ================= ROC / PR =================
def _binary(df, P):
    y = df[P["ytrue"]]
    y = c.num(y) if y.dtype.kind in "biufc" else y.astype(str)
    vals = pd.unique(y.dropna())
    pos = 1 if (y == 1).any() else (vals[0] if len(vals) else 1)
    return (y == pos).astype(int).to_numpy(), str(pos)


def _roc_curve(y, s):
    ok = np.isfinite(s)
    y, s = y[ok], s[ok]
    order = np.argsort(-s)
    y = y[order]
    tp = np.cumsum(y); fp = np.cumsum(1 - y)
    if tp[-1] == 0 or fp[-1] == 0:
        return None
    tpr = np.concatenate([[0], tp / tp[-1]])
    fpr = np.concatenate([[0], fp / fp[-1]])
    auc = float(np.sum((fpr[1:] - fpr[:-1]) * (tpr[1:] + tpr[:-1]) / 2))
    return fpr, tpr, auc


def _pr_curve(y, s):
    ok = np.isfinite(s)
    y, s = y[ok], s[ok]
    order = np.argsort(-s)
    y = y[order]
    tp = np.cumsum(y); fp = np.cumsum(1 - y)
    if tp[-1] == 0:
        return None
    prec = tp / np.maximum(tp + fp, 1)
    rec = tp / tp[-1]
    prec = np.concatenate([[1], prec]); rec = np.concatenate([[0], rec])
    ap = float(np.sum(np.diff(rec) * prec[1:]))
    return rec, prec, ap


def mpl_roc(df, P):
    if not P.get("scores"):
        raise ValueError("请在参数中选择至少一个预测分数列")
    y, pos = _binary(df, P)
    fig, ax = c.prep_fig()
    for name in P["scores"]:
        r = _roc_curve(y, c.num(df[name]).to_numpy(float))
        if r is None:
            continue
        fpr, tpr, auc = r
        ax.plot(fpr, tpr, lw=2, label=f"{c.short(str(name), 20)} (AUC={auc:.3f})")
    ax.plot([0, 1], [0, 1], "--", color="#94A3B8", lw=1.2, label="随机 0.5")
    ax.set_xlabel("假阳性率 FPR"); ax.set_ylabel("真阳性率 TPR")
    ax.set_title(_titled(df, P, f"ROC 曲线（正类 = {pos}）"), pad=12)
    ax.legend(loc="lower right")
    return fig


def ply_roc(df, P):
    if not P.get("scores"):
        raise ValueError("请在参数中选择至少一个预测分数列")
    y, pos = _binary(df, P)
    fig = go.Figure()
    for name in P["scores"]:
        r = _roc_curve(y, c.num(df[name]).to_numpy(float))
        if r is None:
            continue
        fpr, tpr, auc = r
        fig.add_trace(go.Scatter(x=fpr, y=tpr, mode="lines", name=f"{c.short(str(name), 20)} AUC={auc:.3f}",
                                 line=dict(width=2.4), hovertemplate="FPR %{x:.3f}<br>TPR %{y:.3f}<extra></extra>"))
    fig.add_trace(go.Scatter(x=[0, 1], y=[0, 1], mode="lines", name="随机", line=dict(color="#94A3B8", dash="dash", width=1)))
    return ply_style(fig, _titled(df, P, f"ROC 曲线（正类 = {pos}）"), "假阳性率 FPR", "真阳性率 TPR")


def ins_roc(df, P):
    y, pos = _binary(df, P)
    parts = []
    for name in P["scores"]:
        r = _roc_curve(y, c.num(df[name]).to_numpy(float))
        if r:
            grade = "优" if r[2] > 0.85 else ("良" if r[2] > 0.7 else ("一般" if r[2] > 0.6 else "接近随机"))
            parts.append(f"{c.short(str(name), 16)}: AUC={r[2]:.3f}（{grade}）")
    return f"n={len(y)}，正类 {int(y.sum())} 个（正类取 {pos}）。" + "；".join(parts)


def mpl_prc(df, P):
    if not P.get("scores"):
        raise ValueError("请在参数中选择至少一个预测分数列")
    y, pos = _binary(df, P)
    fig, ax = c.prep_fig()
    for name in P["scores"]:
        r = _pr_curve(y, c.num(df[name]).to_numpy(float))
        if r is None:
            continue
        rec, prec, ap = r
        ax.plot(rec, prec, lw=2, label=f"{c.short(str(name), 20)} (AP={ap:.3f})")
    ax.axhline(y.mean(), color="#94A3B8", ls="--", lw=1.2, label=f"基线 {y.mean():.3f}")
    ax.set_xlabel("召回率 Recall"); ax.set_ylabel("精确率 Precision")
    ax.set_title(_titled(df, P, f"PR 曲线（正类 = {pos}）"), pad=12)
    ax.legend(loc="lower left")
    return fig


def ply_prc(df, P):
    if not P.get("scores"):
        raise ValueError("请在参数中选择至少一个预测分数列")
    y, pos = _binary(df, P)
    fig = go.Figure()
    for name in P["scores"]:
        r = _pr_curve(y, c.num(df[name]).to_numpy(float))
        if r is None:
            continue
        rec, prec, ap = r
        fig.add_trace(go.Scatter(x=rec, y=prec, mode="lines", name=f"{c.short(str(name), 20)} AP={ap:.3f}",
                                 line=dict(width=2.4), hovertemplate="Recall %{x:.3f}<br>Precision %{y:.3f}<extra></extra>"))
    fig.add_hline(y=y.mean(), line=dict(color="#94A3B8", dash="dash", width=1))
    return ply_style(fig, _titled(df, P, f"PR 曲线（正类 = {pos}）"), "召回率 Recall", "精确率 Precision")


def ins_prc(df, P):
    y, pos = _binary(df, P)
    parts = []
    for name in P["scores"]:
        r = _pr_curve(y, c.num(df[name]).to_numpy(float))
        if r:
            parts.append(f"{c.short(str(name), 16)}: AP={r[2]:.3f}（基线 {y.mean():.3f}）")
    return f"类别不平衡时 PR 曲线比 ROC 更灵敏。" + "；".join(parts)


# ================= 混淆矩阵 =================
def _cm_labels(df, P):
    yt = df[P["ytrue"]].astype(str); yp = df[P["ypred"]].astype(str)
    labels = sorted(set(yt) | set(yp))
    if len(labels) > 30:
        raise ValueError(f"类别过多（{len(labels)} 类）。混淆矩阵只适用于少类别标签，请换用类别列")
    return yt, yp, labels


def mpl_cm(df, P):
    yt, yp, labels = _cm_labels(df, P)
    m = pd.crosstab(yt, yp).reindex(index=labels, columns=labels, fill_value=0).to_numpy(float)
    fig, ax = c.prep_fig(6.4, 5.4)
    im = ax.imshow(m, cmap="Blues")
    fig.colorbar(im, ax=ax, pad=0.01, shrink=0.85, label="计数")
    ax.set_xticks(range(len(labels)), [c.short(v, 14) for v in labels], rotation=30, ha="right")
    ax.set_yticks(range(len(labels)), [c.short(v, 14) for v in labels])
    ax.grid(False)
    for i in range(len(labels)):
        for j in range(len(labels)):
            row = m[i].sum() or 1
            ax.text(j, i, f"{m[i, j]:.0f}\n{m[i, j] / row * 100:.0f}%", ha="center", va="center",
                    fontsize=9, color="white" if m[i, j] > m.max() * 0.6 else "#1F2937")
    mpl_title(ax, _titled(df, P, "混淆矩阵（行=真实，列=预测）"), "预测", "真实")
    return fig


def ply_cm(df, P):
    yt, yp, labels = _cm_labels(df, P)
    m = pd.crosstab(yt, yp).reindex(index=labels, columns=labels, fill_value=0).to_numpy(float)
    text = [[f"{int(v)}" for v in row] for row in m]
    fig = go.Figure(go.Heatmap(z=m, x=[c.short(v, 14) for v in labels], y=[c.short(v, 14) for v in labels],
                               colorscale="Blues", text=text, texttemplate="%{text}",
                               colorbar=dict(title="计数", thickness=13),
                               hovertemplate="真实 %{y} × 预测 %{x} = %{z:.0f}<extra></extra>"))
    fig.update_yaxes(autorange="reversed")
    return ply_style(fig, _titled(df, P, "混淆矩阵"), "预测", "真实", legend=False)


def ins_cm(df, P):
    yt, yp, labels = _cm_labels(df, P)
    m = pd.crosstab(yt, yp).reindex(index=labels, columns=labels, fill_value=0).to_numpy(float)
    acc = np.trace(m) / max(m.sum(), 1)
    conf = []
    mm = m.copy(); np.fill_diagonal(mm, 0)
    if mm.sum() > 0:
        i, j = np.unravel_index(np.argmax(mm), mm.shape)
        conf = [f"最易混淆：真实 {labels[i]} → 预测 {labels[j]}（{m[i, j]:.0f} 次）"]
    return f"准确率 {acc * 100:.1f}%（{int(np.trace(m))}/{int(m.sum())}，{len(labels)} 类）。" + (conf[0] if conf else "")


# ================= 网络图 =================
def _net_graph(df, P, top_n=120):
    import networkx as nx
    src = df[P["src"]].astype(str); dst = df[P["dst"]].astype(str)
    w = c.num(df[P["weight"]]).to_numpy(float) if P.get("weight") else np.ones(len(df))
    ok = src.ne("") & dst.ne("") & (src.to_numpy() != dst.to_numpy())
    edges = [(a, b, max(float(v), 0.0)) for a, b, v in zip(src[ok], dst[ok], w[ok])]
    G = nx.Graph()
    for a, b, v in edges:
        if G.has_edge(a, b):
            G[a][b]["weight"] += v
        else:
            G.add_edge(a, b, weight=v)
    if G.number_of_nodes() == 0:
        raise ValueError("没有有效边（来源/目标为空或全部自环），请检查两列")
    if len(G) > top_n:
        deg = dict(G.degree())
        keep = sorted(deg, key=deg.get, reverse=True)[:top_n]
        G = G.subgraph(keep).copy()
    pos = nx.spring_layout(G, seed=42, iterations=60, weight="weight")
    return G, pos


def mpl_network(df, P):
    G, pos = _net_graph(df, P, int(P.get("top_n") or 120))
    deg = dict(G.degree())
    fig, ax = c.prep_fig(8.8, 7.2)
    ws = np.array([G[u][v]["weight"] for u, v in G.edges()]) if G.number_of_edges() else np.array([])
    wmax = ws.max() if len(ws) else 1
    for (u, v) in G.edges():
        a = 0.25 + 0.55 * (G[u][v]["weight"] / (wmax or 1))
        ax.plot([pos[u][0], pos[v][0]], [pos[u][1], pos[v][1]], color="#9FB0CC", lw=0.6 + 2.2 * (G[u][v]["weight"] / (wmax or 1)), alpha=a, zorder=1)
    dmax = max(deg.values()) or 1
    xs = [pos[n][0] for n in G]; ys = [pos[n][1] for n in G]
    ss = [40 + 340 * (deg[n] / dmax) ** 1.4 for n in G]
    sc = ax.scatter(xs, ys, s=ss, c=[deg[n] for n in G], cmap="YlGnBu", edgecolors="#31435F", lw=0.7, zorder=2)
    fig.colorbar(sc, ax=ax, pad=0.01, shrink=0.7, label="连接度")
    hubs = sorted(deg, key=deg.get, reverse=True)[:int(P.get("label_top") or 14)]
    for n in hubs:
        ax.annotate(c.short(n, 16), pos[n], textcoords="offset points", xytext=(0, 8),
                    ha="center", fontsize=8.5, color="#1F2937", zorder=3)
    ax.axis("off")
    ax.set_title(_titled(df, P, f"网络图（{len(G)} 节点 / {G.number_of_edges()} 边）"), pad=10)
    return fig


def ply_network(df, P):
    G, pos = _net_graph(df, P, int(P.get("top_n") or 120))
    deg = dict(G.degree())
    fig = go.Figure()
    xs, ys = [], []
    for (u, v) in G.edges():
        xs += [pos[u][0], pos[v][0], None]
        ys += [pos[u][1], pos[v][1], None]
    if xs:
        fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines", name="边", line=dict(color="#9FB0CC", width=1), hoverinfo="skip"))
    dmax = max(deg.values()) or 1
    ns = list(G)
    fig.add_trace(go.Scatter(x=[pos[n][0] for n in ns], y=[pos[n][1] for n in ns], mode="markers", name="节点",
                             marker=dict(size=[8 + 20 * (deg[n] / dmax) ** 1.4 for n in ns],
                                         color=[deg[n] for n in ns], colorscale="YlGnBu",
                                         line=dict(width=1, color="#31435F"), showscale=True,
                                         colorbar=dict(title="连接度", thickness=13)),
                             text=[f"{n}<br>度={deg[n]}" for n in ns],
                             hovertemplate="%{text}<extra></extra>"))
    hubs = sorted(deg, key=deg.get, reverse=True)[:int(P.get("label_top") or 14)]
    for n in hubs:
        fig.add_annotation(x=pos[n][0], y=pos[n][1], text=c.short(n, 16), showarrow=False,
                           yshift=12, font=dict(size=11, color="#1F2937"))
    fig.update_xaxes(visible=False); fig.update_yaxes(visible=False, scaleanchor="x")
    return ply_style(fig, _titled(df, P, f"网络图（{len(G)} 节点 / {G.number_of_edges()} 边）"), legend=False)


def ins_network(df, P):
    G, pos = _net_graph(df, P, int(P.get("top_n") or 120))
    deg = dict(G.degree())
    hubs = sorted(deg, key=deg.get, reverse=True)[:3]
    n, e = G.number_of_nodes(), G.number_of_edges()
    dens = 2 * e / (n * (n - 1)) if n > 1 else 0
    return (f"网络 {n} 节点 / {e} 边，密度 {dens:.3f}。枢纽节点："
            + "、".join(f"{c.short(m, 14)}(度={deg[m]})" for m in hubs)
            + "。枢纽通常值得优先关注/验证。")


# ================= 桑基图 =================
def _sankey_links(df, P, top_n=25):
    src = df[P["src"]].astype(str); dst = df[P["dst"]].astype(str)
    val = c.num(df[P["value"]]).to_numpy(float) if P.get("value") else np.ones(len(df))
    ok = src.ne("") & dst.ne("") & np.isfinite(val) & (val > 0)
    agg = pd.DataFrame({"s": src[ok], "t": dst[ok], "v": val[ok]}).groupby(["s", "t"], as_index=False)["v"].sum()
    agg = agg.sort_values("v", ascending=False).head(top_n)
    return agg


def ply_sankey(df, P):
    agg = _sankey_links(df, P, int(P.get("top_n") or 25))
    nodes = list(dict.fromkeys(list(agg.s) + list(agg.t)))
    idx = {n: i for i, n in enumerate(nodes)}
    fig = go.Figure(go.Sankey(
        node=dict(label=nodes, pad=10, thickness=16,
                  color=[c.PALETTE[i % len(c.PALETTE)] for i in range(len(nodes))]),
        link=dict(source=[idx[s] for s in agg.s], target=[idx[t] for t in agg.t],
                  value=agg.v.tolist(),
                  hovertemplate="%{source.label} → %{target.label}<br>%{value:.4g}<extra></extra>")))
    fig.update_layout(template="plotly_white",
                      title=dict(text=_titled(df, P, f"桑基图（Top {len(agg)} 流量）"), font=dict(size=17)),
                      font=dict(size=13, color="#333F55"))
    return fig


def mpl_sankey(df, P):
    agg = _sankey_links(df, P, int(P.get("top_n") or 25))
    left = agg.groupby("s")["v"].sum().sort_values(ascending=False)
    right = agg.groupby("t")["v"].sum().sort_values(ascending=False)
    total = max(left.sum(), right.sum()) or 1
    fig, ax = plt.subplots(figsize=(10.5, max(4.5, 0.5 * max(len(left), len(right)))))
    gap = 0.02 * total
    ly = total - left.sum() * 1.0
    ly = (total - (left.sum() + gap * (len(left) - 1))) / 2
    ry = (total - (right.sum() + gap * (len(right) - 1))) / 2
    lpos, rpos, lcursor, rcursor = {}, {}, ly, ry
    for n, v in left.items():
        lpos[n] = (lcursor, v); lcursor += v + gap
    for n, v in right.items():
        rpos[n] = (rcursor, v); rcursor += v + gap
    from matplotlib.patches import PathPatch
    from matplotlib.path import Path
    for _, row in agg.iterrows():
        y0, h0 = lpos.get(row.s, (0, 0)); y1, h1 = rpos.get(row.t, (0, 0))
        k = row.v / max(h0, 1e-9)
        path = Path([(0.6, y0 + h0 * k / 2), (5, y0 + h0 * k / 2), (5.4, y1 + h1 / 2), (9.4, y1 + h1 / 2)],
                    [Path.MOVETO, Path.CURVE4, Path.CURVE4, Path.CURVE4])
        ax.add_patch(PathPatch(path, fc=c.PALETTE[hash((row.s, row.t)) % len(c.PALETTE)],
                               alpha=0.45, lw=0))
    for n, (y, v) in lpos.items():
        ax.add_patch(plt.Rectangle((0.5, y), 0.12, v, color=c.PALETTE[hash(n) % len(c.PALETTE)]))
        ax.text(0.42, y + v / 2, c.short(n, 18), ha="right", va="center", fontsize=9)
    for n, (y, v) in rpos.items():
        ax.add_patch(plt.Rectangle((9.38, y), 0.12, v, color=c.PALETTE[hash(n) % len(c.PALETTE)]))
        ax.text(9.6, y + v / 2, c.short(n, 18), ha="left", va="center", fontsize=9)
    ax.set_xlim(0, 10); ax.set_ylim(0, total)
    ax.axis("off")
    ax.set_title(_titled(df, P, f"桑基图（Top {len(agg)} 流量）"), pad=10)
    return fig


def ins_sankey(df, P):
    agg = _sankey_links(df, P, int(P.get("top_n") or 25))
    tot = agg.v.sum()
    i = int(np.argmax(agg.v))
    return (f"共展示 {len(agg)} 条流量，合计 {c.fmt(tot, 4)}。最大流量：{c.short(agg.s.iloc[i], 14)} → "
            f"{c.short(agg.t.iloc[i], 14)}（{c.fmt(agg.v.iloc[i], 4)}，占 {agg.v.iloc[i] / tot * 100:.0f}%）。")
