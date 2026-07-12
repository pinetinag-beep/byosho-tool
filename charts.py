"""
Plotlyチャート生成モジュール
"""
import plotly.graph_objects as go
import plotly.express as px
import pandas as pd
import numpy as np
from data_processor import BED_TYPES, BED_COLORS


def _fig_layout(fig, title="", height=420):
    fig.update_layout(
        title=dict(text=title, font=dict(size=15)),
        height=height,
        margin=dict(l=10, r=10, t=50, b=10),
        font=dict(family="Meiryo, sans-serif", size=14),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        # ドラッグ操作でのボックスズームを無効化。年度数点だけの折れ線/棒
        # グラフはズーム探索を想定しておらず、誤ってドラッグすると軸が
        # 意味不明な範囲にズームされグラフが壊れたように見えるため。
        dragmode=False,
    )
    return fig


# ── 個別病院チャート ────────────────────────────────────────


def bed_donut(row: pd.Series, hospital_name: str) -> go.Figure:
    """病床種別ドーナツチャート"""
    labels, values, colors = [], [], []
    for t in BED_TYPES:
        v = row.get(f"{t}_許可病床数", 0)
        if v > 0:
            labels.append(t)
            values.append(v)
            colors.append(BED_COLORS[t])

    fig = go.Figure(go.Pie(
        labels=labels, values=values,
        hole=0.55,
        marker_colors=colors,
        textinfo="label+percent",
        hovertemplate="%{label}: %{value}床 (%{percent})<extra></extra>",
    ))
    total = sum(values)
    fig.add_annotation(text=f"<b>{total:,}床</b>", x=0.5, y=0.5,
                       font_size=21, showarrow=False)
    return _fig_layout(fig, f"病床種別構成 — {hospital_name}")


def occupancy_gauge(rate: float, label: str) -> go.Figure:
    """稼働率ゲージ"""
    fig = go.Figure(go.Indicator(
        mode="gauge+number",
        value=round(rate * 100, 1),
        number={"suffix": "%", "font": {"size": 34}},
        gauge={
            "axis": {"range": [0, 100], "ticksuffix": "%"},
            "bar": {"color": "#3498db"},
            "steps": [
                {"range": [0, 50],  "color": "rgba(52,152,219,0.08)"},
                {"range": [50, 100], "color": "rgba(52,152,219,0.15)"},
            ],
        },
        title={"text": label, "font": {"size": 15}},
    ))
    return _fig_layout(fig, height=280)


def bed_type_occupancy_bar(row: pd.Series, hospital_name: str) -> go.Figure:
    """種別ごとの稼働率横棒グラフ（在棟患者延べ数/365/許可病床数で計算）"""
    rates, colors, labels = [], [], []
    for t in BED_TYPES:
        kyoka = float(row.get(f"{t}_許可病床数", 0) or 0)
        zaitou = float(row.get(f"{t}_在棟延べ数", 0) or 0)
        kado   = float(row.get(f"{t}_稼働病床数", 0) or 0)
        if kyoka > 0:
            if zaitou > 0:
                rate = round(zaitou / 365 / kyoka * 100, 1)
            else:
                rate = round(kado / kyoka * 100, 1)
            rates.append(rate)
            colors.append(BED_COLORS[t])
            labels.append(t)

    fig = go.Figure(go.Bar(
        x=rates, y=labels, orientation="h",
        marker_color=colors,
        text=[f"{r:.1f}%" for r in rates],
        textposition="auto",
        hovertemplate="%{y}: %{x:.1f}%<extra></extra>",
    ))
    fig.update_xaxes(range=[0, 105], ticksuffix="%")
    return _fig_layout(fig, f"病床種別稼働率 — {hospital_name}")


# ── 地域比較チャート ────────────────────────────────────────


def regional_bed_comparison(region_df: pd.DataFrame, highlight: str) -> go.Figure:
    """地域内病院の病床種別積み上げ棒グラフ"""
    df = region_df.sort_values("合計_許可病床数", ascending=True).tail(20)
    fig = go.Figure()
    for t in BED_TYPES:
        col = f"{t}_許可病床数"
        if col in df.columns:
            fig.add_trace(go.Bar(
                name=t,
                x=df[col],
                y=df["医療機関名"],
                orientation="h",
                marker_color=BED_COLORS[t],
                hovertemplate=f"{t}: %{{x:,}}床<extra>%{{y}}</extra>",
            ))

    # 選択病院名に ◀ を付けてハイライト
    tick_labels = [f"<b>▶ {n}</b>" if n == highlight else n for n in df["医療機関名"]]
    fig.update_layout(
        barmode="stack",
        yaxis=dict(
            tickvals=list(df["医療機関名"]),
            ticktext=tick_labels,
        ),
    )
    return _fig_layout(fig, "地域内病院 病床数比較（上位20院）", height=max(380, len(df) * 28))


def occupancy_scatter(region_df: pd.DataFrame, highlight: str) -> go.Figure:
    """病床規模 vs 稼働率 散布図"""
    df = region_df.copy()
    # 在棟延べ数があれば正式な稼働率を使用、なければ最大使用病床数ベース
    if "合計_在棟延べ数" in df.columns and df["合計_在棟延べ数"].sum() > 0:
        df["稼働率(%)"] = (
            df["合計_在棟延べ数"] / 365 / df["合計_許可病床数"].replace(0, np.nan) * 100
        ).round(1)
    elif "合計稼働率" in df.columns:
        df["稼働率(%)"] = (df["合計稼働率"] * 100).round(1)
    else:
        df["稼働率(%)"] = (df["合計_稼働病床数"] / df["合計_許可病床数"].replace(0, np.nan) * 100).round(1)
    df["is_target"] = df["医療機関名"] == highlight

    fig = px.scatter(
        df, x="合計_許可病床数", y="稼働率(%)",
        color="is_target",
        color_discrete_map={True: "#e74c3c", False: "#95a5a6"},
        size="合計_許可病床数",
        size_max=28,
        hover_name="医療機関名",
        hover_data={"is_target": False, "合計_許可病床数": True, "稼働率(%)": True},
        labels={"合計_許可病床数": "許可病床数（床）", "稼働率(%)": "総稼働率（%）"},
    )
    fig.update_layout(showlegend=False)
    return _fig_layout(fig, "病床規模 vs 稼働率（地域内比較）")


def share_bar(region_df: pd.DataFrame, highlight: str) -> go.Figure:
    """地域内病床シェア棒グラフ"""
    df = region_df.sort_values("地域シェア(%)", ascending=True).tail(15)
    colors = ["#e74c3c" if n == highlight else "#3498db" for n in df["医療機関名"]]
    fig = go.Figure(go.Bar(
        x=df["地域シェア(%)"], y=df["医療機関名"],
        orientation="h",
        marker_color=colors,
        text=df["地域シェア(%)"].apply(lambda v: f"{v:.1f}%"),
        textposition="auto",
        hovertemplate="%{y}: %{x:.1f}%<extra></extra>",
    ))
    fig.update_xaxes(ticksuffix="%")
    return _fig_layout(fig, "地域内病床シェア", height=max(350, len(df) * 26))


_COL_FMT = {
    "合計_許可病床数":  lambda s: pd.to_numeric(s, errors="coerce").fillna(0).astype(int).apply(lambda x: f"{x:,}"),
    "合計_稼働病床数":  lambda s: pd.to_numeric(s, errors="coerce").fillna(0).astype(int).apply(lambda x: f"{x:,}"),
    "地域シェア(%)":    lambda s: pd.to_numeric(s, errors="coerce").round(1).astype(str) + "%",
    "合計稼働率":       lambda s: (pd.to_numeric(s, errors="coerce") * 100).round(1).astype(str) + "%",
    "常勤医師数":       lambda s: pd.to_numeric(s, errors="coerce").fillna(0).astype(int).apply(lambda x: f"{x:,}"),
    "常勤看護師数":     lambda s: pd.to_numeric(s, errors="coerce").fillna(0).astype(int).apply(lambda x: f"{x:,}"),
    "医師数_per100床":  lambda s: pd.to_numeric(s, errors="coerce").round(1).astype(str),
    "看護師数_per100床": lambda s: pd.to_numeric(s, errors="coerce").round(1).astype(str),
    "救急搬送件数":     lambda s: pd.to_numeric(s, errors="coerce").fillna(0).astype(int).apply(lambda x: f"{x:,}"),
    "CT台数":           lambda s: pd.to_numeric(s, errors="coerce").fillna(0).astype(int).apply(lambda x: f"{x:,}"),
    "MRI台数":          lambda s: pd.to_numeric(s, errors="coerce").fillna(0).astype(int).apply(lambda x: f"{x:,}"),
}


def ranking_table_fig(
    region_df: pd.DataFrame,
    highlight: str,
    rank_col: str = "合計_許可病床数",
    show_cols: list | None = None,
    col_labels: list | None = None,
) -> go.Figure:
    """地域内順位テーブル（ランキング列選択対応）"""
    if show_cols is None:
        show_cols = ["合計_許可病床数", "合計_稼働病床数", "地域シェア(%)", "合計稼働率"]
    if col_labels is None:
        col_labels = ["許可病床数", "稼働病床数", "地域シェア", "稼働率"]

    df = region_df.copy()
    df["_rank"] = pd.to_numeric(df[rank_col], errors="coerce").rank(
        ascending=False, method="min", na_option="bottom"
    ).astype(int)
    df = df.sort_values("_rank").reset_index(drop=True)

    # format display columns
    disp = pd.DataFrame()
    disp["_rank"] = df["_rank"]
    disp["医療機関名"] = df["医療機関名"]
    for c in show_cols:
        if c in df.columns:
            fmt = _COL_FMT.get(c, lambda s: s.astype(str))
            disp[c] = fmt(df[c])
        else:
            disp[c] = "—"

    n = len(disp)
    is_hl = [name == highlight for name in disp["医療機関名"]]

    fill_colors = [
        ["#c0392b" if h else ("#f2f2f2" if i % 2 == 0 else "white") for i, h in enumerate(is_hl)]
    ] * len(disp.columns)
    font_colors = [
        ["white" if h else "#2c3e50" for h in is_hl]
    ] * len(disp.columns)

    all_labels = ["順位", "医療機関名"] + col_labels
    col_widths = [55, 220] + [95] * len(col_labels)
    align = ["center", "left"] + ["center"] * len(col_labels)

    fig = go.Figure(go.Table(
        columnwidth=col_widths,
        header=dict(
            values=all_labels,
            fill_color="#2c3e50",
            font=dict(color="white", size=14),
            align="center",
            height=34,
        ),
        cells=dict(
            values=[disp[c] for c in disp.columns],
            fill_color=fill_colors,
            font=dict(color=font_colors, size=14),
            align=align,
            height=30,
        ),
    ))
    return _fig_layout(fig, "地域内ランキング", height=max(340, n * 32 + 90))


# ── トレンドチャート ─────────────────────────────────────────


def trend_beds(trend_df: pd.DataFrame, hospital_name: str) -> go.Figure:
    """病床数推移（種別別積み上げ面グラフ）"""
    fig = go.Figure()
    for t in BED_TYPES:
        col = f"{t}_許可病床数"
        if col in trend_df.columns:
            fig.add_trace(go.Scatter(
                x=trend_df["報告年度"], y=trend_df[col],
                name=t, mode="lines+markers",
                fill="tonexty" if t != BED_TYPES[0] else "tozeroy",
                line_color=BED_COLORS[t],
                hovertemplate=f"{t}: %{{y:,}}床<extra></extra>",
            ))
    return _fig_layout(fig, f"病床数推移 — {hospital_name}")


def trend_occupancy(trend_df: pd.DataFrame, hospital_name: str) -> go.Figure:
    """稼働率推移（在棟患者延べ数/365/許可病床数ベース）"""
    fig = go.Figure()

    kyoka = trend_df["合計_許可病床数"].replace(0, np.nan)
    if "合計_在棟延べ数" in trend_df.columns and trend_df["合計_在棟延べ数"].sum() > 0:
        occ_pct = (trend_df["合計_在棟延べ数"] / 365 / kyoka * 100).round(1)
        label = "総稼働率（在棟延べ数ベース）"
    else:
        occ_pct = (trend_df["合計_稼働病床数"] / kyoka * 100).round(1)
        label = "総稼働率"

    fig.add_trace(go.Scatter(
        x=trend_df["報告年度"],
        y=occ_pct,
        mode="lines+markers+text",
        name=label,
        line=dict(color="#3498db", width=3),
        marker=dict(size=9),
        text=occ_pct.astype(str) + "%",
        textposition="top center",
    ))
    fig.update_yaxes(ticksuffix="%", range=[0, max(105, occ_pct.max() + 5) if not occ_pct.empty else 105])
    return _fig_layout(fig, f"総稼働率推移 — {hospital_name}")


def trend_los(los_df: pd.DataFrame, hospital_name: str) -> go.Figure:
    """平均在院日数推移"""
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=los_df["報告年度"], y=los_df["平均在院日数"],
        mode="lines+markers+text",
        name="平均在院日数",
        line=dict(color="#f39c12", width=3),
        marker=dict(size=9),
        text=los_df["平均在院日数"].map(lambda v: f"{v:.1f}日" if pd.notna(v) else ""),
        textposition="top center",
        hovertemplate="%{y:.1f}日<extra></extra>",
    ))
    fig.update_yaxes(ticksuffix="日")
    return _fig_layout(fig, f"平均在院日数推移 — {hospital_name}")


def trend_dpc_cases(dpc_trend_df: pd.DataFrame, hospital_name: str) -> go.Figure:
    """DPC症例数（MDC別患者件数合計）推移"""
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=dpc_trend_df["年度"], y=dpc_trend_df["DPC症例数"],
        name="DPC症例数", marker_color="#8b5cf6", opacity=0.85,
        text=dpc_trend_df["DPC症例数"].map(lambda v: f"{v:,.0f}例"),
        textposition="outside",
        hovertemplate="%{y:,.0f}例<extra></extra>",
    ))
    return _fig_layout(fig, f"DPC症例数推移 — {hospital_name}")


def trend_staff(trend_df: pd.DataFrame, hospital_name: str) -> go.Figure:
    """スタッフ数推移"""
    fig = go.Figure()
    if "常勤医師数" in trend_df.columns:
        fig.add_trace(go.Bar(
            x=trend_df["報告年度"], y=trend_df["常勤医師数"],
            name="常勤医師数", marker_color="#e74c3c", opacity=0.8,
        ))
    if "常勤看護師数" in trend_df.columns:
        fig.add_trace(go.Bar(
            x=trend_df["報告年度"], y=trend_df["常勤看護師数"],
            name="常勤看護師数", marker_color="#3498db", opacity=0.8,
        ))
    fig.update_layout(barmode="group")
    return _fig_layout(fig, f"スタッフ数推移 — {hospital_name}")


# ── スタッフ分析チャート ──────────────────────────────────────


def staff_scatter(region_df: pd.DataFrame, highlight: str) -> go.Figure:
    """医師数/看護師数 vs 病床数 散布図"""
    df = region_df.copy()
    if "医師数_per100床" not in df.columns or "看護師数_per100床" not in df.columns:
        return go.Figure()
    df["is_target"] = df["医療機関名"] == highlight

    fig = px.scatter(
        df, x="医師数_per100床", y="看護師数_per100床",
        color="is_target",
        color_discrete_map={True: "#e74c3c", False: "#95a5a6"},
        size="合計_許可病床数",
        size_max=30,
        hover_name="医療機関名",
        hover_data={"is_target": False, "医師数_per100床": True, "看護師数_per100床": True, "合計_許可病床数": True},
        labels={"医師数_per100床": "医師数（per 100床）", "看護師数_per100床": "看護師数（per 100床）"},
    )
    fig.update_layout(showlegend=False)
    return _fig_layout(fig, "医療スタッフ配置密度（地域内比較）")


def staff_bar_region(region_df: pd.DataFrame, highlight: str, col: str, label: str) -> go.Figure:
    """地域内スタッフ比率棒グラフ"""
    if col not in region_df.columns:
        return go.Figure()
    df = region_df.dropna(subset=[col]).sort_values(col, ascending=True).tail(15)
    colors = ["#e74c3c" if n == highlight else "#3498db" for n in df["医療機関名"]]
    fig = go.Figure(go.Bar(
        x=df[col], y=df["医療機関名"],
        orientation="h",
        marker_color=colors,
        text=df[col].round(1),
        textposition="auto",
    ))
    return _fig_layout(fig, f"{label}（per 100床）", height=max(320, len(df) * 26))


# ── 詳細分析チャート ─────────────────────────────────────────


def detail_bed_type_table(ward_df: pd.DataFrame, hospital_name: str) -> pd.DataFrame:
    """
    選択病院の入院基本料別病床テーブルを返す（st.dataframe で表示する用）。
    ward_df は病棟単位 DataFrame。
    """
    sub = ward_df[ward_df["医療機関名"] == hospital_name].copy()
    if sub.empty:
        return pd.DataFrame()

    rows = []
    for _, r in sub.iterrows():
        kyoka    = float(r.get("許可病床数", 0) or 0)
        max_use  = float(r.get("最大使用病床数", 0) or 0)
        zaitou   = float(r.get("在棟延べ数", 0) or 0)
        nyutou   = float(r.get("新規入棟患者数", 0) or 0)
        if zaitou > 0 and kyoka > 0:
            rate = round(zaitou / 365 / kyoka * 100, 1)
        elif kyoka > 0:
            rate = round(max_use / kyoka * 100, 1)
        else:
            rate = 0.0
        zaitou_per_day = zaitou / 365 if zaitou > 0 else max_use
        taitou  = float(r.get("退棟患者数", 0) or 0)
        denom   = (nyutou + taitou) / 2
        avg_los = round(zaitou / denom, 1) if zaitou > 0 and denom > 0 else None
        rows.append({
            "機能区分":          r.get("機能区分", ""),
            "入院基本料":        r.get("入院基本料", ""),
            "届出病床数":        f"{int(r.get('届出病床数', 0) or 0)}床",
            "許可病床数":        f"{int(kyoka)}床",
            "平均在棟患者数/日": f"{zaitou_per_day:.1f}人",
            "平均在院日数":      f"{avg_los}日" if avg_los is not None else "—",
            "稼働率":            f"{rate}%",
        })
    return pd.DataFrame(rows)


def admission_route_pie(ward_df: pd.DataFrame, hospital_name: str) -> go.Figure:
    """
    入院経路の円グラフ。
    予定外救急入院 / 予定外非救急 / 予定入院・院内転棟 の内訳。
    """
    sub = ward_df[ward_df["医療機関名"] == hospital_name]
    if sub.empty:
        fig = go.Figure()
        fig.add_annotation(text="データなし", x=0.5, y=0.5, showarrow=False)
        return _fig_layout(fig, "入院経路")

    total_new  = float(sub["新規入棟患者数"].sum())
    kyukyu     = float(sub["救急入院患者数"].sum())
    other      = max(total_new - kyukyu, 0)

    labels = ["予定外救急入院", "予定・院内転棟等"]
    values = [kyukyu, other]
    colors = ["#e74c3c", "#3498db"]

    fig = go.Figure(go.Pie(
        labels=labels, values=values,
        hole=0.45,
        marker_colors=colors,
        textinfo="label+percent",
        hovertemplate="%{label}: %{value:,}人 (%{percent})<extra></extra>",
    ))
    fig.add_annotation(
        text=f"<b>{int(total_new):,}人</b>", x=0.5, y=0.5,
        font_size=18, showarrow=False,
    )
    return _fig_layout(fig, "入院経路（年間新規入棟）")


def discharge_route_pie(ward_df: pd.DataFrame, hospital_name: str) -> go.Figure:
    """
    退院経路の円グラフ。
    家庭退院 / 他院転院 / 施設入所 / 死亡 / その他 の内訳。
    """
    sub = ward_df[ward_df["医療機関名"] == hospital_name]
    if sub.empty:
        fig = go.Figure()
        fig.add_annotation(text="データなし", x=0.5, y=0.5, showarrow=False)
        return _fig_layout(fig, "退院経路")

    total     = float(sub["退棟患者数"].sum())
    katei     = float(sub["家庭退院数"].sum())
    tain      = float(sub["他院転院数"].sum())
    shisetsu  = float(sub["施設入所数"].sum())
    shibo     = float(sub["死亡退院数"].sum())
    sonota    = max(total - katei - tain - shisetsu - shibo, 0)

    labels = ["家庭退院", "他院転院", "施設入所", "死亡退院", "その他"]
    values = [katei, tain, shisetsu, shibo, sonota]
    colors = ["#2ecc71", "#e67e22", "#9b59b6", "#7f8c8d", "#bdc3c7"]

    fig = go.Figure(go.Pie(
        labels=labels, values=values,
        hole=0.45,
        marker_colors=colors,
        textinfo="label+percent",
        hovertemplate="%{label}: %{value:,}人 (%{percent})<extra></extra>",
    ))
    fig.add_annotation(
        text=f"<b>{int(total):,}人</b>", x=0.5, y=0.5,
        font_size=18, showarrow=False,
    )
    return _fig_layout(fig, "退院経路（年間退棟）")


def home_return_rate_bar(ward_df: pd.DataFrame, hospital_name: str, secondary_region: str) -> go.Figure:
    """
    同二次医療圏の在宅復帰率横棒グラフ（選択病院をハイライト）。

    計算式: 在宅復帰率 = 家庭退院数 ÷ (退棟患者数 − 死亡退院数) × 100
    出典: 病床機能報告 様式1（厚生労働省）病棟票 退棟先区分に準拠
    ゼロ除算対策: 分母 (退棟患者数 − 死亡退院数) = 0 の場合は NaN として除外する。
    """
    sub = ward_df[ward_df["二次医療圏名"] == secondary_region].copy() if "二次医療圏名" in ward_df.columns else ward_df.copy()

    if sub.empty:
        fig = go.Figure()
        fig.add_annotation(text="データなし", x=0.5, y=0.5, showarrow=False)
        return _fig_layout(fig, "在宅復帰率（地域内比較）")

    # 病院単位に集計
    agg_cols = {"家庭退院数": ("家庭退院数", "sum"), "退棟患者数": ("退棟患者数", "sum")}
    if "死亡退院数" in sub.columns:
        agg_cols["死亡退院数"] = ("死亡退院数", "sum")
    agg = sub.groupby("医療機関名").agg(**agg_cols).reset_index()

    # 正しい分母: 退棟患者数 − 死亡退院数
    if "死亡退院数" in agg.columns:
        _denom = (agg["退棟患者数"] - agg["死亡退院数"]).clip(lower=0)
    else:
        _denom = agg["退棟患者数"]

    agg["在宅復帰率(%)"] = (
        agg["家庭退院数"] / _denom.replace(0, np.nan) * 100
    ).round(1)
    agg = agg.dropna(subset=["在宅復帰率(%)"]).sort_values("在宅復帰率(%)", ascending=True).tail(20)

    colors = ["#e74c3c" if n == hospital_name else "#3498db" for n in agg["医療機関名"]]

    fig = go.Figure(go.Bar(
        x=agg["在宅復帰率(%)"], y=agg["医療機関名"],
        orientation="h",
        marker_color=colors,
        text=agg["在宅復帰率(%)"].apply(lambda v: f"{v:.1f}%"),
        textposition="auto",
        hovertemplate="%{y}: %{x:.1f}%<extra></extra>",
    ))
    fig.update_xaxes(range=[0, 105], ticksuffix="%")
    return _fig_layout(fig, f"在宅復帰率 地域内比較（{secondary_region}）",
                       height=max(350, len(agg) * 26))
