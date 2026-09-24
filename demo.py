"""LP（ログイン前画面）から見られる、ログイン不要のサンプル病院デモ。

auth.require_login() はデータ読み込み前に未ログインユーザーを止める設計
（ボット等の未ログインアクセスでサーバー資源を消費しないため）なので、
デモはアプリ本体のデータ読み込みを通さず、固定の1病院と同じ二次医療圏の
行だけを parquet から直接読む。対象を1病院に固定しているのは、ログイン
無しで全国のデータを引き出せる入口を作らないため。
"""
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

import charts
from data_processor import hospital_trend, region_share

DEMO_HOSPITAL_CODE = "3939020052"  # 近森病院（高知県・中央）。LPのスクリーンショットと同じ病院
_CACHE_FILE = "data_cache.parquet"


@st.cache_data(show_spinner=False)
def _load_demo_data(code: str):
    hosp_all = pd.read_parquet(_CACHE_FILE, filters=[("医療機関コード", "==", code)])
    latest = hosp_all.sort_values("報告年度").iloc[-1]
    year = int(latest["報告年度"])
    pref = str(latest["都道府県名"])
    region = str(latest["二次医療圏名"])
    region_rows = pd.read_parquet(
        _CACHE_FILE,
        filters=[("報告年度", "==", year), ("都道府県名", "==", pref), ("二次医療圏名", "==", region)],
    )
    return hosp_all, region_share(region_rows, year, pref, region), year, pref, region


def _kpi_card(col, label, value, sub=""):
    col.markdown(
        f'<div class="metric-card"><div class="metric-label">{label}</div>'
        f'<div class="metric-value">{value}</div>'
        f'<div class="metric-sub">{sub or "&nbsp;"}</div></div>',
        unsafe_allow_html=True,
    )


def _source_note(text: str) -> None:
    st.markdown(
        f"<div style='text-align:right;font-size:0.75rem;color:#6E6A5E;margin:4px 0 0;'>"
        f"データ出典：{text}</div>",
        unsafe_allow_html=True,
    )


def render_demo(render_signup, on_view=None) -> None:
    """デモ画面を描画する。

    render_signup: 申込みフォームを描画する関数（form_key を受け取る）。
      デモを見て興味を持った人がLPへ戻らずにその場で申し込めるようにする。
    on_view: デモを表示したことをGA4等に記録する関数（1セッション1回だけ呼ぶ）。
    """
    hosp_all, region_df, year, pref, region = _load_demo_data(DEMO_HOSPITAL_CODE)
    row = hosp_all[hosp_all["報告年度"] == year].iloc[0]
    name = str(row["医療機関名"])

    if on_view and not st.session_state.get("_demo_view_logged"):
        on_view()
        st.session_state["_demo_view_logged"] = True

    components.html(
        "<script>window.parent.document.querySelector('[data-testid=\"stMain\"]')"
        "?.scrollTo(0,0);</script>",
        height=0,
    )

    st.markdown(
        """
<style>
.st-key-_demo_wrap { max-width: 960px; margin: 0 auto; }
</style>""",
        unsafe_allow_html=True,
    )
    with st.container(key="_demo_wrap"):
        if st.button("← トップに戻る", key="_demo_back"):
            del st.query_params["demo"]
            st.rerun()

        st.markdown(
            f"""
<div style="background:#EAF4F0;border:1px solid #BFDFD4;border-radius:12px;
            padding:12px 16px;margin:8px 0 20px;font-size:0.88rem;color:#26251F;">
  🔍 <strong>ログイン不要のお試し版</strong>です。実際の画面と同じデータ・計算で、
  サンプルとして1病院だけ表示しています。会員になると全国7,000件以上の病院で
  同じ分析・検索・CSV出力ができます。
</div>
<div style="font-size:0.85rem;color:#6E6A5E;">{year}年度 › {pref} › {region}</div>
<h2 style="margin:4px 0 16px;">{name}</h2>""",
            unsafe_allow_html=True,
        )

        kyoka = int(row["合計_許可病床数"])
        zaitou = int(row["合計_在棟延べ数"])
        occ = zaitou / 365 / kyoka if kyoka else 0
        doctors = int(row["常勤医師数"])
        nurses = int(row["常勤看護師数"])
        rank_row = region_df[region_df["医療機関コード"] == DEMO_HOSPITAL_CODE].iloc[0]

        m1, m2, m3, m4, m5 = st.columns(5)
        _kpi_card(m1, "許可病床数", f"{kyoka:,}床", f"平均在棟 {zaitou // 365:,}人/日")
        _kpi_card(m2, "総稼働率", f"{occ * 100:.1f}%", "在棟延べ数は前年度実績")
        _kpi_card(m3, "地域内順位", f"{int(rank_row['地域内順位'])}位", f"/ {len(region_df)}院中")
        _kpi_card(m4, "地域シェア", f"{float(rank_row['地域シェア(%)']):.1f}%", "許可病床数ベース")
        _kpi_card(m5, "常勤医師数", f"{doctors:,}人", f"看護師 {nurses:,}人")
        _source_note(f"{year}年度 病床機能報告（厚生労働省）")

        c1, c2 = st.columns(2)
        with c1:
            st.plotly_chart(charts.bed_donut(row, name), use_container_width=True, config={"responsive": True})
        with c2:
            st.plotly_chart(charts.occupancy_gauge(occ, "総稼働率"), use_container_width=True, config={"responsive": True})

        st.markdown('<div class="section-header">経年トレンド</div>', unsafe_allow_html=True)
        trend_df = hospital_trend(hosp_all, DEMO_HOSPITAL_CODE)
        t1, t2 = st.columns(2)
        with t1:
            st.plotly_chart(charts.trend_beds(trend_df, name), use_container_width=True, config={"responsive": True})
        with t2:
            st.plotly_chart(charts.trend_occupancy(trend_df, name), use_container_width=True, config={"responsive": True})

        st.markdown(f'<div class="section-header">{region}医療圏の中での位置づけ</div>', unsafe_allow_html=True)
        st.plotly_chart(
            charts.regional_bed_comparison(region_df, name),
            use_container_width=True, config={"responsive": True},
        )
        _source_note(f"{year}年度 病床機能報告（厚生労働省）")

        st.markdown(
            """
<div style="background:#FFFFFF;border:2px solid #12886D;border-radius:14px;
            padding:20px 24px;margin:32px 0 16px;text-align:center;">
  <div style="font-weight:800;font-size:1.1rem;color:#26251F;margin-bottom:6px;">
    全国7,000件以上の病院で、同じ分析を
  </div>
  <div style="font-size:0.88rem;color:#6E6A5E;">
    病院名・エリア・設備・手術件数での検索、DPC、施設基準届出、PDF資料出力、CSV出力が使えます。
    お試し価格 月額500円（税込）
  </div>
</div>""",
            unsafe_allow_html=True,
        )
        render_signup("_demo_signup_form")
