"""结构/Rosetta、分子动力学、序列分析渲染器（14 种），mpl + plotly 双实现。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import plotly.graph_objects as go

import common as c
from renderers_basic import ply_style, mpl_title, _titled

KD_HYDRO = dict(A=1.8, R=-4.5, N=-3.5, D=-3.5, C=2.5, Q=-3.5, E=-3.5, G=-0.4,
                H=-3.2, I=4.5, L=3.8, K=-3.9, M=1.9, F=2.8, P=-1.6, S=-0.8,
                T=-0.7, W=-0.9, Y=-1.3, V=4.2)


# ================= 能量漏斗图 =================
def _fun_groups(df, P):
    x = c.num(df[P["x"]]); y = c.num(df[P["y"]])
    ok = np.isfinite(x) & np.isfinite(y)
    gcol = P.get("group")
    if gcol and gcol in df.columns:
        g = df.loc[ok, gcol].astype(str)
        return [(v, x[ok][g == v].to_numpy(), y[ok][g == v].to_numpy()) for v in pd.unique(g)]
    return [("decoy", x[ok].to_numpy(), y[ok].to_numpy())]


def _funnel_envelope(xs, ys, nbins=14):
    """每个 x 分位桶的最优(最小)y 连线。"""
    edges = np.quantile(xs, np.linspace(0, 1, nbins + 1))
    edges = np.unique(edges)
    pts = []
    for a, b in zip(edges[:-1], edges[1:]):
        m = (xs >= a) & (xs <= b + 1e-12)
        if m.sum() >= 2:
            i = np.argmin(ys[m])
            pts.append((xs[m][i], ys[m][i]))
    return np.array(pts).T if len(pts) >= 3 else (None, None)


def mpl_funnel(df, P):
    groups = _fun_groups(df, P)
    fig, ax = c.prep_fig()
    colors = c.cat_colors([g[0] for g in groups])
    allx = np.concatenate([g[1] for g in groups]); ally = np.concatenate([g[2] for g in groups])
    for name, xs, ys in groups:
        ax.scatter(xs, ys, s=22, alpha=0.6, color=colors[name], label=str(name), edgecolors="none")
    ex, ey = _funnel_envelope(allx, ally)
    if ex is not None:
        ax.plot(ex, ey, "-", color="#1F2937", lw=1.8, alpha=0.75, label="前沿（分位桶最优）")
    i = np.argmin(ally)
    ax.plot(allx[i], ally[i], "*", ms=17, color="#E8546D", mec="white", zorder=5)
    ax.annotate(f"最优 {c.fmt(ally[i], 4)}\n({P['x']}={c.fmt(allx[i], 3)})", (allx[i], ally[i]),
                textcoords="offset points", xytext=(10, 6), fontsize=9.5)
    ax.legend(loc="best")
    mpl_title(ax, _titled(df, P, "能量漏斗图（越往左下越好）"), P["x"], P["y"])
    return fig


def ply_funnel(df, P):
    groups = _fun_groups(df, P)
    fig = go.Figure()
    colors = c.cat_colors([g[0] for g in groups])
    allx = np.concatenate([g[1] for g in groups]); ally = np.concatenate([g[2] for g in groups])
    for name, xs, ys in groups:
        fig.add_trace(go.Scatter(x=xs, y=ys, mode="markers", name=c.short(name, 18),
                                 marker=dict(size=6, color=colors[name], opacity=0.6)))
    ex, ey = _funnel_envelope(allx, ally)
    if ex is not None:
        fig.add_trace(go.Scatter(x=ex, y=ey, mode="lines", name="前沿", line=dict(color="#1F2937", width=2)))
    i = np.argmin(ally)
    fig.add_trace(go.Scatter(x=[allx[i]], y=[ally[i]], mode="markers+text", name="最优",
                             text=[f"最优 {c.fmt(ally[i], 4)}"], textposition="top right",
                             marker=dict(size=15, symbol="star", color="#E8546D", line=dict(width=1, color="white"))))
    return ply_style(fig, _titled(df, P, "能量漏斗图"), P["x"], P["y"])


def ins_funnel(df, P):
    groups = _fun_groups(df, P)
    allx = np.concatenate([g[1] for g in groups]); ally = np.concatenate([g[2] for g in groups])
    i = np.argmin(ally)
    r = c.corr(allx, ally)
    s = f"n={len(ally)}，最优 {P['y']} = {c.fmt(ally[i], 4)}（{P['x']} = {c.fmt(allx[i], 3)}）。"
    if r is not None:
        s += f" 两变量 r = {r:.3f}；漏斗状分布（低 {P['x']} 区离散、随 {P['x']} 收敛）通常代表采样良好。"
    return s


# ================= Score 分布（多群体阶梯叠加） =================
def mpl_scorehist(df, P):
    x = c.num(df[P["x"]])
    hue = P.get("hue") or None
    fig, ax = c.prep_fig()
    if hue and hue in df.columns and df[hue].nunique() <= 10:
        g = df[hue].astype(str)
        colors = c.cat_colors(g.dropna().unique())
        for v, col in colors.items():
            d = x[g == v].dropna().to_numpy()
            if len(d):
                ax.hist(d, bins=int(P.get("bins") or 40), histtype="step", lw=2, density=True,
                        color=col, label=f"{c.short(v, 18)} (n={len(d)})")
        ax.legend(title=str(hue), title_fontsize=9)
        ax.set_ylabel("概率密度")
    else:
        ax.hist(x.dropna(), bins=int(P.get("bins") or 40), color="#4C6FE8", edgecolor="white", lw=0.5)
        ax.set_ylabel("频数")
    mpl_title(ax, _titled(df, P, "Score 分布"), P["x"], None)
    return fig


def ply_scorehist(df, P):
    x = c.num(df[P["x"]])
    hue = P.get("hue") or None
    fig = go.Figure()
    overlay = False
    if hue and hue in df.columns and df[hue].nunique() <= 10:
        overlay = True
        g = df[hue].astype(str)
        colors = c.cat_colors(g.dropna().unique())
        for v, col in colors.items():
            d = x[g == v].dropna()
            if len(d):
                fig.add_trace(go.Histogram(x=d, histnorm="probability density", name=c.short(v, 18),
                                           marker=dict(color=col, opacity=0.55)))
    else:
        fig.add_trace(go.Histogram(x=x.dropna(), marker=dict(color="#4C6FE8")))
    if overlay:
        fig.update_layout(barmode="overlay")
    return ply_style(fig, _titled(df, P, "Score 分布"), P["x"], None, legend=overlay)


def ins_scorehist(df, P):
    x = c.num(df[P["x"]]).dropna()
    hue = P.get("hue")
    if hue and hue in df.columns and df[hue].nunique() <= 10:
        g = df[hue].astype(str)
        parts = [f"{c.short(v, 14)}: 中位 {c.fmt(x[g == v].median(), 4)} / 最优 {c.fmt(x[g == v].min(), 4)}"
                 for v in pd.unique(g.dropna())[:6]]
        return f"{df[hue].nunique()} 个群体。" + "；".join(parts) + "（分数越低越好）。"
    q = x.quantile([0.05, 0.5, 0.95])
    return f"n={len(x)}；5% 分位 {c.fmt(q.iloc[0], 4)}，中位 {c.fmt(q.iloc[1], 4)}，95% 分位 {c.fmt(q.iloc[2], 4)}，双峰/多峰提示多个构象族群。"


# ================= 每残基能量分解（正负堆叠柱） =================
def mpl_perres(df, P):
    if not P.get("values"):
        raise ValueError("请在参数中选择至少一个能量项数值列")
    xv = df[P["x"]]
    vals = df[list(P["values"])].apply(c.num)
    fig, ax = c.prep_fig(11 if len(df) > 60 else 8.4, 4.8)
    x = np.arange(len(xv))
    colors = c.cat_colors(list(P["values"]))
    pos = np.zeros(len(x)); neg = np.zeros(len(x))
    for colname in P["values"]:
        v = vals[colname].to_numpy(float)
        v = np.nan_to_num(v, nan=0.0)
        ax.bar(x, np.maximum(v, 0), bottom=pos, color=colors[colname], width=0.9, label=str(colname))
        ax.bar(x, np.minimum(v, 0), bottom=neg, color=colors[colname], width=0.9)
        pos += np.maximum(v, 0); neg += np.minimum(v, 0)
    total = pos + neg
    i = int(np.argmin(total))
    ax.annotate(f"{c.short(xv.iloc[i], 14)}\n合计 {c.fmt(total[i], 4)}", (x[i], total[i]),
                textcoords="offset points", xytext=(0, -30 if total[i] < 0 else 10),
                ha="center", fontsize=9, color="#E8546D")
    ax.axhline(0, color="#94A3B8", lw=0.8)
    step = max(1, len(x) // 36)
    ax.set_xticks(x[::step], [c.short(v, 10) for v in xv[::step]], fontsize=8)
    ax.legend(ncol=min(6, len(P["values"])), loc="upper right", fontsize=9)
    mpl_title(ax, _titled(df, P, "每残基能量分解"), P["x"], "能量（各项堆叠）")
    return fig


def ply_perres(df, P):
    if not P.get("values"):
        raise ValueError("请在参数中选择至少一个能量项数值列")
    xv = df[P["x"]].astype(str)
    vals = df[list(P["values"])].apply(c.num)
    fig = go.Figure()
    colors = c.cat_colors(list(P["values"]))
    for colname in P["values"]:
        v = vals[colname].to_numpy(float)
        fig.add_trace(go.Bar(x=xv, y=np.nan_to_num(v, nan=0.0), name=c.short(str(colname), 22),
                             marker_color=colors[colname], hovertemplate=f"{c.short(str(colname), 22)}: %{{y:.3f}}<extra></extra>"))
    fig.update_layout(barmode="relative")
    total = np.nansum(vals.to_numpy(float), axis=1)
    i = int(np.argmin(total))
    fig.add_annotation(x=xv.iloc[i], y=float(total[i]), text=f"合计最优 {c.short(xv.iloc[i], 14)}: {c.fmt(total[i], 4)}",
                       showarrow=True, arrowhead=2, font=dict(size=12, color="#E8546D"))
    return ply_style(fig, _titled(df, P, "每残基能量分解"), P["x"], "能量（各项堆叠）")


def ins_perres(df, P):
    vals = df[list(P["values"])].apply(c.num).to_numpy(float)
    total = np.nansum(np.nan_to_num(vals, nan=0.0), axis=1)
    i = int(np.argmin(total))
    colsum = np.nansum(np.nan_to_num(vals, nan=0.0), axis=0)
    j = int(np.argmax(np.abs(colsum)))
    return (f"{len(P['values'])} 个能量项、{len(total)} 个残基位。合计最低（最有利）为第 {i + 1} 位 "
            f"{c.short(df[P['x']].iloc[i], 12)}（{c.fmt(total[i], 4)}）；贡献绝对值最大的能量项是 "
            f"「{P['values'][j]}」（Σ={c.fmt(colsum[j], 4)}）。")


# ================= ddG 条形图 =================
def _ddg_top(df, P):
    """数据过多时两端各取 N/2（最去稳定 + 最稳定），按 ΔΔG 升序展示。"""
    lab = df[P["label"]].astype(str).to_numpy()  # numpy 数组：后续 [idx] 是位置索引
    v = c.num(df[P["value"]]).to_numpy(float)
    ok = np.isfinite(v)
    lab, v = lab[ok], v[ok]
    top_n = int(P.get("top_n") or 60)
    n_total = len(v)
    if top_n > 0 and n_total > top_n:
        half = max(top_n // 2, 1)
        up = np.argsort(-v)[:half]   # 最去稳定
        dn = np.argsort(v)[:half]    # 最稳定
        idx = np.unique(np.concatenate([dn, up]))
    else:
        idx = np.arange(n_total)
    lab, v = lab[idx], v[idx]
    order = np.argsort(v)  # barh 自下而上 → 升序让正最大(去稳定)在最上
    return lab[order], v[order], n_total


def mpl_ddgbar(df, P):
    lab, v, n_total = _ddg_top(df, P)
    n = len(v)
    cut = P.get("cut")
    fig_h = min(max(4.0, n * 0.3), 16)
    fig, ax = plt.subplots(figsize=(8.6, fig_h))
    colors = ["#C62828" if x > 0 else "#2E6FB7" for x in v]
    ax.barh(np.arange(n), v, color=colors, height=0.7)
    ax.set_yticks(np.arange(n), [c.short(t, 26) for t in lab], fontsize=8.5)
    ax.axvline(0, color="#64748B", lw=1)
    if cut:
        for s in (cut, -cut):
            ax.axvline(s, color="#94A3B8", ls="--", lw=1)
        ax.text(cut, n - 0.2, f" ±{cut} REU", fontsize=8.5, color="#94A3B8", va="top")
    if n <= 30:
        for i, x in enumerate(v):
            ax.text(x + (0.02 * (np.nanmax(np.abs(v)) or 1)) * (1 if x >= 0 else -1), i,
                    f"{x:.2f}", va="center", ha="left" if x >= 0 else "right", fontsize=7.5, color="#334155")
    if n < n_total:
        ax.text(0.99, 0.02, f"仅展示两端各 {n // 2} 条（共 {n_total}）", transform=ax.transAxes,
                ha="right", fontsize=8, color="#94A3B8")
    mpl_title(ax, _titled(df, P, "ΔΔG 条形图（红=去稳定，蓝=稳定）"), "ΔΔG (REU)", None)
    return fig


def ply_ddgbar(df, P):
    lab, v, n_total = _ddg_top(df, P)
    fig = go.Figure(go.Bar(x=v, y=[c.short(t, 30) for t in lab], orientation="h",
                           marker=dict(color=["#C62828" if t > 0 else "#2E6FB7" for t in v]),
                           hovertemplate="%{y}<br>ΔΔG = %{x:.3f}<extra></extra>"))
    cut = P.get("cut")
    shapes = []
    if cut:
        for s in (cut, -cut):
            shapes.append(dict(type="line", x0=s, x1=s, y0=0, y1=1, yref="paper",
                               line=dict(color="#94A3B8", width=1, dash="dash")))
    fig.update_layout(shapes=shapes)
    fig = ply_style(fig, _titled(df, P, "ΔΔG 条形图"), "ΔΔG (REU)", None)
    if len(v) < n_total:
        fig.add_annotation(xref="paper", yref="paper", x=0.99, y=0.02,
                           text=f"仅展示两端各 {len(v) // 2} 条（共 {n_total}）", showarrow=False,
                           font=dict(size=11, color="#94A3B8"))
    return fig


def ins_ddgbar(df, P):
    lab = df[P["label"]].astype(str)
    v = c.num(df[P["value"]])
    ok = v.notna()
    lab, vv = lab[ok], v[ok]
    cut = P.get("cut") or 2.0
    top_n = int(P.get("top_n") or 60)
    def names(s):
        return "、".join(f"{c.short(lab[i], 14)} {c.fmt(x, 3)}" for i, x in s.items())
    worst = vv.nlargest(3); best = vv.nsmallest(2)
    n_strong = int((vv.abs() > cut).sum())
    shown = top_n if (0 < top_n < len(vv)) else len(vv)
    return (f"n={len(vv)}，图中展示两端共 {shown} 条。最去稳定：{names(worst)}；"
            f"最稳定：{names(best)}。|ΔΔG| > {cut} 的有 {n_strong} 个（{n_strong / max(len(vv), 1) * 100:.0f}%）。"
            "正值通常表示去稳定。")


# ================= 矩阵热图（ddG 矩阵 / 接触图 / DCCM） =================
def mpl_matheat(df, P):
    if not P.get("values"):
        raise ValueError("请在参数中选择至少一个数值列作为矩阵列")
    idx = df[P["index"]].astype(str)
    values = list(P["values"])
    m = df[values].apply(c.num)
    fig, ax = plt.subplots(figsize=(min(14, max(7, 0.5 * len(values) + 3.5)),
                                    min(12, max(5, 0.32 * len(idx) + 2.5))))
    cmap_name = P.get("cmap") or "发散 (RdBu_r)"
    cmap = {"发散 (RdBu_r)": "RdBu_r", "顺序 (YlOrRd)": "YlOrRd", "蓝 (Blues)": "Blues", "Viridis": "viridis"}.get(cmap_name, "RdBu_r")
    arr = m.to_numpy(float)
    if cmap == "RdBu_r":
        vmax = np.nanmax(np.abs(arr)) or 1
        im = ax.imshow(arr, cmap=cmap, vmin=-vmax, vmax=vmax, aspect="auto")
    else:
        im = ax.imshow(arr, cmap=cmap, aspect="auto")
    fig.colorbar(im, ax=ax, pad=0.01, shrink=0.85)
    ax.set_xticks(range(len(values)), [c.short(v, 14) for v in values], rotation=45, ha="right", fontsize=8.5)
    step = max(1, len(idx) // 44)
    ax.set_yticks(np.arange(0, len(idx), step), [c.short(v, 16) for v in idx[::step]], fontsize=8)
    ax.grid(False)
    if len(idx) <= 22 and len(values) <= 22 and P.get("annot", True):
        for i in range(arr.shape[0]):
            for j in range(arr.shape[1]):
                if np.isfinite(arr[i, j]):
                    ax.text(j, i, f"{arr[i, j]:.2f}", ha="center", va="center", fontsize=7)
    mpl_title(ax, _titled(df, P, "矩阵热图"))
    return fig


def ply_matheat(df, P):
    if not P.get("values"):
        raise ValueError("请在参数中选择至少一个数值列作为矩阵列")
    idx = df[P["index"]].astype(str)
    values = list(P["values"])
    arr = df[values].apply(c.num).to_numpy(float)
    cmap_name = P.get("cmap") or "发散 (RdBu_r)"
    cmap = {"发散 (RdBu_r)": "RdBu", "顺序 (YlOrRd)": "YlOrRd", "蓝 (Blues)": "Blues", "Viridis": "Viridis"}.get(cmap_name, "RdBu")
    zmin = zmax = None
    if cmap == "RdBu":
        vm = np.nanmax(np.abs(arr)) or 1
        zmin, zmax = -vm, vm
    fig = go.Figure(go.Heatmap(z=arr, x=[c.short(v, 16) for v in values], y=[c.short(v, 20) for v in idx],
                               colorscale=cmap, reversescale=(cmap == "RdBu"), zmin=zmin, zmax=zmax,
                               colorbar=dict(thickness=13),
                               hovertemplate="%{y} × %{x}<br>%{z:.4f}<extra></extra>"))
    return ply_style(fig, _titled(df, P, "矩阵热图"), legend=False)


def ins_matheat(df, P):
    idx = df[P["index"]].astype(str).to_numpy()
    values = list(P["values"])
    arr = df[values].apply(c.num).to_numpy(float)
    vmax_v = np.nanmax(arr); vmin_v = np.nanmin(arr)
    imax = np.unravel_index(np.nanargmax(arr), arr.shape)
    imin = np.unravel_index(np.nanargmin(arr), arr.shape)
    return (f"矩阵 {arr.shape[0]}×{arr.shape[1]}。最大 {c.fmt(vmax_v, 4)}（{c.short(idx[imax[0]], 14)} × {values[imax[1]]}），"
            f"最小 {c.fmt(vmin_v, 4)}（{c.short(idx[imin[0]], 14)} × {values[imin[1]]}）。")


# ================= 界面能量散点 =================
def mpl_iface(df, P):
    x = c.num(df[P["x"]]); y = c.num(df[P["y"]])
    ok = np.isfinite(x) & np.isfinite(y)
    fig, ax = c.prep_fig()
    ax.scatter(x[ok], y[ok], s=30, c="#4C6FE8", alpha=0.7, edgecolors="white", lw=0.4)
    mx, my = np.nanmedian(x[ok]), np.nanmedian(y[ok])
    ax.axvline(mx, color="#94A3B8", ls="--", lw=1)
    ax.axhline(my, color="#94A3B8", ls="--", lw=1)
    for tx, ha in ((0.02, "left"), (0.98, "right")):
        ax.text(tx, 0.96, "大界面·低能" if ha == "right" else "小界面·低能", transform=ax.transAxes,
                ha=ha, fontsize=9.5, color="#3FA66A")
        ax.text(tx, 0.03, "大界面·高能" if ha == "right" else "小界面·高能", transform=ax.transAxes,
                ha=ha, fontsize=9.5, color="#E8546D")
    mpl_title(ax, _titled(df, P, "界面 ΔG vs 埋藏面积（左下象限更理想）"), P["x"], P["y"])
    return fig


def ply_iface(df, P):
    x = c.num(df[P["x"]]); y = c.num(df[P["y"]])
    ok = np.isfinite(x) & np.isfinite(y)
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=x[ok], y=y[ok], mode="markers", marker=dict(size=7, color="#4C6FE8", opacity=0.7),
                             hovertemplate=f"{P['x']}: %{{x:.3f}}<br>{P['y']}: %{{y:.3f}}<extra></extra>"))
    mx, my = np.nanmedian(x[ok]), np.nanmedian(y[ok])
    fig.add_vline(x=mx, line=dict(color="#94A3B8", dash="dash", width=1))
    fig.add_hline(y=my, line=dict(color="#94A3B8", dash="dash", width=1))
    return ply_style(fig, _titled(df, P, "界面 ΔG vs 埋藏面积"), P["x"], P["y"])


def ins_iface(df, P):
    x = c.num(df[P["x"]]); y = c.num(df[P["y"]])
    ok = np.isfinite(x) & np.isfinite(y)
    mx, my = np.nanmedian(x[ok]), np.nanmedian(y[ok])
    ll = int(((x[ok] >= mx) & (y[ok] <= my)).sum())
    r = c.corr(x[ok], y[ok])
    s = f"n={int(ok.sum())}；{P['x']} 中位 {c.fmt(mx, 3)}，{P['y']} 中位 {c.fmt(my, 3)}。"
    s += f" 大界面且能量好（右下）占 {ll / max(ok.sum(), 1) * 100:.0f}%。"
    if r is not None:
        s += f" 两变量 r = {r:.3f}。"
    return s


# ================= pLDDT 曲线 =================
PLDDT_BANDS = [(0, 50, "#E8834C", "很低 <50"), (50, 70, "#E5C13D", "偏低 50–70"),
               (70, 90, "#5FB8D6", "可信 70–90"), (90, 101, "#3E6FE0", "很高 >90")]


def _plddt_xy(df, P):
    x = c.num(df[P["pos"]]).to_numpy(float)
    y = c.num(df[P["val"]]).to_numpy(float)
    ok = np.isfinite(x) & np.isfinite(y)
    return x[ok], y[ok]


def mpl_plddt(df, P):
    x, y = _plddt_xy(df, P)
    fig, ax = c.prep_fig(10.5, 4.2)
    for lo, hi, col, name in PLDDT_BANDS:
        ax.axhspan(lo, hi, color=col, alpha=0.07)
    for lo, hi, col, name in PLDDT_BANDS:
        m = (y >= lo) & (y < hi)
        if m.any():
            ax.plot(x[m], y[m], lw=1.6, color=col, label=name, solid_capstyle="round")
    for lv in (50, 70, 90):
        ax.axhline(lv, color="#CBD5E1", lw=0.8, ls=":")
    ax.set_ylim(min(0, y.min() - 3), 100)
    ax.legend(ncol=4, loc="upper center", bbox_to_anchor=(0.5, -0.14), fontsize=9)
    mpl_title(ax, _titled(df, P, "pLDDT 置信度沿序列分布"), P["pos"], "pLDDT")
    return fig


def ply_plddt(df, P):
    x, y = _plddt_xy(df, P)
    fig = go.Figure()
    for lo, hi, col, name in PLDDT_BANDS:
        m = (y >= lo) & (y < hi)
        if m.any():
            fig.add_trace(go.Scatter(x=x[m], y=y[m], mode="lines", name=name,
                                     line=dict(color=col, width=2)))
    for lv in (50, 70, 90):
        fig.add_hline(y=lv, line=dict(color="#CBD5E1", width=1, dash="dot"))
    fig.update_yaxes(range=[min(0, float(np.nanmin(y)) - 3), 100])
    return ply_style(fig, _titled(df, P, "pLDDT 置信度"), P["pos"], "pLDDT")


def ins_plddt(df, P):
    x, y = _plddt_xy(df, P)
    hi90 = (y > 90).mean() * 100
    hi70 = (y > 70).mean() * 100
    i = int(np.argmin(y))
    return (f"平均 pLDDT = {y.mean():.1f}；>90 占 {hi90:.0f}%，>70 占 {hi70:.0f}%"
            f"（>70 区域可认为骨架可信）。最低点：位置 {x[i]:.0f}（{y[i]:.1f}），此类区域侧链摆放需谨慎。")


# ================= 时间序列 ± 带状（RMSD/RMSF） =================
def mpl_tsband(df, P):
    t = c.num(df[P["t"]]).to_numpy(float)
    m = c.num(df[P["mean"]]).to_numpy(float)
    err = c.num(df[P["err"]]).to_numpy(float) if P.get("err") else None
    ok = np.isfinite(t) & np.isfinite(m)
    t, m = t[ok], m[ok]
    step = max(1, len(t) // 6000)
    fig, ax = c.prep_fig(9.5, 4.2)
    if err is not None:
        e = np.nan_to_num(err, nan=0.0)[ok]
        ax.fill_between(t[::step], (m - e)[::step], (m + e)[::step], color="#4C6FE8", alpha=0.22, label="±SD/SEM")
    ax.plot(t[::step], m[::step], color="#4C6FE8", lw=1.5)
    ax.legend(loc="best")
    mpl_title(ax, _titled(df, P, "时间序列"), P["t"], P["mean"])
    return fig


def ply_tsband(df, P):
    t = c.num(df[P["t"]]).to_numpy(float)
    m = c.num(df[P["mean"]]).to_numpy(float)
    err = c.num(df[P["err"]]).to_numpy(float) if P.get("err") else None
    ok = np.isfinite(t) & np.isfinite(m)
    t, m = t[ok], m[ok]
    step = max(1, len(t) // 6000)
    t, m = t[::step], m[::step]
    fig = go.Figure()
    if err is not None:
        e = np.nan_to_num(err, nan=0.0)[ok][::step]
        fig.add_trace(go.Scatter(x=np.concatenate([t, t[::-1]]),
                                 y=np.concatenate([m + e, (m - e)[::-1]]),
                                 fill="toself", name="±SD/SEM",
                                 fillcolor="rgba(76,111,232,0.18)", line=dict(width=0), hoverinfo="skip"))
    fig.add_trace(go.Scatter(x=t, y=m, mode="lines", name=P["mean"], line=dict(color="#4C6FE8", width=2)))
    return ply_style(fig, _titled(df, P, "时间序列"), P["t"], P["mean"])


def ins_tsband(df, P):
    t = c.num(df[P["t"]]).to_numpy(float); m = c.num(df[P["mean"]]).to_numpy(float)
    ok = np.isfinite(t) & np.isfinite(m)
    t, m = t[ok], m[ok]
    drift = m[-1] - m[: max(1, len(m) // 10)].mean()
    return (f"n={len(m)} 帧范围 [{c.fmt(t.min(), 3)}, {c.fmt(t.max(), 3)}]。早期均值 {c.fmt(m[: max(1, len(m) // 10)].mean(), 4)} → "
            f"末期 {c.fmt(m[-50:].mean(), 4)}，漂移 {c.fmt(drift, 4)}；峰值 {c.fmt(m.max(), 4)}（t={c.fmt(t[int(np.argmax(m))], 3)}）。"
            "平台期后稳定说明体系已弛豫。")


# ================= 自由能面 =================
def mpl_felandscape(df, P):
    x = c.num(df[P["x"]]).to_numpy(float); y = c.num(df[P["y"]]).to_numpy(float)
    ok = np.isfinite(x) & np.isfinite(y)
    x, y = x[ok], y[ok]
    bins = int(P.get("bins") or 50)
    temp = float(P.get("temp") or 298.15)
    kT = 0.0019872 * temp  # kcal/mol
    fmax = float(P.get("fmax") or 10)
    H, xe, ye = np.histogram2d(x, y, bins=bins)
    H = H.astype(float)
    if H.max() == 0:
        raise ValueError("有效数据点不足")
    with np.errstate(divide="ignore"):
        F = -kT * np.log(H / H.max())
    F[H == 0] = fmax
    F = np.clip(F.T, 0, fmax)
    fig, ax = c.prep_fig(8.6, 6.2)
    lv = np.linspace(0, fmax, 13)
    cf = ax.contourf((xe[:-1] + xe[1:]) / 2, (ye[:-1] + ye[1:]) / 2, F, levels=lv, cmap="jet_r", extend="max")
    fig.colorbar(cf, ax=ax, pad=0.01, label="ΔG (kcal/mol)")
    i = np.unravel_index(np.argmin(F), F.shape)
    bx = ((xe[:-1] + xe[1:]) / 2)[i[1]]
    by = ((ye[:-1] + ye[1:]) / 2)[i[0]]
    ax.plot(bx, by, "P", ms=13, color="white", mec="#1F2937")
    ax.annotate(f"最低盆地 {c.fmt(F[i], 2)} kcal/mol", (bx, by),
                textcoords="offset points", xytext=(10, 8), fontsize=9.5, color="white",
                bbox=dict(fc="black", alpha=0.45, ec="none", boxstyle="round,pad=0.25"))
    mpl_title(ax, _titled(df, P, f"自由能面（T = {temp:.0f} K）"), P["x"], P["y"])
    return fig


def ply_felandscape(df, P):
    x = c.num(df[P["x"]]).to_numpy(float); y = c.num(df[P["y"]]).to_numpy(float)
    ok = np.isfinite(x) & np.isfinite(y)
    x, y = x[ok], y[ok]
    bins = int(P.get("bins") or 50)
    temp = float(P.get("temp") or 298.15)
    kT = 0.0019872 * temp
    fmax = float(P.get("fmax") or 10)
    H, xe, ye = np.histogram2d(x, y, bins=bins)
    with np.errstate(divide="ignore"):
        F = -kT * np.log(H / max(H.max(), 1))
    F[H == 0] = fmax
    F = np.clip(F.T, 0, fmax)
    fig = go.Figure(go.Contour(z=F, x=(xe[:-1] + xe[1:]) / 2, y=(ye[:-1] + ye[1:]) / 2,
                               colorscale="Jet", reversescale=True, line_smoothing=0.85,
                               contours=dict(start=0, end=fmax, size=fmax / 12, coloring="heatmap"),
                               colorbar=dict(title="ΔG<br>(kcal/mol)", thickness=13),
                               hovertemplate=f"{P['x']}: %{{x:.3f}}<br>{P['y']}: %{{y:.3f}}<br>ΔG: %{{z:.2f}}<extra></extra>"))
    return ply_style(fig, _titled(df, P, f"自由能面（T = {temp:.0f} K）"), P["x"], P["y"], legend=False)


def ins_felandscape(df, P):
    x = c.num(df[P["x"]]).to_numpy(float); y = c.num(df[P["y"]]).to_numpy(float)
    ok = np.isfinite(x) & np.isfinite(y)
    bins = int(P.get("bins") or 50)
    H, xe, ye = np.histogram2d(x[ok], y[ok], bins=bins)
    kT = 0.0019872 * float(P.get("temp") or 298.15)
    F = np.where(H > 0, -kT * np.log(H / max(H.max(), 1)), np.nan)
    i = np.unravel_index(np.nanargmin(F), F.shape)
    return (f"n={int(ok.sum())} 点，{bins}×{bins} 分箱。最低盆地位于 {P['x']}={c.fmt(xe[i[1]], 3)}、"
            f"{P['y']}={c.fmt(ye[i[0]], 3)}（ΔG≈0 为参考态）；多个孤立深盆提示构象切换。")


# ================= 序列工具 =================
def _seqs(df, P, max_len=300):
    vals = df[P["seq"]].dropna().astype(str).str.strip().str.upper()
    vals = vals[vals.str.len() >= 6]
    if len(vals) < 2 and P.get("_single_ok") is not True:
        raise ValueError("序列列有效序列不足（至少 2 条，长度≥6）")
    L = int(np.median(vals.str.len()))
    truncated = False
    if L > max_len:
        L = max_len
        truncated = True
    seqs = [s[:L] for s in vals.tolist()]
    return seqs, L, truncated


def _freq_bits(seqs, L):
    idx = {aa: i for i, aa in enumerate(c.AA20)}
    F = np.zeros((L, 21))
    for s in seqs:
        for i, ch in enumerate(s[:L]):
            F[i, idx.get(ch, 20)] += 1
    n = F.sum(axis=1, keepdims=True)
    n[n == 0] = 1
    p = F / n
    with np.errstate(divide="ignore", invalid="ignore"):
        H = -np.where(p > 0, p * np.log2(p), 0).sum(axis=1)
    e = 20.0 / (2 * np.log(2) * np.maximum(n[:, 0], 2))  # 有限样本校正
    cons = np.clip(np.log2(20) - H - e, 0, None)
    bits = p[:, :20] * cons[:, None]
    return p, cons, bits


# ================= 序列 Logo =================
def mpl_logo(df, P):
    seqs, L, tr = _seqs(df, P)
    p, cons, bits = _freq_bits(seqs, L)
    w = min(max(7.0, L * 0.18), 24)
    fig, ax = plt.subplots(figsize=(w, 3.8))
    ymax = max(cons.max(), 0.4)
    ax.set_xlim(0.2, L + 0.8); ax.set_ylim(0, ymax)
    fig.canvas.draw()
    bbox = ax.get_window_extent()
    pts_per_bit = (bbox.height / fig.dpi * 72.0) / ymax
    mono = "DejaVu Sans Mono"
    for i in range(L):
        order = np.argsort(bits[i])[::-1]
        y = 0.0
        for j in order:
            h = bits[i, j]
            if h < 0.03:
                break
            ax.text(i + 1, y + h / 2, c.AA20[j], fontsize=max(2.2, h * pts_per_bit * 0.96),
                    ha="center", va="center", family=mono, color=c.aa_color(c.AA20[j]))
            y += h
    ax.set_ylabel("bits")
    ax.set_xlabel("位置")
    ax.tick_params(axis="x", labelsize=7)
    ttl = _titled(df, P, f"序列 Logo（{len(seqs)} 条" + ("，超过 300 位截断）" if tr else "）"))
    ax.set_title(ttl, pad=10)
    return fig


def ply_logo(df, P):
    seqs, L, tr = _seqs(df, P)
    p, cons, bits = _freq_bits(seqs, L)
    ymax = max(cons.max(), 0.4)
    px_per_bit = 340 / ymax
    fig = go.Figure()
    for i in range(L):
        y = 0.0
        order = np.argsort(bits[i])[::-1]
        for j in order:
            h = bits[i, j]
            if h < 0.04:
                break
            fig.add_annotation(x=i + 1, y=y + h / 2, text=c.AA20[j], showarrow=False,
                               font=dict(size=max(4, int(h * px_per_bit * 0.95)), family="monospace",
                                         color=c.aa_color(c.AA20[j])), hoverinfo="skip")
            y += h
    fig.update_xaxes(range=[0.2, L + 0.8], title="位置")
    fig.update_yaxes(range=[0, ymax], title="bits")
    fig.update_layout(template="plotly_white", margin=dict(l=60, r=30, t=56, b=50),
                      title=dict(text=_titled(df, P, f"序列 Logo（{len(seqs)} 条）"), font=dict(size=17)))
    return fig


def ins_logo(df, P):
    seqs, L, tr = _seqs(df, P)
    p, cons, bits = _freq_bits(seqs, L)
    order = np.argsort(cons)[::-1][:5]
    parts = [f"位置 {int(o) + 1}（{cons[o]:.2f} bits，主残基 {c.AA20[int(np.argmax(bits[o]))]}）" for o in order if cons[o] > 0.5]
    return f"{len(seqs)} 条序列 × {L} 位。" + ("最保守：" + "；".join(parts) if parts else "整体保守性较低。")


# ================= 保守性曲线 =================
def _cons_series(df, P):
    seqs, L, tr = _seqs(df, P)
    p, cons, bits = _freq_bits(seqs, L)
    return cons, L, len(seqs)


def mpl_entropy(df, P):
    cons, L, n = _cons_series(df, P)
    fig, ax = c.prep_fig(10 if L > 80 else 8.4, 4.0)
    ax.plot(np.arange(1, L + 1), cons, color="#4C6FE8", lw=1.6)
    ax.fill_between(np.arange(1, L + 1), cons, color="#4C6FE8", alpha=0.15)
    top = np.argsort(cons)[::-1][:min(6, L)]
    for o in top:
        if cons[o] < 0.4:
            continue
        ax.annotate(f"{o + 1}", (o + 1, cons[o]), textcoords="offset points", xytext=(0, 5),
                    ha="center", fontsize=8.5, color="#E8546D")
        ax.plot(o + 1, cons[o], "o", ms=4, color="#E8546D")
    mpl_title(ax, _titled(df, P, f"保守性曲线（{n} 条序列；红点=最保守位）"), "位置", "保守度 (bits)")
    return fig


def ply_entropy(df, P):
    cons, L, n = _cons_series(df, P)
    fig = go.Figure()
    xs = np.arange(1, L + 1)
    fig.add_trace(go.Scatter(x=xs, y=cons, mode="lines", fill="tozeroy", name="保守度",
                             line=dict(color="#4C6FE8", width=2),
                             hovertemplate="位置 %{x}<br>保守度 %{y:.2f} bits<extra></extra>"))
    return ply_style(fig, _titled(df, P, f"保守性曲线（{n} 条序列）"), "位置", "保守度 (bits)", legend=False)


def ins_entropy(df, P):
    cons, L, n = _cons_series(df, P)
    hi = np.argsort(cons)[::-1][:5]
    return (f"{n} 条序列 × {L} 位；平均保守度 {cons.mean():.2f} bits。"
            f"最保守位：{'、'.join(str(int(i) + 1) for i in hi)}（≥0.8 bits 通常具功能/结构意义）。")


# ================= 氨基酸组成 =================
def mpl_aacomp(df, P):
    seqs, L, _ = _seqs(df, P, max_len=100000)
    s = "".join(seqs)
    cnt = {aa: s.count(aa) for aa in c.AA20}
    tot = sum(cnt.values()) or 1
    items = sorted(cnt.items(), key=lambda kv: -kv[1])
    fig, ax = c.prep_fig()
    xs = [k for k, _ in items]
    ys = [v / tot * 100 for _, v in items]
    ax.bar(xs, ys, color=[c.aa_color(a) for a in xs], width=0.76)
    for i, v in enumerate(ys):
        ax.text(i, v + 0.15, f"{v:.1f}", ha="center", fontsize=8)
    ax.set_ylabel("占比 (%)")
    mpl_title(ax, _titled(df, P, f"氨基酸组成（{len(seqs)} 条 / {tot} 残基）"), None, None)
    return fig


def ply_aacomp(df, P):
    seqs, L, _ = _seqs(df, P, max_len=100000)
    s = "".join(seqs)
    cnt = {aa: s.count(aa) for aa in c.AA20}
    tot = sum(cnt.values()) or 1
    items = sorted(cnt.items(), key=lambda kv: -kv[1])
    xs = [k for k, _ in items]
    ys = [v / tot * 100 for _, v in items]
    fig = go.Figure(go.Bar(x=xs, y=ys, marker_color=[c.aa_color(a) for a in xs],
                           hovertemplate="%{x}: %{y:.2f}%<extra></extra>"))
    return ply_style(fig, _titled(df, P, f"氨基酸组成（{len(seqs)} 条）"), None, "占比 (%)")


def ins_aacomp(df, P):
    seqs, L, _ = _seqs(df, P, max_len=100000)
    s = "".join(seqs)
    cnt = sorted(((aa, s.count(aa)) for aa in c.AA20), key=lambda kv: -kv[1])
    tot = sum(v for _, v in cnt) or 1
    top3 = "、".join(f"{aa} {v / tot * 100:.1f}%" for aa, v in cnt[:3])
    hydro = sum(v for aa, v in cnt if aa in "AVLIFWMP") / tot * 100
    charged = sum(v for aa, v in cnt if aa in "DEKRH") / tot * 100
    return f"总残基 {tot}。最多：{top3}；疏水类占 {hydro:.0f}%，带电类占 {charged:.0f}%。"


# ================= 疏水性曲线（Kyte-Doolittle） =================
def mpl_hydro(df, P):
    seqs, L, _ = _seqs(df, P, max_len=100000)
    seq = seqs[0]
    w = int(P.get("window") or 9)
    w += (w + 1) % 2  # 取奇数
    vals = np.array([KD_HYDRO.get(a, 0.0) for a in seq])
    if len(vals) < w:
        raise ValueError(f"序列长度 {len(vals)} 小于窗口 {w}")
    k = np.ones(w) / w
    sm = np.convolve(vals, k, mode="valid")
    xs = np.arange(w // 2 + 1, w // 2 + 1 + len(sm))
    fig, ax = c.prep_fig(10.5, 4.0)
    ax.plot(xs, sm, color="#4C6FE8", lw=1.8)
    ax.axhline(0, color="#94A3B8", lw=1, ls="--")
    if sm.max() > 1.6:
        ax.axhline(1.6, color="#E8834C", lw=1, ls=":")
        ax.text(len(seq), 1.65, "跨膜阈值 1.6", fontsize=8.5, color="#E8834C", ha="right")
    ax.fill_between(xs, sm, 0, where=sm > 0, color="#E8834C", alpha=0.18, interpolate=True)
    ax.fill_between(xs, sm, 0, where=sm < 0, color="#3E6FE0", alpha=0.15, interpolate=True)
    mpl_title(ax, _titled(df, P, f"疏水性曲线（Kyte-Doolittle，窗 {w}；首条序列）"), "位置", "疏水指数")
    return fig


def ply_hydro(df, P):
    seqs, L, _ = _seqs(df, P, max_len=100000)
    seq = seqs[0]
    w = int(P.get("window") or 9)
    w += (w + 1) % 2
    vals = np.array([KD_HYDRO.get(a, 0.0) for a in seq])
    k = np.ones(w) / w
    sm = np.convolve(vals, k, mode="valid")
    xs = np.arange(w // 2 + 1, w // 2 + 1 + len(sm))
    fig = go.Figure(go.Scatter(x=xs, y=sm, mode="lines", fill="tozeroy",
                               line=dict(color="#4C6FE8", width=2),
                               hovertemplate="位置 %{x}<br>疏水 %{y:.2f}<extra></extra>"))
    fig.add_hline(y=0, line=dict(color="#94A3B8", dash="dash", width=1))
    return ply_style(fig, _titled(df, P, f"疏水性曲线（窗 {w}）"), "位置", "疏水指数", legend=False)


def ins_hydro(df, P):
    seqs, L, _ = _seqs(df, P, max_len=100000)
    seq = seqs[0]
    w = int(P.get("window") or 9)
    w += (w + 1) % 2
    vals = np.array([KD_HYDRO.get(a, 0.0) for a in seq])
    sm = np.convolve(vals, np.ones(w) / w, mode="valid")
    i = int(np.argmax(sm))
    seg = "可能含跨膜/疏水核心" if sm.max() > 1.6 else "未达典型跨膜阈值（1.6）"
    return (f"首条序列 {len(seq)} 残基，窗 {w}。疏水峰位于位置 {i + w // 2}（{sm.max():.2f}），最低 {sm.min():.2f}；{seg}。")
