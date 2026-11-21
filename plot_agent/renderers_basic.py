"""通用统计图渲染器（8 种）。每个类型提供 mpl_*（PNG）与 ply_*（交互）双实现。

参数 P 为 dict；列名取 P[key]，缺失列在函数内兜底报 KeyError 由上层捕获。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import plotly.graph_objects as go

import common as c

AGG = {"mean": "均值", "sum": "总和", "median": "中位数", "count": "计数"}
_AGG_KEYS = {"均值": "mean", "总和": "sum", "中位数": "median", "计数": "count",
             "mean": "mean", "sum": "sum", "median": "median", "count": "count"}


# ---------------- plotly 公共样式 ----------------
def ply_style(fig, title="", xtitle="", ytitle="", legend=True):
    fig.update_layout(
        template="plotly_white",
        title=dict(text=title, font=dict(size=17), x=0.01),
        xaxis_title=xtitle, yaxis_title=ytitle,
        margin=dict(l=64, r=36, t=56, b=56),
        font=dict(family="system-ui,-apple-system,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif",
                  size=13, color="#333F55"),
        showlegend=legend,
        legend=dict(orientation="h", yanchor="bottom", y=1.01, x=0),
        hoverlabel=dict(font_size=12),
        coloraxis_colorbar=dict(thickness=14, len=0.85))
    fig.update_xaxes(gridcolor="#E8EDF5", zeroline=False, linecolor="#C3CBD9", ticks="outside")
    fig.update_yaxes(gridcolor="#E8EDF5", zeroline=False, linecolor="#C3CBD9", ticks="outside")
    return fig


def mpl_title(ax, title, xtitle="", ytitle=""):
    ax.set_title(title, pad=12)
    if xtitle:
        ax.set_xlabel(xtitle)
    if ytitle:
        ax.set_ylabel(ytitle)


def _titled(df, P, default=""):
    return str(P.get("_title") or default)


# ============ 1. 散点图 ============
def _scatter_frames(df, P):
    x = c.num(df[P["x"]]); y = c.num(df[P["y"]])
    ok = np.isfinite(x) & np.isfinite(y)
    color = P.get("color") or None
    if color and color not in df.columns:
        color = None
    cont = False
    if color is not None:
        cn = c.num(df[color])
        cont = cn.notna().mean() > 0.8
        color_vals = cn if cont else df[color]
    else:
        color_vals = None
    size = P.get("size") or None
    return x, y, ok, color, color_vals, cont, size


def mpl_scatter(df, P):
    x, y, ok, color, cv, cont, size = _scatter_frames(df, P)
    fig, ax = c.prep_fig()
    s = c.size_scale(df[size], 12, 170) if size else np.full(len(df), 34.0)
    if color is None:
        ax.scatter(x[ok], y[ok], s=s[ok], c="#4C6FE8", alpha=0.75, edgecolors="white", linewidths=0.4)
    elif cont:
        sc = ax.scatter(x[ok], y[ok], s=s[ok], c=cv[ok], cmap="viridis", alpha=0.85,
                        edgecolors="white", linewidths=0.3)
        cb = fig.colorbar(sc, ax=ax, pad=0.01)
        cb.set_label(str(color))
    else:
        for v, col in c.cat_colors(cv[ok]).items():
            m = (cv[ok] == v).to_numpy()
            ax.scatter(x[ok][m], y[ok][m], s=s[ok][m], c=col, alpha=0.8, label=c.short(v), edgecolors="white", linewidths=0.4)
        ax.legend(title=str(color), loc="best", title_fontsize=9)
    if P.get("trend") and P.get("trend") != "无":
        xm, ym = x[ok], y[ok]
        if len(xm) >= 3 and np.ptp(xm) > 0:
            k, b = np.polyfit(xm, ym, 1)
            xs = np.array([xm.min(), xm.max()])
            ax.plot(xs, k * xs + b, "--", color="#E8546D", lw=2, label=f"线性趋势 (r={c.corr(xm, ym):.2f})")
            ax.legend(loc="best")
    mpl_title(ax, _titled(df, P, "散点图"), P["x"], P["y"])
    return fig


def ply_scatter(df, P):
    x, y, ok, color, cv, cont, size = _scatter_frames(df, P)
    fig = go.Figure()
    s = c.size_scale(df[size], 5, 28) if size else 7
    sub = df[ok]
    xs, ys = x[ok], y[ok]
    ss = s if np.ndim(s) > 0 else np.full(ok.sum(), s)
    if color is None:
        fig.add_trace(go.Scatter(x=xs, y=ys, mode="markers", marker=dict(size=ss, color="#4C6FE8", line=dict(width=0.6, color="white")), name="数据点"))
    elif cont:
        fig.add_trace(go.Scatter(x=xs, y=ys, mode="markers", marker=dict(size=ss, color=cv[ok], colorscale="Viridis", showscale=True, colorbar=dict(title=str(color), thickness=13), line=dict(width=0.5, color="white")), name=str(color)))
    else:
        for v, col in c.cat_colors(cv[ok]).items():
            m = (cv[ok] == v).to_numpy()
            fig.add_trace(go.Scatter(x=xs[m], y=ys[m], mode="markers", name=c.short(v),
                                     marker=dict(size=ss[m], color=col, line=dict(width=0.6, color="white"))))
    if P.get("trend") and P.get("trend") != "无":
        xm, ym = xs, ys
        if len(xm) >= 3 and np.ptp(xm) > 0:
            k, b = np.polyfit(xm, ym, 1)
            xg = np.array([xm.min(), xm.max()])
            fig.add_trace(go.Scatter(x=xg, y=k * xg + b, mode="lines", name=f"线性趋势 r={c.corr(xm, ym):.2f}",
                                     line=dict(color="#E8546D", width=2, dash="dash")))
    return ply_style(fig, _titled(df, P, "散点图"), P["x"], P["y"])


def ins_scatter(df, P):
    x, y, ok, *_ = _scatter_frames(df, P)
    r = c.corr(x[ok], y[ok])
    s = f"共 {int(ok.sum())} 个有效点。"
    if r is not None:
        strength = "强" if abs(r) > 0.7 else ("中等" if abs(r) > 0.4 else "弱")
        s += f" {P['x']} 与 {P['y']} 呈{strength}{'正' if r > 0 else '负'}相关（Pearson r = {r:.3f}）。"
    return s


# ============ 2. 折线图 ============
def _line_frames(df, P):
    xcol, ycol, gcol = P["x"], P["y"], P.get("group")
    y = c.num(df[ycol])
    g = df[gcol].astype(str) if gcol and gcol in df.columns else None
    xn = c.num(df[xcol])
    is_num_x = xn.notna().mean() > 0.9
    if is_num_x:
        key = xn
        if g is not None:
            d = pd.DataFrame({"x": xn, "y": y, "g": g}).dropna()
            d = d.groupby(["g", "x"], as_index=False)["y"].mean()
            frames = [(gv, d[d.g == gv].sort_values("x")["x"].to_numpy(),
                       d[d.g == gv].sort_values("x")["y"].to_numpy()) for gv in d.g.unique()]
        else:
            d = pd.DataFrame({"x": xn, "y": y}).dropna().groupby("x", as_index=False)["y"].mean().sort_values("x")
            frames = [(ycol, d.x.to_numpy(), d.y.to_numpy())]
        return frames, is_num_x
    # 类别 x：按出现顺序，重复取均值
    if g is not None:
        d = pd.DataFrame({"x": df[xcol].astype(str), "y": y, "g": g}).dropna()
        piv = d.pivot_table(index="x", columns="g", values="y", aggfunc="mean")
        order = list(dict.fromkeys(d.x))
        piv = piv.reindex(order)
        frames = [(gv, piv.index.to_numpy(), piv[gv].to_numpy()) for gv in piv.columns]
    else:
        d = pd.DataFrame({"x": df[xcol].astype(str), "y": y}).dropna()
        d = d.groupby("x", as_index=False)["y"].mean()
        frames = [(ycol, d.x.to_numpy(), d.y.to_numpy())]
    return frames, False


def mpl_line(df, P):
    frames, is_num = _line_frames(df, P)
    fig, ax = c.prep_fig()
    colors = c.cat_colors([f[0] for f in frames])
    for name, xs, ys in frames:
        ax.plot(xs, ys, "-o", ms=3.2, lw=2, color=colors[name], label=str(name) if len(frames) > 1 else None)
    if len(frames) > 1:
        ax.legend(title=str(P.get("group") or ""), title_fontsize=9)
    if not is_num:
        ax.tick_params(axis="x", rotation=30)
        [lb.set_ha("right") for lb in ax.get_xticklabels()]
    mpl_title(ax, _titled(df, P, "折线图"), P["x"], P["y"])
    return fig


def ply_line(df, P):
    frames, is_num = _line_frames(df, P)
    fig = go.Figure()
    colors = c.cat_colors([f[0] for f in frames])
    for name, xs, ys in frames:
        fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines+markers", name=c.short(name),
                                 marker=dict(size=6), line=dict(width=2.2, color=colors[name]),
                                 hovertemplate=f"{c.short(name)}<br>%{{x}}: %{{y:.4g}}<extra></extra>"))
    return ply_style(fig, _titled(df, P, "折线图"), P["x"], P["y"])


def ins_line(df, P):
    frames, is_num = _line_frames(df, P)
    s = f"{len(frames)} 条曲线；"
    if is_num and len(frames) == 1:
        _, xs, ys = frames[0]
        if len(xs) >= 3 and np.ptp(xs) > 0:
            k = np.polyfit(xs, ys, 1)[0]
            s += f"整体{'上升' if k > 0 else '下降'}趋势（斜率 {k:.3g}）。"
    if df[P["x"]].duplicated().any():
        s += "重复 x 值已按均值聚合。"
    return s


# ============ 3. 柱状图 ============
def _bar_frame(df, P):
    xcol, ycol, gcol = P["x"], P.get("y"), P.get("group")
    aggfn = _AGG_KEYS.get(P.get("agg") or "均值", "mean")
    dd = pd.DataFrame({"x": df[xcol].astype(str)})
    if ycol and ycol in df.columns:
        dd["y"] = c.num(df[ycol])
    else:
        dd["y"] = 1.0
        aggfn = "count"
    if gcol and gcol in df.columns:
        dd["g"] = df[gcol].astype(str)
        piv = dd.pivot_table(index="x", columns="g", values="y", aggfunc=aggfn, observed=True)
    else:
        piv = dd.groupby("x")["y"].agg(aggfn).to_frame(ycol or "计数")
        piv.columns = [ycol or "计数"]
    sort = P.get("sort") or "默认"
    if sort in ("按数值降序", "按数值升序"):
        tot = piv.sum(axis=1)
        order = tot.sort_values(ascending=(sort == "按数值升序")).index
        piv = piv.reindex(order)
    if len(piv) > 120:
        raise ValueError(f"类别过多（{len(piv)} 类）。柱状图最多 120 类，请先聚合或筛选（如取 Top N）")
    return piv, aggfn


def mpl_bar(df, P):
    piv, aggfn = _bar_frame(df, P)
    horizontal = bool(P.get("horizontal"))
    fig, ax = c.prep_fig(8.4, 5.2 if len(piv) <= 30 else 7.5)
    x = np.arange(len(piv.index))
    cols = c.cat_colors(piv.columns)
    if P.get("group") and P.get("stacked"):
        pos = np.zeros(len(piv)); neg = np.zeros(len(piv))
        for gcolname in piv.columns:
            v = piv[gcolname].to_numpy()
            mask = v >= 0
            b = np.where(mask, pos, neg)
            if horizontal:
                ax.barh(x, v, left=b, color=cols[gcolname], label=str(gcolname), height=0.72)
            else:
                ax.bar(x, v, bottom=b, color=cols[gcolname], label=str(gcolname), width=0.72)
            pos = np.where(mask, pos + np.where(mask, v, 0), pos)
            neg = np.where(~mask, neg + np.where(~mask, v, 0), neg)
        ax.legend(title=str(P["group"]), title_fontsize=9)
    elif P.get("group"):
        w = 0.8 / len(piv.columns)
        for i, gname in enumerate(piv.columns):
            if horizontal:
                ax.barh(x + i * w - 0.4 + w / 2, piv[gname], height=w * 0.92, color=cols[gname], label=str(gname))
            else:
                ax.bar(x + i * w - 0.4 + w / 2, piv[gname], width=w * 0.92, color=cols[gname], label=str(gname))
        ax.legend(title=str(P["group"]), title_fontsize=9)
    else:
        if horizontal:
            ax.barh(x, piv.iloc[:, 0], color="#4C6FE8", height=0.72)
        else:
            ax.bar(x, piv.iloc[:, 0], color="#4C6FE8", width=0.72)
    lbl = f"{AGG.get(aggfn, aggfn)}({P.get('y') or '计数'})"
    if horizontal:
        ax.set_yticks(x, [c.short(v, 30) for v in piv.index], fontsize=9)
        ax.set_xlabel(lbl)
        if len(piv) > 12:
            fig.set_figheight(min(max(4.5, len(piv) * 0.32), 14))
    else:
        ax.set_xticks(x, [c.short(v, 14) for v in piv.index], fontsize=9, rotation=30)
        [lb.set_ha("right") for lb in ax.get_xticklabels()]
        ax.set_ylabel(lbl)
    mpl_title(ax, _titled(df, P, "柱状图"), None if not horizontal else P["x"], lbl if not horizontal else None)
    return fig


def ply_bar(df, P):
    piv, aggfn = _bar_frame(df, P)
    horizontal = bool(P.get("horizontal"))
    fig = go.Figure()
    cols = c.cat_colors(piv.columns)
    if P.get("group") and P.get("stacked"):
        barmode = "relative"  # 相对堆叠可正确处理正负值
    elif P.get("group"):
        barmode = "group"
    else:
        barmode = "overlay"
    for gname in piv.columns:
        fig.add_trace(go.Bar(
            x=piv.index if not horizontal else piv[gname],
            y=piv[gname] if not horizontal else piv.index,
            orientation="v" if not horizontal else "h",
            name=c.short(str(gname)), marker_color=cols[gname],
            hovertemplate=f"{c.short(str(gname))}<br>%{{x}}: %{{y:.4g}}<extra></extra>"))
    fig.update_layout(barmode=barmode)
    lbl = f"{AGG.get(aggfn, aggfn)}({P.get('y') or '计数'})"
    tickangle = -30 if (not horizontal and len(piv) > 8) else 0
    fig.update_xaxes(tickangle=tickangle, automargin=True)
    if horizontal:
        fig.update_layout(xaxis_title=lbl, yaxis_title=str(P["x"]))
        fig.update_yaxes(type="category")
    else:
        fig.update_layout(xaxis_title=str(P["x"]), yaxis_title=lbl)
    return ply_style(fig, _titled(df, P, "柱状图"))


def ins_bar(df, P):
    piv, aggfn = _bar_frame(df, P)
    tot = piv.sum(axis=1)
    top = tot.idxmax()
    s = f"共 {len(piv)} 个类别，聚合方式 = {AGG.get(aggfn, aggfn)}。最大：「{c.short(top, 20)}」（{c.fmt(tot.max())}）"
    if len(piv) >= 3:
        s += f"；前二合计占 {tot.nlargest(2).sum() / max(tot.sum(), 1e-9) * 100:.0f}%。"
    return s


# ============ 4. 箱线图 / 5. 小提琴 ============
def _box_groups(df, P):
    ycol, xcol = P["y"], P.get("x")
    y = c.num(df[ycol])
    if xcol and xcol in df.columns:
        g = df[xcol].astype(str)
        order = list(pd.unique(g.dropna()))[:24]
        groups = [(v, y[g == v].dropna().to_numpy()) for v in order]
        return groups, xcol
    return [("全部数据", y.dropna().to_numpy())], None


def mpl_box(df, P):
    groups, xcol = _box_groups(df, P)
    fig, ax = c.prep_fig()
    data = [g for _, g in groups if len(g)]
    labels = [c.short(k, 18) for k, g in groups if len(g)]
    colors = c.cat_colors([k for k, g in groups if len(g)])
    bp = ax.boxplot(data, tick_labels=labels, notch=bool(P.get("notch")), patch_artist=True,
                    medianprops=dict(color="#1F2937", lw=1.6),
                    flierprops=dict(marker="o", ms=3.5, mfc="#94A3B8", mec="none", alpha=0.7),
                    widths=0.62)
    for patch, (k, _) in zip(bp["boxes"], [g for g in groups if len(g[1])]):
        patch.set_facecolor(colors[k]); patch.set_alpha(0.65); patch.set_edgecolor("#64748B")
    ax.tick_params(axis="x", rotation=30)
    [lb.set_ha("right") for lb in ax.get_xticklabels()]
    mpl_title(ax, _titled(df, P, "箱线图"), xcol, P["y"])
    return fig


def mpl_violin(df, P):
    groups, xcol = _box_groups(df, P)
    fig, ax = c.prep_fig()
    data = [g for _, g in groups if len(g) >= 3]
    labels = [c.short(k, 18) for k, g in groups if len(g) >= 3]
    colors = c.cat_colors([k for k, g in groups if len(g) >= 3])
    if not data:
        raise ValueError("每组至少需要 3 个数值才能画小提琴图")
    vp = ax.violinplot(data, showmedians=True, widths=0.82)
    for body, (k, _) in zip(vp["bodies"], [g for g in groups if len(g[1]) >= 3]):
        body.set_facecolor(colors[k]); body.set_alpha(0.6); body.set_edgecolor("none")
    vp["cmedians"].set_color("#1F2937")
    ax.set_xticks(range(1, len(labels) + 1), labels, rotation=30, ha="right")
    mpl_title(ax, _titled(df, P, "小提琴图"), xcol, P["y"])
    return fig


def ply_violin(df, P):
    groups, xcol = _box_groups(df, P)
    fig = go.Figure()
    colors = c.cat_colors([k for k, _ in groups])
    for k, g in groups:
        if len(g) < 3:
            continue
        fig.add_trace(go.Violin(y=g, name=c.short(k, 20), marker_color=colors[k],
                                box_visible=True, meanline_visible=True, points="outliers", spanmode="hard"))
    if not fig.data:
        raise ValueError("每组至少需要 3 个数值才能画小提琴图")
    return ply_style(fig, _titled(df, P, "小提琴图"), xcol, P["y"], legend=bool(xcol))


def ply_box(df, P):
    groups, xcol = _box_groups(df, P)
    fig = go.Figure()
    colors = c.cat_colors([k for k, _ in groups])
    for k, g in groups:
        if not len(g):
            continue
        fig.add_trace(go.Box(y=g, name=c.short(k, 20), marker_color=colors[k],
                             notched=bool(P.get("notch")), boxpoints="outliers", jitter=0.3))
    return ply_style(fig, _titled(df, P, "箱线图"), xcol, P["y"], legend=bool(xcol))


def ins_box(df, P):
    groups, _ = _box_groups(df, P)
    n_out = 0
    parts = []
    for k, g in groups[:6]:
        if len(g) < 4:
            continue
        q1, q3 = np.percentile(g, [25, 75])
        iqr = q3 - q1
        out = int(((g < q1 - 1.5 * iqr) | (g > q3 + 1.5 * iqr)).sum())
        n_out += out
        parts.append(f"{c.short(k, 12)}(n={len(g)}, 中位 {c.fmt(np.median(g), 4)})")
    s = f"{len(groups)} 组；" + "；".join(parts)
    if n_out:
        s += f"。1.5×IQR 规则下共 {n_out} 个离群点。"
    return s


# ============ 6. 直方图 ============
def mpl_histogram(df, P):
    x = c.num(df[P["x"]]).dropna().to_numpy()
    bins = int(P.get("bins") or 30)
    hue = P.get("hue") or None
    fig, ax = c.prep_fig()
    if hue and hue in df.columns and df[hue].nunique() <= 8:
        g = df[hue].astype(str)
        colors = c.cat_colors(g.dropna().unique())
        for v, col in colors.items():
            d = c.num(df.loc[g == v, P["x"]]).dropna().to_numpy()
            if not len(d):
                continue
            ax.hist(d, bins=bins, histtype="stepfilled", alpha=0.5, density=True, color=col, label=c.short(v, 20), edgecolor=col, lw=1)
        ax.legend(title=str(hue), title_fontsize=9)
        ax.set_ylabel("概率密度")
    else:
        ax.hist(x, bins=bins, color="#4C6FE8", edgecolor="white", lw=0.6, alpha=0.9)
        ax.set_ylabel("频数")
    mpl_title(ax, _titled(df, P, "直方图"), P["x"], None)
    return fig


def ply_histogram(df, P):
    x = c.num(df[P["x"]])
    bins = int(P.get("bins") or 30)
    hue = P.get("hue") or None
    fig = go.Figure()
    mn, mx = np.nanmin(x), np.nanmax(x)
    size = (mx - mn) / bins if np.isfinite(mn) and mx > mn else 1.0
    if hue and hue in df.columns and df[hue].nunique() <= 8:
        g = df[hue].astype(str)
        colors = c.cat_colors(g.dropna().unique())
        for v, col in colors.items():
            d = x[g == v].dropna()
            if not len(d):
                continue
            fig.add_trace(go.Histogram(x=d, histnorm="probability density", name=c.short(v, 20),
                                       xbins=dict(start=mn, end=mx, size=size),
                                       marker=dict(color=col, opacity=0.62, line=dict(width=0.8, color=col))))
        fig.update_layout(barmode="overlay", yaxis_title="概率密度")
    else:
        fig.add_trace(go.Histogram(x=x.dropna(), xbins=dict(start=mn, end=mx, size=size),
                                   marker=dict(color="#4C6FE8", line=dict(width=0.7, color="white"))))
        fig.update_layout(yaxis_title="频数")
    return ply_style(fig, _titled(df, P, "直方图"), P["x"], legend=bool(hue))


def ins_histogram(df, P):
    x = c.num(df[P["x"]]).dropna()
    q1, med, q3 = x.quantile([0.25, 0.5, 0.75])
    skew = "右偏（长尾在右）" if x.mean() > med * 1.05 else ("左偏（长尾在左）" if x.mean() < med * 0.95 else "近似对称")
    return (f"n={len(x)}，均值 {c.fmt(x.mean(), 4)}，中位数 {c.fmt(med, 4)}，标准差 {c.fmt(x.std(), 4)}，"
            f"IQR [{c.fmt(q1, 4)}, {c.fmt(q3, 4)}]，分布{skew}。")


# ============ 7. 二维密度（KDE 等高线） ============
def mpl_kde2d(df, P):
    g = c.kde_grid(df[P["x"]], df[P["y"]], bins=int(P.get("bins") or 90))
    if g is None:
        raise ValueError("有效数值点太少（<8）无法估计密度")
    xi, yi, Z = g
    fig, ax = c.prep_fig()
    if P.get("filled", True):
        cf = ax.contourf(xi, yi, Z, levels=14, cmap="viridis")
        fig.colorbar(cf, ax=ax, pad=0.01, label="核密度")
    else:
        ax.contour(xi, yi, Z, levels=10, cmap="viridis")
    if P.get("points", True):
        x = c.num(df[P["x"]]); y = c.num(df[P["y"]])
        ok = np.isfinite(x) & np.isfinite(y)
        ax.scatter(x[ok], y[ok], s=5, c="white", alpha=0.25, lw=0)
    iy, ix = np.unravel_index(np.argmax(Z), Z.shape)
    ax.plot(xi[ix], yi[iy], "P", ms=12, color="#E8546D", mec="white")
    ax.annotate(f"密度峰 ({c.fmt(xi[ix], 3)}, {c.fmt(yi[iy], 3)})", (xi[ix], yi[iy]),
                textcoords="offset points", xytext=(8, 8), fontsize=10)
    mpl_title(ax, _titled(df, P, "二维核密度"), P["x"], P["y"])
    return fig


def ply_kde2d(df, P):
    x = c.num(df[P["x"]]); y = c.num(df[P["y"]])
    ok = np.isfinite(x) & np.isfinite(y)
    fig = go.Figure()
    fig.add_trace(go.Histogram2dContour(x=x[ok], y=y[ok], colorscale="Viridis", showscale=True,
                                        ncontours=14, contours_coloring="heatmap", line=dict(width=0.6),
                                        colorbar=dict(title="核密度", thickness=13)))
    if P.get("points", True):
        fig.add_trace(go.Scatter(x=x[ok], y=y[ok], mode="markers", name="数据点",
                                 marker=dict(size=3, color="rgba(255,255,255,0.35)")))
    return ply_style(fig, _titled(df, P, "二维核密度"), P["x"], P["y"])


def ins_kde2d(df, P):
    g = c.kde_grid(df[P["x"]], df[P["y"]])
    if g is None:
        return "有效数值点太少，密度估计不可靠。"
    xi, yi, Z = g
    iy, ix = np.unravel_index(np.argmax(Z), Z.shape)
    x = c.num(df[P["x"]]); y = c.num(df[P["y"]])
    ok = np.isfinite(x) & np.isfinite(y)
    r = c.corr(x[ok], y[ok])
    s = f"密度峰位于 ({c.fmt(xi[ix], 3)}, {c.fmt(yi[iy], 3)})，n={int(ok.sum())}。"
    if r is not None:
        s += f" 两变量相关 r = {r:.3f}。"
    return s


# ============ 8. 相关性热图 ============
def _corr_mat(df, P):
    nums = df.select_dtypes(include=[np.number])
    nums = nums.loc[:, nums.apply(lambda s: c.num(s).notna().mean() > 0.8, axis=0)]
    if nums.shape[1] < 2:
        raise ValueError("数值列不足 2 列，无法计算相关矩阵")
    method = P.get("method") or "pearson"
    m = nums.corr(method=method)
    return m


def mpl_corrheat(df, P):
    m = _corr_mat(df, P)
    fig, ax = c.prep_fig(8.4, 6.6)
    im = ax.imshow(m.to_numpy(), cmap="RdBu_r", vmin=-1, vmax=1)
    fig.colorbar(im, ax=ax, pad=0.01, label="相关系数", shrink=0.85)
    n = len(m.columns)
    ax.set_xticks(range(n), m.columns, rotation=45, ha="right", fontsize=max(7, 10 - n // 6))
    ax.set_yticks(range(n), m.columns, fontsize=max(7, 10 - n // 6))
    ax.grid(False)
    if n <= 14 and bool(P.get("annot", True)):
        for i in range(n):
            for j in range(n):
                v = m.iloc[i, j]
                ax.text(j, i, f"{v:.2f}", ha="center", va="center",
                        fontsize=max(6.5, 9 - n // 10), color="white" if abs(v) > 0.55 else "#1F2937")
    mpl_title(ax, _titled(df, P, f"相关性热图（{P.get('method') or 'pearson'}）"))
    return fig


def ply_corrheat(df, P):
    m = _corr_mat(df, P)
    fig = go.Figure(go.Heatmap(z=m.to_numpy(), x=m.columns.astype(str), y=m.columns.astype(str),
                               zmin=-1, zmax=1, colorscale="RdBu", reversescale=True, zmid=0,
                               colorbar=dict(title="r", thickness=13),
                               hovertemplate="%{y} × %{x}<br>r = %{z:.3f}<extra></extra>"))
    fig.update_yaxes(autorange="reversed")
    return ply_style(fig, _titled(df, P, f"相关性热图（{P.get('method') or 'pearson'}）"), legend=False)


def ins_corrheat(df, P):
    m = _corr_mat(df, P)
    mm = m.abs().to_numpy()
    np.fill_diagonal(mm, 0)
    i, j = np.unravel_index(np.argmax(mm), mm.shape)
    v = m.iloc[i, j]
    k = int((mm > 0).sum() / 2)
    return f"{len(m.columns)} 个数值列共 {k} 对。最强相关：{m.columns[i]} × {m.columns[j]}（r = {v:.3f}，{'正' if v > 0 else '负'}）。"
