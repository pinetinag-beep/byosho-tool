"""
病床機能報告 分析・比較ツール
"""
import streamlit as st
import pandas as pd
import numpy as np
import unicodedata
import re

import urllib.request
from pathlib import Path
from datetime import datetime

from data_processor import (
    load_data, load_multiple_mhlw, load_mhlw_byosho_extended, load_multiple_mhlw_extended,
    load_mhlw_yoshiki2, load_mhlw_shisetsu, merge_shisetsu,
    normalize, add_derived_columns,
    region_share, hospital_trend, bed_composition,
    load_hospitals_from_db, load_wards_from_db, load_surgery_from_db, get_db_meta,
    BED_TYPES, BED_COLORS, PREF_CODE_MAP,
)

# 都道府県コード順（北から南）のソートキー
_PREF_ORDER = {name: code for code, name in PREF_CODE_MAP.items()}

def _sort_prefs(pref_list):
    """都道府県名リストを都道府県コード順に並べる"""
    return sorted(pref_list, key=lambda p: _PREF_ORDER.get(p, "99"))

def _normalize_name(name: str) -> str:
    """病院名の表記揺れを正規化（全角→半角、スペース除去、小文字化）"""
    if not isinstance(name, str):
        return ""
    name = unicodedata.normalize("NFKC", name)
    name = re.sub(r'[\s　・]', '', name)
    name = name.lower()
    return name

from charts import (
    bed_donut, occupancy_gauge, bed_type_occupancy_bar,
    regional_bed_comparison, occupancy_scatter, share_bar, ranking_table_fig,
    trend_beds, trend_occupancy, trend_staff,
    staff_scatter, staff_bar_region,
    detail_bed_type_table, admission_route_pie, discharge_route_pie, home_return_rate_bar,
)
from sample_data import generate_sample_data

# ── ページ設定 ─────────────────────────────────────────────

st.set_page_config(
    page_title="病床機能報告 分析ツール",
    page_icon="🏥",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Google Analytics ───────────────────────────────────────
st.markdown("""
<!-- Google tag (gtag.js) -->
<script async src="https://www.googletagmanager.com/gtag/js?id=G-8Y6SDBSCMQ"></script>
<script>
  window.dataLayer = window.dataLayer || [];
  function gtag(){dataLayer.push(arguments);}
  gtag('js', new Date());
  gtag('config', 'G-8Y6SDBSCMQ');
</script>
""", unsafe_allow_html=True)

st.markdown("""
<style>
.metric-card {
    background: #f8f9fa;
    border-radius: 10px;
    padding: 16px 20px;
    text-align: center;
    border-left: 4px solid #3498db;
}
.metric-label { font-size: 0.85rem; color: #666; margin-bottom: 4px; }
.metric-value { font-size: 1.8rem; font-weight: 700; color: #2c3e50; }
.metric-sub   { font-size: 0.8rem;  color: #999; margin-top: 2px; }
.section-header {
    font-size: 1.1rem; font-weight: 600; color: #2c3e50;
    border-bottom: 2px solid #3498db; padding-bottom: 6px; margin: 20px 0 12px;
}
/* サイドバーの検索結果ボタンをフラットに */
div[data-testid="stSidebar"] .stButton button {
    text-align: left;
    font-size: 0.82rem;
    padding: 4px 8px;
    height: auto;
    white-space: normal;
    word-break: break-all;
}

/* ── 印刷ボタン（画面表示用） ── */
.print-btn {
    display: inline-block;
    padding: 6px 16px;
    background: #f0f2f6;
    border: 1px solid #d0d3db;
    border-radius: 6px;
    font-size: 0.85rem;
    color: #444;
    cursor: pointer;
    text-decoration: none;
}
.print-btn:hover { background: #e0e3ea; }

/* ══════════════════════════════════
   印刷用スタイル（A4縦）
══════════════════════════════════ */
@media print {
    @page {
        size: A4 portrait;
        margin: 15mm 12mm 12mm 12mm;
    }

    /* サイドバー・ヘッダー・ツールバーを非表示 */
    [data-testid="stSidebar"],
    header[data-testid="stHeader"],
    [data-testid="stToolbar"],
    [data-testid="stDecoration"],
    [data-testid="stStatusWidget"],
    .stDeployButton,
    footer,
    #MainMenu {
        display: none !important;
    }

    /* メインコンテンツを全幅に */
    .main .block-container {
        max-width: 100% !important;
        padding: 8mm 0 0 0 !important;
    }
    section.main { padding: 0 !important; }

    /* タブのナビゲーション（タブボタン行）を非表示 */
    .stTabs [role="tablist"] {
        display: none !important;
    }

    /* アクティブなタブパネルだけ表示 */
    .stTabs [role="tabpanel"][hidden] {
        display: none !important;
    }

    /* ボタン・入力・ダウンロードを非表示 */
    .stButton, .stDownloadButton,
    .stTextInput, .stSelectbox,
    .stCheckbox, .stRadio,
    .print-btn {
        display: none !important;
    }

    /* 白背景・黒文字に統一 */
    body, .main, .stApp {
        background: white !important;
        color: #111 !important;
    }

    /* メトリクスカード: 印刷用にシンプル化 */
    .metric-card {
        background: white !important;
        border: 1px solid #bbb !important;
        border-left-width: 4px !important;
        box-shadow: none !important;
        -webkit-print-color-adjust: exact;
        print-color-adjust: exact;
    }
    .metric-value { color: #111 !important; }
    .metric-label { color: #444 !important; }
    .metric-sub   { color: #666 !important; }

    /* セクションヘッダー */
    .section-header {
        color: #111 !important;
        border-bottom-color: #3498db !important;
        page-break-after: avoid;
        -webkit-print-color-adjust: exact;
        print-color-adjust: exact;
    }

    /* グラフ: 縮小してページ内に収める */
    .js-plotly-plot, [data-testid="stArrowVegaLiteChart"] {
        max-width: 100% !important;
        page-break-inside: avoid;
    }

    /* テーブル */
    [data-testid="stDataFrame"] {
        page-break-inside: avoid;
    }

    /* ページブレーク制御 */
    h2, h3 { page-break-after: avoid; }
}
</style>
""", unsafe_allow_html=True)


# ── DuckDB パス ────────────────────────────────────────────

DB_PATH = Path(__file__).parent / "data" / "byosho.duckdb"

CACHE_FILE         = Path(__file__).parent / "data_cache.parquet"
CACHE_FILE_WARD    = Path(__file__).parent / "ward_cache.parquet"
CACHE_FILE_SURGERY = Path(__file__).parent / "surgery_cache.parquet"


def save_cache(df: pd.DataFrame):
    df.to_parquet(CACHE_FILE, index=False)

def save_ward_cache(df: pd.DataFrame):
    df.to_parquet(CACHE_FILE_WARD, index=False)

def save_surgery_cache(df: pd.DataFrame):
    df.to_parquet(CACHE_FILE_SURGERY, index=False)


# ── DuckDB からキャッシュ付きで読み込む ──────────────────────

@st.cache_data(show_spinner="📊 データ読み込み中...")
def _db_hospitals():
    return load_hospitals_from_db(str(DB_PATH))

@st.cache_data(show_spinner=False)
def _db_wards():
    return load_wards_from_db(str(DB_PATH))

@st.cache_data(show_spinner=False)
def _db_surgery():
    return load_surgery_from_db(str(DB_PATH))


# ── セッションステート初期化 ────────────────────────────────

if "df" not in st.session_state:
    if DB_PATH.exists():
        try:
            st.session_state.df          = _db_hospitals()
            st.session_state.ward_df     = _db_wards()
            st.session_state.surgery_df  = _db_surgery()
            st.session_state._datasrc   = "db"
        except Exception as _e:
            st.session_state.df         = None
            st.session_state.ward_df    = None
            st.session_state.surgery_df = None
            st.session_state._datasrc   = "none"
    elif CACHE_FILE.exists():
        st.session_state.df = pd.read_parquet(CACHE_FILE)
        st.session_state.ward_df     = pd.read_parquet(CACHE_FILE_WARD) if CACHE_FILE_WARD.exists() else None
        st.session_state.surgery_df  = pd.read_parquet(CACHE_FILE_SURGERY) if CACHE_FILE_SURGERY.exists() else None
        st.session_state._datasrc   = "parquet"
    else:
        st.session_state.df         = None
        st.session_state.ward_df    = None
        st.session_state.surgery_df = None
        st.session_state._datasrc   = "none"

if "ward_df" not in st.session_state:
    st.session_state.ward_df = None
if "surgery_df" not in st.session_state:
    st.session_state.surgery_df = None
if "_datasrc" not in st.session_state:
    st.session_state._datasrc = "none"
# 表示モード: "detail"（病院詳細）or "search"（詳細条件検索）
if "_view_mode" not in st.session_state:
    st.session_state["_view_mode"] = "detail"


# ── NaN → int ヘルパー ─────────────────────────────────────
def _si(val):
    """NaN / None / 文字列を安全に int に変換"""
    try:
        return int(val or 0)
    except (ValueError, TypeError):
        return 0


# ── サイドバー ───────────────────────────────────────────────

with st.sidebar:
    st.title("🏥 病床機能報告")

    # ── 免責事項 ──
    with st.expander("⚠️ 免責事項", expanded=False):
        st.markdown("""
<div style="font-size:0.75rem; color:#555; line-height:1.6;">

本ツールは、厚生労働省が公表する**病床機能報告**のデータをもとに集計・分析を行うものです。

**ご利用にあたっての注意事項：**

- 原データ（報告値）に誤りや未報告が含まれる場合があり、分析結果が実態と異なることがあります。
- 本ツールの分析結果は参考情報であり、医療機関の評価・優劣を示すものではありません。
- 経営判断・医療政策の立案などに利用する場合は、必ず一次データや専門家の助言を合わせてご確認ください。
- 本ツールの利用によって生じたいかなる損害についても、作成者は責任を負いません。
- データは報告年度時点のものであり、現在の状況と異なる場合があります。

</div>
""", unsafe_allow_html=True)

    # ── データなし → サンプルまたは手動ロード ──
    if st.session_state.df is None:
        st.divider()
        st.warning("データが未準備です")

        tab_s, tab_m = st.tabs(["🎮 サンプル", "📁 手動"])

        with tab_s:
            st.caption("デモ用のサンプルデータ（4年分）を生成します")
            if st.button("サンプルデータを使う", type="primary", use_container_width=True):
                with st.spinner("生成中..."):
                    df_loaded = generate_sample_data()
                    st.session_state.df = df_loaded
                    st.session_state.ward_df = None
                    st.session_state.surgery_df = None
                    st.session_state._datasrc = "sample"
                st.rerun()

        with tab_m:
            st.caption("様式1・2 Excelをアップロード")
            report_year = st.number_input("報告年度", value=2023, min_value=2010, max_value=2030, step=1)
            uploaded_files = st.file_uploader(
                "Excelファイル（複数可）",
                type=["xlsx", "xls"],
                accept_multiple_files=True,
            )
            if uploaded_files and st.button("読み込む", type="primary", use_container_width=True):
                with st.spinner(f"{len(uploaded_files)}ファイルを処理中..."):
                    try:
                        fb = [(f.name, f.read()) for f in uploaded_files]
                        df_loaded, ward_loaded = load_multiple_mhlw_extended(fb, year=int(report_year))
                        st.session_state.df = df_loaded
                        st.session_state.ward_df = ward_loaded
                        st.success(f"{len(df_loaded):,}病院を読み込みました")
                        st.rerun()
                    except Exception as e:
                        st.error(f"読み込みエラー: {e}")

    # ── 病院を探す（データあり） ──
    if st.session_state.df is not None:
        _df_all = st.session_state.df

        st.divider()
        st.subheader("🔍 病院を探す")

        # ── ① 病院名で探す ──
        st.markdown(
            "<div style='font-size:0.8rem;font-weight:600;color:#444;margin-bottom:4px;'>"
            "① 病院名で探す</div>",
            unsafe_allow_html=True,
        )
        _name_q = st.text_input(
            "病院名で検索",
            placeholder="例: 大学病院、中央病院",
            key="_sidebar_name_q",
            label_visibility="collapsed",
        )

        if _name_q:
            _norm_q = _normalize_name(_name_q)
            _latest_year = int(_df_all["報告年度"].max())
            _name_df = _df_all[_df_all["報告年度"] == _latest_year].copy()
            _name_df["_norm"] = _name_df["医療機関名"].apply(_normalize_name)
            _matched = _name_df[_name_df["_norm"].str.contains(_norm_q, na=False)]

            if _matched.empty:
                st.caption("🔎 見つかりませんでした")
            else:
                st.caption(f"**{len(_matched)}件** 見つかりました（{_latest_year}年度）")
                for _, _mrow in _matched.head(12).iterrows():
                    _btn_label = f"🏥 {_mrow['医療機関名']}"
                    _btn_key   = f"_nbtn_{_mrow['医療機関名']}"
                    if st.button(_btn_label, key=_btn_key, use_container_width=True):
                        st.session_state["_sel_year"]     = int(_mrow["報告年度"])
                        st.session_state["_sel_pref"]     = str(_mrow["都道府県名"])
                        st.session_state["_sel_region"]   = str(_mrow["二次医療圏名"])
                        st.session_state["_sel_hospital"] = str(_mrow["医療機関名"])
                        st.session_state["_view_mode"]    = "detail"
                        st.rerun()
                if len(_matched) > 12:
                    st.caption(f"… 他 {len(_matched)-12}件（絞り込んでください）")

        # ── ② 条件で検索する ──
        st.markdown(
            "<div style='font-size:0.8rem;font-weight:600;color:#444;"
            "margin-top:12px;margin-bottom:4px;'>"
            "② 条件で検索する</div>",
            unsafe_allow_html=True,
        )
        st.caption("手術・設備などの条件で絞り込み")
        _is_search = st.session_state.get("_view_mode") == "search"
        if _is_search:
            if st.button("← 病院詳細に戻る", use_container_width=True, type="secondary"):
                st.session_state["_view_mode"] = "detail"
                st.rerun()
        else:
            if st.button("🔧 条件で検索する", use_container_width=True, type="secondary"):
                st.session_state["_view_mode"] = "search"
                st.rerun()

        st.divider()
        st.subheader("📍 選択中の病院")

        # 検索タブからのナビゲーションジャンプを処理
        _nav = st.session_state.pop("_nav_jump", None)
        if _nav:
            st.session_state["_sel_year"]     = int(_nav["year"])
            st.session_state["_sel_pref"]     = str(_nav["pref"])
            st.session_state["_sel_region"]   = str(_nav["region"])
            st.session_state["_sel_hospital"] = str(_nav["hospital"])
            st.session_state["_nav_done"]     = str(_nav["hospital"])

        years = [int(y) for y in sorted(_df_all["報告年度"].dropna().unique(), reverse=True)]
        sel_year = st.selectbox("報告年度", years, key="_sel_year")

        prefs = _sort_prefs(_df_all["都道府県名"].unique())
        if st.session_state.get("_sel_pref") not in prefs:
            st.session_state["_sel_pref"] = prefs[0] if prefs else None
        sel_pref = st.selectbox("都道府県", prefs, key="_sel_pref")

        regions = sorted(r for r in _df_all[_df_all["都道府県名"] == sel_pref]["二次医療圏名"].unique() if r != "不明")
        if st.session_state.get("_sel_region") not in regions:
            st.session_state["_sel_region"] = regions[0] if regions else None
        sel_region = st.selectbox("二次医療圏", regions, key="_sel_region")

        hospitals_in_region = _df_all[
            (_df_all["報告年度"] == sel_year) &
            (_df_all["都道府県名"] == sel_pref) &
            (_df_all["二次医療圏名"] == sel_region)
        ]["医療機関名"].sort_values().tolist()

        if st.session_state.get("_sel_hospital") not in hospitals_in_region:
            st.session_state["_sel_hospital"] = hospitals_in_region[0] if hospitals_in_region else None
        sel_hospital = st.selectbox("医療機関名", hospitals_in_region, key="_sel_hospital")

        if "_nav_done" in st.session_state:
            st.success(f"✅ {st.session_state.pop('_nav_done')}\n「病院概要」タブで確認できます")

        st.divider()
        st.caption(f"全 {len(_df_all[_df_all['報告年度']==sel_year]):,} 病院 | {sel_year}年度")
        if len(years) > 1:
            st.caption(f"収録年度: {int(min(years))}〜{int(max(years))}")

    # ── DB ステータス表示（一番下） ──
    st.divider()
    _src = st.session_state.get("_datasrc", "none")
    if _src == "db":
        try:
            meta = get_db_meta(str(DB_PATH))
            st.success("✅ データ読み込み済み")
            st.caption(f"📅 更新: {meta['updated_at']}")
            st.caption(f"📊 年度: {meta['years']}")
            st.caption(f"🏥 病院数: {meta['hospital_cnt']:,}件")
        except Exception:
            st.info("DuckDB 接続中...")
    elif _src == "parquet":
        st.warning("⚠️ 旧データを表示中")
    elif _src == "sample":
        st.info("🎮 サンプルデータ表示中")

    # ── 管理者セクション ──
    st.divider()
    with st.expander("🔧 管理者"):
        st.caption("データの再読み込み / キャッシュ管理")
        if st.button("キャッシュをクリアして再読み込み", use_container_width=True):
            st.cache_data.clear()
            for key in ["df", "ward_df", "surgery_df"]:
                st.session_state.pop(key, None)
            st.rerun()
        st.divider()
        st.caption("サーバー上でデータを再構築する場合:")
        st.code("python build_db.py", language="bash")
        st.caption("特定年度だけ更新する場合:")
        st.code("python build_db.py --years 2023", language="bash")


# ── メインエリア ───────────────────────────────────────────

if st.session_state.df is None:
    st.markdown("## 🏥 病床機能報告 分析・比較ツール")
    col1, col2 = st.columns(2)
    with col1:
        st.info("""
**このツールでできること**
- 選択した病院の病床種別・稼働率を可視化
- 同二次医療圏内でのベンチマーク比較
- 地域内順位・シェアの把握
- 経年変化トレンドの確認
- 医療スタッフ配置の地域比較
        """)
    with col2:
        st.info("""
**使い方**
1. 左サイドバーの「サンプルデータを読み込む」をクリック
   （または実データのCSV/Excelをアップロード）
2. 都道府県・二次医療圏・医療機関名を選択
3. 各タブで分析結果を確認

**対応データ形式**
- 厚生労働省 病床機能報告 CSVファイル
- 独自整備のExcelファイル（列名が一致する場合）
        """)
    st.stop()


# ── データ準備 ─────────────────────────────────────────────

df = st.session_state.df
year     = sel_year
pref     = sel_pref
region   = sel_region
hospital = sel_hospital


# ══════════════════════════════════════════════════════════
# 詳細条件検索モード
# ══════════════════════════════════════════════════════════

if st.session_state.get("_view_mode") == "search":

    st.markdown("## 🔧 詳細条件で病院を検索")
    st.caption("手術件数・医療設備の条件で全国の病院を絞り込んで一覧表示します")

    # ── フィルターパネル ──
    with st.expander("🔎 絞り込みフィルター", expanded=True):
        fc1, fc2, fc3 = st.columns(3)

        with fc1:
            st.markdown("**📍 場所**")
            s_years_list = [int(y) for y in sorted(df["報告年度"].dropna().unique(), reverse=True)]
            s_year   = st.selectbox("年度", s_years_list, key="s_year",
                help="病床機能報告の報告年度\nデータ列: 報告年度")
            s_all_prefs = ["全都道府県"] + _sort_prefs(df["都道府県名"].unique())
            s_pref   = st.selectbox("都道府県", s_all_prefs, key="s_pref",
                help="都道府県で絞り込み\nデータ列: 都道府県名")
            if s_pref != "全都道府県":
                s_all_regions = ["全二次医療圏"] + sorted(
                    r for r in df[df["都道府県名"] == s_pref]["二次医療圏名"].unique()
                    if r != "不明"
                )
            else:
                s_all_regions = ["全二次医療圏"]
            s_region = st.selectbox("二次医療圏", s_all_regions, key="s_region",
                help="二次医療圏で絞り込み\nデータ列: 二次医療圏名")
            s_kw     = st.text_input("病院名キーワード", placeholder="例: 大学病院", key="s_kw",
                help="医療機関名の部分一致検索\n全角/半角・スペース・中点などの表記揺れを自動正規化\nデータ列: 医療機関名")

        with fc2:
            st.markdown("**✂️ 手術**")
            s_surg_mode = st.radio(
                "対象",
                ["手術（全数）", "全身麻酔の手術"],
                horizontal=True,
                key="s_surg_mode",
                help="様式2（手術実績票）の集計対象を切り替え\n"
                     "・手術（全数）→ データ列: 手術_[臓器名]\n"
                     "・全身麻酔の手術 → データ列: 全麻_[臓器名]",
            )
            s_surg_logic = st.radio(
                "複数選択時の絞り込み方法",
                ["AND（すべて該当）", "OR（いずれか該当）"],
                horizontal=True,
                key="s_surg_logic",
                help="臓器・術式を複数チェックしたときの絞り込み方法\n"
                     "・AND: チェックしたすべての項目を同時に実施している病院のみ表示\n"
                     "・OR: チェックした項目のどれか1つでも実施していれば表示",
            )
            st.caption("臓器別（1件以上で表示）")
            _organ_help = (
                "様式2（手術実績票）の臓器別年間手術件数\n"
                "1件以上の病院を絞り込み対象とします\n"
                "参照列: 手術_[臓器名] または 全麻_[臓器名]（「対象」の選択に連動）"
            )
            _oa, _ob = st.columns(2)
            with _oa:
                s_ck_hifuka  = st.checkbox("皮膚・皮下組織",     key="s_ck_hifuka",  help=_organ_help)
                s_ck_kinkot  = st.checkbox("筋骨格系・四肢",     key="s_ck_kinkot",  help=_organ_help)
                s_ck_shinkei = st.checkbox("神経系・頭蓋",       key="s_ck_shinkei", help=_organ_help)
                s_ck_me      = st.checkbox("眼",                 key="s_ck_me",      help=_organ_help)
                s_ck_jibika  = st.checkbox("耳鼻咽喉",           key="s_ck_jibika",  help=_organ_help)
                s_ck_ganmen  = st.checkbox("顔面・口腔・頸部",   key="s_ck_ganmen",  help=_organ_help)
            with _ob:
                s_ck_kyobu   = st.checkbox("胸部",               key="s_ck_kyobu",  help=_organ_help)
                s_ck_shin    = st.checkbox("心・脈管",            key="s_ck_shin",   help=_organ_help)
                s_ck_fukubu  = st.checkbox("腹部",               key="s_ck_fukubu", help=_organ_help)
                s_ck_nyo     = st.checkbox("尿路系・副腎",       key="s_ck_nyo",    help=_organ_help)
                s_ck_seiki   = st.checkbox("性器",               key="s_ck_seiki",  help=_organ_help)
                s_ck_shika   = st.checkbox("歯科",               key="s_ck_shika",  help=_organ_help)
            st.caption("術式（1件以上で表示）")
            s_ck_robot_s = st.checkbox("ロボット支援手術", key="s_ck_robot_s",
                help="様式2（手術実績票）\nデータ列: ロボット支援手術数")
            s_ck_fuku    = st.checkbox("腹腔鏡下手術",   key="s_ck_fuku",
                help="様式2（手術実績票）\nデータ列: 腹腔鏡下手術数")
            s_ck_kyou    = st.checkbox("胸腔鏡下手術",   key="s_ck_kyou",
                help="様式2（手術実績票）\nデータ列: 胸腔鏡下手術数")

        with fc3:
            st.markdown("**🔵 CT**")
            ct_filter = st.radio(
                "CT絞り込み",
                ["指定なし", "CTあり（合計）", "CTなし（合計）", "スペック別"],
                key="ct_filter",
                label_visibility="collapsed",
                help="様式1（施設票）CT装置の台数データ\n"
                     "・指定なし: フィルターなし\n"
                     "・あり/なし: CT台数（全スペック合計）で判定 → データ列: CT台数\n"
                     "・スペック別: 列種別ごとに個別判定 → データ列: CT_64列以上 / CT_16〜64列 / CT_16列未満",
            )
            s_ck_ct64 = s_ck_ct16p = s_ck_ct16m = False
            if ct_filter == "スペック別":
                s_ck_ct64  = st.checkbox("64列以上",  key="s_ck_ct64",
                    help="様式1（施設票）\nデータ列: CT_64列以上（台数 1台以上を条件）")
                s_ck_ct16p = st.checkbox("16〜64列",  key="s_ck_ct16p",
                    help="様式1（施設票）\nデータ列: CT_16〜64列（台数 1台以上を条件）")
                s_ck_ct16m = st.checkbox("16列未満",  key="s_ck_ct16m",
                    help="様式1（施設票）\nデータ列: CT_16列未満（台数 1台以上を条件）")

            st.markdown("**🔴 MRI**")
            mri_filter = st.radio(
                "MRI絞り込み",
                ["指定なし", "MRIあり（合計）", "MRIなし（合計）", "スペック別"],
                key="mri_filter",
                label_visibility="collapsed",
                help="様式1（施設票）MRI装置の台数データ\n"
                     "・指定なし: フィルターなし\n"
                     "・あり/なし: MRI台数（全スペック合計）で判定 → データ列: MRI台数\n"
                     "・スペック別: 列種別ごとに個別判定 → データ列: MRI_3T以上 / MRI_1.5〜3T / MRI_1.5T未満",
            )
            s_ck_mri3t = s_ck_mri15p = s_ck_mri15m = False
            if mri_filter == "スペック別":
                s_ck_mri3t  = st.checkbox("3T以上",   key="s_ck_mri3t",
                    help="様式1（施設票）\nデータ列: MRI_3T以上（台数 1台以上を条件）")
                s_ck_mri15p = st.checkbox("1.5〜3T",  key="s_ck_mri15p",
                    help="様式1（施設票）\nデータ列: MRI_1.5〜3T（台数 1台以上を条件）")
                s_ck_mri15m = st.checkbox("1.5T未満", key="s_ck_mri15m",
                    help="様式1（施設票）\nデータ列: MRI_1.5T未満（台数 1台以上を条件）")

            st.markdown("**🏥 その他設備**")
            s_has_pet      = st.checkbox("PET / PET-CTあり",    key="s_has_pet",
                help="様式1（施設票）\nデータ列: PET台数 + PETCT台数（合計 1台以上を条件）")
            s_has_robot_eq = st.checkbox("手術支援ロボットあり", key="s_has_robot_eq",
                help="様式1（施設票）\nデータ列: 内視鏡手術支援機器台数（1台以上を条件）")
            s_has_gamma    = st.checkbox("ガンマナイフあり",     key="s_has_gamma",
                help="様式1（施設票）\nデータ列: ガンマナイフ台数（1台以上を条件）")

    # ── フィルタリング処理 ──
    s_df = df[df["報告年度"] == s_year].copy()

    if s_pref != "全都道府県":
        s_df = s_df[s_df["都道府県名"] == s_pref]
    if s_region != "全二次医療圏":
        s_df = s_df[s_df["二次医療圏名"] == s_region]
    if s_kw:
        _norm_kw = _normalize_name(s_kw)
        s_df = s_df[s_df["医療機関名"].apply(_normalize_name).str.contains(_norm_kw, na=False)]

    # 手術データをマージ
    _ORGAN_LABELS = [
        "皮膚・皮下組織", "筋骨格系・四肢・体幹", "神経系・頭蓋", "眼",
        "耳鼻咽喉", "顔面・口腔・頸部", "胸部", "心・脈管",
        "腹部", "尿路系・副腎", "性器", "歯科",
    ]
    _surg_cols_all = (
        ["手術総数", "全身麻酔手術数", "ロボット支援手術数",
         "腹腔鏡下手術数", "胸腔鏡下手術数", "悪性腫瘍手術数",
         "脳血管内手術数", "人工心肺手術数"]
        + [f"手術_{lb}" for lb in _ORGAN_LABELS]
        + [f"全麻_{lb}" for lb in _ORGAN_LABELS]
    )
    _surg_state = st.session_state.get("surgery_df")

    if _surg_state is not None and not _surg_state.empty:
        _sy = _surg_state[_surg_state["報告年度"] == s_year] if "報告年度" in _surg_state.columns else _surg_state
        _avail = [c for c in _surg_cols_all if c in _sy.columns]
        if _avail:
            _join = "医療機関コード" if ("医療機関コード" in _sy.columns and "医療機関コード" in s_df.columns) else "医療機関名"
            _sy_m = _sy[[_join] + _avail].copy()
            _sy_m[_join] = _sy_m[_join].astype(str).str.strip()
            if _join == "医療機関コード" and "医療機関コード" in s_df.columns:
                s_df = s_df.copy()
                s_df["医療機関コード"] = s_df["医療機関コード"].astype(str).str.strip()
            s_df = s_df.merge(
                _sy_m.drop_duplicates(_join),
                on=_join, how="left", suffixes=("", "_sy"),
            )
        for c in _avail:
            s_df[c] = pd.to_numeric(s_df[c], errors="coerce").fillna(0).astype(int)
    else:
        for c in _surg_cols_all:
            s_df[c] = 0

    # ── 臓器別手術フィルター ──
    _organ_prefix = "全麻_" if s_surg_mode == "全身麻酔の手術" else "手術_"
    _organ_checks = [
        (s_ck_hifuka,  "皮膚・皮下組織"),
        (s_ck_kinkot,  "筋骨格系・四肢・体幹"),
        (s_ck_shinkei, "神経系・頭蓋"),
        (s_ck_me,      "眼"),
        (s_ck_jibika,  "耳鼻咽喉"),
        (s_ck_ganmen,  "顔面・口腔・頸部"),
        (s_ck_kyobu,   "胸部"),
        (s_ck_shin,    "心・脈管"),
        (s_ck_fukubu,  "腹部"),
        (s_ck_nyo,     "尿路系・副腎"),
        (s_ck_seiki,   "性器"),
        (s_ck_shika,   "歯科"),
    ]

    _organ_cols_exist = any(f"手術_{lb}" in s_df.columns for lb in _ORGAN_LABELS)
    _any_organ_checked = any(ck for ck, _ in _organ_checks)

    if _any_organ_checked and not _organ_cols_exist:
        st.warning(
            "⚠️ 臓器別の手術データはまだ読み込まれていません。\n\n"
            "**「起動_build.bat」を再実行**して DuckDB を再ビルドしてください。"
        )

    # ── 臓器・術式フィルター（OR / AND 切り替え）──
    _organ_col_checks = [(ck, f"{_organ_prefix}{lb}") for ck, lb in _organ_checks]
    _shiki_col_checks = [
        (s_ck_robot_s, "ロボット支援手術数"),
        (s_ck_fuku,    "腹腔鏡下手術数"),
        (s_ck_kyou,    "胸腔鏡下手術数"),
    ]
    _active_surg_checks = [
        (ck, col)
        for ck, col in _organ_col_checks + _shiki_col_checks
        if ck and col in s_df.columns
    ]

    if _active_surg_checks:
        if s_surg_logic == "OR（いずれか該当）":
            _or_mask = pd.Series(False, index=s_df.index)
            for _, _col in _active_surg_checks:
                _or_mask = _or_mask | (pd.to_numeric(s_df[_col], errors="coerce").fillna(0) > 0)
            s_df = s_df[_or_mask]
        else:  # AND（すべて該当）
            for _, _col in _active_surg_checks:
                s_df = s_df[pd.to_numeric(s_df[_col], errors="coerce").fillna(0) > 0]

    # ── CT フィルター ──
    _CT_SPEC_COLS = ["CT_64列以上", "CT_16〜64列", "CT_16列未満", "CT_その他"]
    if ct_filter == "CTあり（合計）":
        if "CT台数" in s_df.columns:
            s_df = s_df[pd.to_numeric(s_df["CT台数"], errors="coerce").fillna(0) > 0]
        else:
            _ct_avail = [c for c in _CT_SPEC_COLS if c in s_df.columns]
            if _ct_avail:
                _ct_sum = sum(pd.to_numeric(s_df[c], errors="coerce").fillna(0) for c in _ct_avail)
                s_df = s_df[_ct_sum > 0]
    elif ct_filter == "CTなし（合計）":
        if "CT台数" in s_df.columns:
            s_df = s_df[pd.to_numeric(s_df["CT台数"], errors="coerce").fillna(0) == 0]
        else:
            _ct_avail = [c for c in _CT_SPEC_COLS if c in s_df.columns]
            if _ct_avail:
                _ct_sum = sum(pd.to_numeric(s_df[c], errors="coerce").fillna(0) for c in _ct_avail)
                s_df = s_df[_ct_sum == 0]
    elif ct_filter == "スペック別":
        for _ck, _col in [(s_ck_ct64, "CT_64列以上"), (s_ck_ct16p, "CT_16〜64列"), (s_ck_ct16m, "CT_16列未満")]:
            if _ck and _col in s_df.columns:
                s_df = s_df[pd.to_numeric(s_df[_col], errors="coerce").fillna(0) > 0]

    # ── MRI フィルター ──
    _MRI_SPEC_COLS = ["MRI_3T以上", "MRI_1.5〜3T", "MRI_1.5T未満"]
    if mri_filter == "MRIあり（合計）":
        if "MRI台数" in s_df.columns:
            s_df = s_df[pd.to_numeric(s_df["MRI台数"], errors="coerce").fillna(0) > 0]
        else:
            _mri_avail = [c for c in _MRI_SPEC_COLS if c in s_df.columns]
            if _mri_avail:
                _mri_sum = sum(pd.to_numeric(s_df[c], errors="coerce").fillna(0) for c in _mri_avail)
                s_df = s_df[_mri_sum > 0]
    elif mri_filter == "MRIなし（合計）":
        if "MRI台数" in s_df.columns:
            s_df = s_df[pd.to_numeric(s_df["MRI台数"], errors="coerce").fillna(0) == 0]
        else:
            _mri_avail = [c for c in _MRI_SPEC_COLS if c in s_df.columns]
            if _mri_avail:
                _mri_sum = sum(pd.to_numeric(s_df[c], errors="coerce").fillna(0) for c in _mri_avail)
                s_df = s_df[_mri_sum == 0]
    elif mri_filter == "スペック別":
        for _ck, _col in [(s_ck_mri3t, "MRI_3T以上"), (s_ck_mri15p, "MRI_1.5〜3T"), (s_ck_mri15m, "MRI_1.5T未満")]:
            if _ck and _col in s_df.columns:
                s_df = s_df[pd.to_numeric(s_df[_col], errors="coerce").fillna(0) > 0]
    if s_has_pet:
        _pet_v   = pd.to_numeric(s_df["PET台数"],   errors="coerce").fillna(0) if "PET台数"   in s_df.columns else pd.Series(0, index=s_df.index)
        _petct_v = pd.to_numeric(s_df["PETCT台数"], errors="coerce").fillna(0) if "PETCT台数" in s_df.columns else pd.Series(0, index=s_df.index)
        s_df = s_df[(_pet_v > 0) | (_petct_v > 0)]
    if s_has_robot_eq and "内視鏡手術支援機器台数" in s_df.columns:
        s_df = s_df[pd.to_numeric(s_df["内視鏡手術支援機器台数"], errors="coerce").fillna(0) > 0]
    if s_has_gamma and "ガンマナイフ台数" in s_df.columns:
        s_df = s_df[pd.to_numeric(s_df["ガンマナイフ台数"], errors="coerce").fillna(0) > 0]

    # ── 表示列の決定 ──
    _base = ["医療機関名", "都道府県名", "二次医療圏名", "合計_許可病床数"]
    _any_surg = any(ck for ck, _ in _organ_checks) or s_ck_robot_s or s_ck_fuku or s_ck_kyou
    _sshow = []
    if _any_surg and "手術総数" in s_df.columns:
        _sshow.append("手術総数")
    if _any_surg and s_surg_mode == "全身麻酔の手術" and "全身麻酔手術数" in s_df.columns:
        _sshow.append("全身麻酔手術数")
    if s_ck_robot_s and "ロボット支援手術数" in s_df.columns:
        _sshow.append("ロボット支援手術数")
    if s_ck_fuku and "腹腔鏡下手術数" in s_df.columns:
        _sshow.append("腹腔鏡下手術数")
    if s_ck_kyou and "胸腔鏡下手術数" in s_df.columns:
        _sshow.append("胸腔鏡下手術数")
    _checked_organ_cols = [f"{_organ_prefix}{lb}" for _ck, lb in _organ_checks if _ck]
    _organ_show = [c for c in _checked_organ_cols if c in s_df.columns]
    _ct_ck_map  = [(s_ck_ct64, "CT_64列以上"), (s_ck_ct16p, "CT_16〜64列"), (s_ck_ct16m, "CT_16列未満")]
    _mri_ck_map = [(s_ck_mri3t, "MRI_3T以上"), (s_ck_mri15p, "MRI_1.5〜3T"), (s_ck_mri15m, "MRI_1.5T未満")]
    _eshow = []
    # CT列: スペック別なら選択スペック列、あり/なし指定なら合計台数列を表示
    if ct_filter == "スペック別":
        _eshow += [col for ck, col in _ct_ck_map if ck and col in s_df.columns]
    elif ct_filter in ("CTあり（合計）", "CTなし（合計）"):
        if "CT台数" in s_df.columns:
            _eshow.append("CT台数")
    # MRI列: 同様
    if mri_filter == "スペック別":
        _eshow += [col for ck, col in _mri_ck_map if ck and col in s_df.columns]
    elif mri_filter in ("MRIあり（合計）", "MRIなし（合計）"):
        if "MRI台数" in s_df.columns:
            _eshow.append("MRI台数")
    if s_has_pet:
        _eshow += [c for c in ["PET台数", "PETCT台数"] if c in s_df.columns]
    if s_has_robot_eq and "内視鏡手術支援機器台数" in s_df.columns:
        _eshow.append("内視鏡手術支援機器台数")
    if s_has_gamma and "ガンマナイフ台数" in s_df.columns:
        _eshow.append("ガンマナイフ台数")
    _disp = _base + _sshow + _organ_show + _eshow

    result_s = (
        s_df[_disp]
        .sort_values("合計_許可病床数", ascending=False)
        .reset_index(drop=True)
    )

    # ── 結果表示 ──
    st.markdown(f"**{len(result_s):,} 件の病院が見つかりました**")

    _col_cfg = {
        "合計_許可病床数":  st.column_config.NumberColumn("許可病床数（床）", format="%d 床"),
        "CT_64列以上":      st.column_config.NumberColumn("CT 64列以上",      format="%d 台"),
        "CT_16〜64列":      st.column_config.NumberColumn("CT 16〜64列",      format="%d 台"),
        "CT_16列未満":      st.column_config.NumberColumn("CT 16列未満",      format="%d 台"),
        "MRI_3T以上":       st.column_config.NumberColumn("MRI 3T以上",       format="%d 台"),
        "MRI_1.5〜3T":      st.column_config.NumberColumn("MRI 1.5〜3T",      format="%d 台"),
        "MRI_1.5T未満":     st.column_config.NumberColumn("MRI 1.5T未満",     format="%d 台"),
        "内視鏡手術支援機器台数": st.column_config.NumberColumn("手術支援ロボット", format="%d 台"),
    }
    for _c in _sshow:
        _col_cfg[_c] = st.column_config.NumberColumn(format="%d 件")
    for _c in _organ_show:
        _label = _c.replace("手術_", "").replace("全麻_", "全麻:")
        _col_cfg[_c] = st.column_config.NumberColumn(_label, format="%d 件")
    for _c in _eshow:
        if _c not in _col_cfg:
            _col_cfg[_c] = st.column_config.NumberColumn(format="%d 台")

    st.dataframe(result_s, hide_index=True, use_container_width=True, column_config=_col_cfg)

    # CSVダウンロード
    st.download_button(
        "📥 CSV ダウンロード",
        result_s.to_csv(index=False).encode("utf-8-sig"),
        file_name=f"hospital_search_{s_year}.csv",
        mime="text/csv",
        key="s_csv_dl",
    )

    # ── 詳細ナビゲーション ──
    if not result_s.empty:
        st.divider()
        st.markdown("### 🏥 病院を選んで詳細を見る")
        st.caption("病院名をクリックすると、その病院の詳細分析画面に移動します。")

        # 病院名ボタングリッド
        _nav_hospitals = result_s["医療機関名"].tolist()
        _nav_cols = st.columns(3)
        for _i, _hname in enumerate(_nav_hospitals[:30]):
            with _nav_cols[_i % 3]:
                if st.button(f"🏥 {_hname}", key=f"_snav_{_i}", use_container_width=True):
                    _hrow = df[(df["医療機関名"] == _hname) & (df["報告年度"] == s_year)]
                    if not _hrow.empty:
                        _hr = _hrow.iloc[0]
                        st.session_state["_sel_year"]     = int(_hr["報告年度"])
                        st.session_state["_sel_pref"]     = str(_hr["都道府県名"])
                        st.session_state["_sel_region"]   = str(_hr["二次医療圏名"])
                        st.session_state["_sel_hospital"] = _hname
                        st.session_state["_view_mode"]    = "detail"
                        st.rerun()

        if len(_nav_hospitals) > 30:
            st.caption(f"※ 先頭30件を表示。全{len(_nav_hospitals)}件はCSVをダウンロードしてください。")

    # 検索モードはここで終了
    st.stop()


# ══════════════════════════════════════════════════════════
# 病院詳細モード（TAB1〜6）
# ══════════════════════════════════════════════════════════

# 選択病院の年次データ
hosp_row = df[
    (df["報告年度"] == year) &
    (df["医療機関名"] == hospital)
].squeeze()

# コードが取れない場合は名前で代用
hosp_code = hosp_row.get("医療機関コード") if isinstance(hosp_row, pd.Series) else None

# 地域データ
region_df = region_share(df, year, pref, region)

# 経年データ
if hosp_code and "医療機関コード" in df.columns:
    trend_df = hospital_trend(df, hosp_code)
else:
    trend_df = df[df["医療機関名"] == hospital].copy()
    trend_df = add_derived_columns(trend_df).sort_values("報告年度")


# ── ページヘッダー ─────────────────────────────────────────

_hdr_col, _btn_col = st.columns([8, 1])
with _hdr_col:
    st.markdown(f"## 🏥 {hospital}")
    st.caption(f"{year}年度　|　{pref}　{region}")
with _btn_col:
    st.markdown(
        "<div style='padding-top:16px;text-align:right'>"
        "<a class='print-btn' onclick='window.print();return false;' href='#'>🖨️ 印刷</a>"
        "</div>",
        unsafe_allow_html=True,
    )

# KPIメトリクス行
m1, m2, m3, m4, m5 = st.columns(5)

total_kyoka  = _si(hosp_row.get("合計_許可病床数", 0)) if isinstance(hosp_row, pd.Series) else 0
total_kado   = _si(hosp_row.get("合計_稼働病床数", 0)) if isinstance(hosp_row, pd.Series) else 0
total_zaitou = _si(hosp_row.get("合計_在棟延べ数", 0)) if isinstance(hosp_row, pd.Series) else 0
doctors      = _si(hosp_row.get("常勤医師数", 0)) if isinstance(hosp_row, pd.Series) else 0
nurses       = _si(hosp_row.get("常勤看護師数", 0)) if isinstance(hosp_row, pd.Series) else 0

if total_zaitou > 0 and total_kyoka > 0:
    occ = total_zaitou / 365 / total_kyoka
    kado_sub = f"平均在棟 {total_zaitou // 365:,}人/日"
elif total_kyoka > 0:
    occ = total_kado / total_kyoka
    kado_sub = f"稼働 {total_kado:,}床"
else:
    occ = 0
    kado_sub = ""

region_rank_row = region_df[region_df["医療機関名"] == hospital]
region_rank = int(region_rank_row["地域内順位"].values[0]) if len(region_rank_row) > 0 else "-"
region_share_val = float(region_rank_row["地域シェア(%)"].values[0]) if len(region_rank_row) > 0 else 0

def kpi_card(col, label, value, sub="", color="#3498db"):
    col.markdown(f"""
    <div class="metric-card" style="border-left-color:{color}">
      <div class="metric-label">{label}</div>
      <div class="metric-value">{value}</div>
      <div class="metric-sub">{sub}</div>
    </div>""", unsafe_allow_html=True)

kpi_card(m1, "許可病床数", f"{total_kyoka:,}床", kado_sub)
kpi_card(m2, "総稼働率", f"{occ*100:.1f}%", "")
kpi_card(m3, "地域内順位", f"{region_rank}位", f"/ {len(region_df)}院中")
kpi_card(m4, "地域シェア", f"{region_share_val:.1f}%", "許可病床数ベース")
kpi_card(m5, "常勤医師数", f"{doctors}人", f"看護師 {nurses}人")

st.markdown("<br>", unsafe_allow_html=True)


# ── タブ ──────────────────────────────────────────────────

tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
    "📊 病院概要",
    "🏆 地域比較",
    "📋 ランキング",
    "📈 経年トレンド",
    "👨‍⚕️ スタッフ分析",
    "📋 詳細分析",
])


# ── TAB 1: 病院概要 ─────────────────────────────────────────

with tab1:
    if not isinstance(hosp_row, pd.Series):
        st.warning("選択した年度のデータが見つかりません")
    else:
        c1, c2 = st.columns([1, 1])
        with c1:
            st.plotly_chart(bed_donut(hosp_row, hospital), use_container_width=True)
        with c2:
            st.plotly_chart(occupancy_gauge(occ, "総稼働率"), use_container_width=True)

        st.plotly_chart(bed_type_occupancy_bar(hosp_row, hospital), use_container_width=True)

        st.markdown('<div class="section-header">病床種別詳細</div>', unsafe_allow_html=True)
        def _safe_int(val):
            try:
                return int(val or 0)
            except (ValueError, TypeError):
                return 0

        detail_rows = []
        for t in BED_TYPES:
            k    = _safe_int(hosp_row.get(f"{t}_許可病床数", 0))
            z    = _safe_int(hosp_row.get(f"{t}_在棟延べ数", 0))
            comp = bed_composition(hosp_row)[t]
            avg      = f"{z / 365:.1f}" if z > 0 else "—"
            occ_rate = f"{z / 365 / k * 100:.1f}%" if (z > 0 and k > 0) else "—"
            detail_rows.append({
                "病床種別":          t,
                "許可病床数（床）":  k,
                "平均在棟患者数/日": avg,
                "病床稼働率(%)":     occ_rate,
                "構成比（%）":       f"{comp:.1f}%",
            })
        st.dataframe(
            pd.DataFrame(detail_rows),
            hide_index=True,
            use_container_width=True,
            column_config={
                "許可病床数（床）": st.column_config.NumberColumn(format="%d 床"),
            },
        )

        if "救急搬送件数" in hosp_row and hosp_row["救急搬送件数"] > 0:
            st.markdown('<div class="section-header">診療実績</div>', unsafe_allow_html=True)
            r1, r2 = st.columns(2)
            r1.metric("救急搬送件数（年間）", f"{int(hosp_row['救急搬送件数']):,}件")
            if "手術件数" in hosp_row:
                r2.metric("手術件数（年間）", f"{int(hosp_row['手術件数']):,}件")

        # ── 医療設備セクション ──────────────────────
        def _ev(col):
            if not isinstance(hosp_row, pd.Series) or col not in hosp_row.index:
                return None
            val = hosp_row.get(col, 0)
            try:
                return int(val or 0)
            except (ValueError, TypeError):
                return 0

        CT_BREAKDOWN  = {"CT_64列以上": "64列以上", "CT_16〜64列": "16〜64列",
                         "CT_16列未満": "16列未満", "CT_その他": "その他"}
        MRI_BREAKDOWN = {"MRI_3T以上": "3T以上", "MRI_1.5〜3T": "1.5〜3T",
                         "MRI_1.5T未満": "1.5T未満"}
        OTHER_EQUIP   = {
            "PET台数":             "PET",
            "PETCT台数":           "PET-CT",
            "PETMRI台数":          "PET-MRI",
            "内視鏡手術支援機器台数": "内視鏡手術支援ロボット",
            "IMRT台数":            "IMRT（強度変調放射線治療）",
            "ガンマナイフ台数":     "ガンマナイフ",
            "サイバーナイフ台数":   "サイバーナイフ",
            "血管連続撮影装置台数": "血管造影",
            "SPECT台数":           "SPECT",
            "マンモグラフィ台数":   "マンモグラフィ",
        }

        all_equip_cols = (["CT台数"] + list(CT_BREAKDOWN) +
                          ["MRI台数"] + list(MRI_BREAKDOWN) +
                          list(OTHER_EQUIP))
        has_equip = isinstance(hosp_row, pd.Series) and any(c in hosp_row.index for c in all_equip_cols)

        if has_equip:
            st.markdown('<div class="section-header">医療設備（モダリティ）</div>', unsafe_allow_html=True)

            def _modality_card(title: str, accent: str, total: int, breakdown: dict) -> str:
                items_html = "".join(
                    f'<div style="flex:1;text-align:center;padding:0 6px;'
                    f'border-right:1px solid rgba(255,255,255,0.07);">'
                    f'<div style="color:#8899aa;font-size:0.68rem;margin-bottom:3px;">{lbl}</div>'
                    f'<div style="color:white;font-size:1.05rem;font-weight:600;">{val}台</div>'
                    f'</div>'
                    for lbl, val in breakdown.items()
                )
                return (
                    f'<div style="background:linear-gradient(135deg,#1a2133,#1e2840);'
                    f'border-left:5px solid {accent};border-radius:10px;'
                    f'padding:14px 18px;margin-bottom:10px;">'
                    f'<div style="color:{accent};font-size:0.78rem;font-weight:700;'
                    f'letter-spacing:.4px;margin-bottom:6px;">{title}</div>'
                    f'<div style="display:flex;align-items:baseline;gap:3px;margin-bottom:10px;">'
                    f'<span style="color:white;font-size:2.2rem;font-weight:700;">{total}</span>'
                    f'<span style="color:#8899aa;font-size:0.9rem;margin-left:2px;">台</span>'
                    f'</div>'
                    f'<div style="display:flex;border-top:1px solid rgba(255,255,255,0.07);padding-top:8px;">'
                    f'{items_html}'
                    f'</div></div>'
                )

            def _equip_badge(label: str, val: int) -> str:
                return (
                    f'<div style="background:#1a2133;border:1px solid rgba(255,255,255,0.1);'
                    f'border-radius:8px;padding:10px 14px;text-align:center;">'
                    f'<div style="color:#8899aa;font-size:0.72rem;margin-bottom:4px;">{label}</div>'
                    f'<div style="color:white;font-size:1.4rem;font-weight:700;">{val}'
                    f'<span style="font-size:0.75rem;color:#8899aa;margin-left:2px;">台</span></div>'
                    f'</div>'
                )

            ct_total = _ev("CT台数") or 0
            has_ct   = any(_ev(c) is not None for c in CT_BREAKDOWN) or _ev("CT台数") is not None
            if has_ct:
                breakdown_ct = {lbl: _ev(col) or 0 for col, lbl in CT_BREAKDOWN.items()}
                st.markdown(
                    _modality_card("🔵 CT（コンピューター断層撮影装置）", "#3498db", ct_total, breakdown_ct),
                    unsafe_allow_html=True,
                )

            mri_total = _ev("MRI台数") or 0
            has_mri   = any(_ev(c) is not None for c in MRI_BREAKDOWN) or _ev("MRI台数") is not None
            if has_mri:
                breakdown_mri = {lbl: _ev(col) or 0 for col, lbl in MRI_BREAKDOWN.items()}
                st.markdown(
                    _modality_card("🔴 MRI（磁気共鳴画像診断装置）", "#e74c3c", mri_total, breakdown_mri),
                    unsafe_allow_html=True,
                )

            other_data = {lbl: (_ev(col) or 0) for col, lbl in OTHER_EQUIP.items() if _ev(col) is not None}
            if other_data:
                items = list(other_data.items())
                for row_start in range(0, len(items), 4):
                    row_items = items[row_start:row_start + 4]
                    badge_html = (
                        '<div style="display:grid;grid-template-columns:repeat('
                        + str(len(row_items))
                        + ',1fr);gap:8px;margin-bottom:8px;">'
                        + "".join(_equip_badge(lbl, val) for lbl, val in row_items)
                        + "</div>"
                    )
                    st.markdown(badge_html, unsafe_allow_html=True)


# ── TAB 2: 地域比較 ─────────────────────────────────────────

with tab2:
    st.markdown(f"**{pref}　{region}　{year}年度 — {len(region_df)}院**")

    c1, c2 = st.columns([3, 2])
    with c1:
        st.plotly_chart(regional_bed_comparison(region_df, hospital), use_container_width=True)
    with c2:
        st.plotly_chart(share_bar(region_df, hospital), use_container_width=True)

    st.plotly_chart(occupancy_scatter(region_df, hospital), use_container_width=True)

    st.markdown('<div class="section-header">地域全体の集計</div>', unsafe_allow_html=True)
    agg = {
        "総病床数（許可）": region_df["合計_許可病床数"].sum(),
        "総病床数（稼働）": region_df["合計_稼働病床数"].sum(),
        "平均稼働率": f"{(region_df['合計_稼働病床数'].sum() / region_df['合計_許可病床数'].sum() * 100):.1f}%",
        "病院数": len(region_df),
    }
    for t in BED_TYPES:
        col = f"{t}_許可病床数"
        if col in region_df.columns:
            agg[f"{t}（許可）"] = region_df[col].sum()

    a_cols = st.columns(len(agg))
    for col, (k, v) in zip(a_cols, agg.items()):
        col.metric(k, v if isinstance(v, str) else f"{v:,}")


# ── TAB 3: ランキング ──────────────────────────────────────

with tab3:
    st.markdown(f"**{pref}　{region}　{year}年度**")
    st.plotly_chart(ranking_table_fig(region_df, hospital), use_container_width=True)

    st.markdown('<div class="section-header">稼働率ランキング</div>', unsafe_allow_html=True)
    occ_df = region_df.copy()
    occ_df["稼働率(%)"] = (
        occ_df["合計_稼働病床数"] / occ_df["合計_許可病床数"].replace(0, np.nan) * 100
    ).round(1)
    occ_rank = occ_df[["医療機関名", "稼働率(%)", "合計_許可病床数", "合計_稼働病床数"]].sort_values(
        "稼働率(%)", ascending=False
    ).reset_index(drop=True)
    occ_rank.index += 1
    occ_rank.index.name = "順位"
    st.dataframe(occ_rank, use_container_width=True)


# ── TAB 4: 経年トレンド ────────────────────────────────────

with tab4:
    if len(trend_df) < 2:
        st.info("経年比較には複数年度のデータが必要です。サンプルデータは4年分（2020〜2023年度）含まれています。")
    else:
        c1, c2 = st.columns(2)
        with c1:
            st.plotly_chart(trend_beds(trend_df, hospital), use_container_width=True)
        with c2:
            st.plotly_chart(trend_occupancy(trend_df, hospital), use_container_width=True)

        st.plotly_chart(trend_staff(trend_df, hospital), use_container_width=True)

        st.markdown('<div class="section-header">年度別データ一覧</div>', unsafe_allow_html=True)
        disp_cols = ["報告年度", "合計_許可病床数", "合計_稼働病床数"]
        for t in BED_TYPES:
            if f"{t}_許可病床数" in trend_df.columns:
                disp_cols.append(f"{t}_許可病床数")
        if "常勤医師数" in trend_df.columns:
            disp_cols += ["常勤医師数", "常勤看護師数"]
        st.dataframe(trend_df[disp_cols].reset_index(drop=True), hide_index=True, use_container_width=True)

        if len(trend_df) >= 2:
            first_y = trend_df.iloc[0]
            last_y  = trend_df.iloc[-1]
            delta_beds = int(last_y["合計_許可病床数"]) - int(first_y["合計_許可病床数"])
            delta_occ  = (
                last_y["合計_稼働病床数"] / max(last_y["合計_許可病床数"], 1) -
                first_y["合計_稼働病床数"] / max(first_y["合計_許可病床数"], 1)
            ) * 100
            st.markdown('<div class="section-header">期間内変化サマリー</div>', unsafe_allow_html=True)
            sc1, sc2, sc3 = st.columns(3)
            sc1.metric(
                f"許可病床数 ({int(first_y['報告年度'])}→{int(last_y['報告年度'])})",
                f"{int(last_y['合計_許可病床数']):,}床",
                f"{delta_beds:+,}床",
            )
            sc2.metric(
                "稼働率変化",
                f"{last_y['合計_稼働病床数'] / max(last_y['合計_許可病床数'],1)*100:.1f}%",
                f"{delta_occ:+.1f}pt",
            )
            if "常勤医師数" in trend_df.columns:
                delta_doc = int(last_y["常勤医師数"]) - int(first_y["常勤医師数"])
                sc3.metric("常勤医師数変化", f"{int(last_y['常勤医師数'])}人", f"{delta_doc:+,}人")


# ── TAB 5: スタッフ分析 ────────────────────────────────────

with tab5:
    has_staff = "常勤医師数" in region_df.columns and "常勤看護師数" in region_df.columns

    if not has_staff:
        st.info("スタッフデータが含まれていません")
    else:
        region_df_staff = add_derived_columns(region_df)

        st.plotly_chart(staff_scatter(region_df_staff, hospital), use_container_width=True)

        c1, c2 = st.columns(2)
        with c1:
            st.plotly_chart(
                staff_bar_region(region_df_staff, hospital, "医師数_per100床", "医師数"),
                use_container_width=True,
            )
        with c2:
            st.plotly_chart(
                staff_bar_region(region_df_staff, hospital, "看護師数_per100床", "看護師数"),
                use_container_width=True,
            )

        st.markdown('<div class="section-header">選択病院 vs 地域平均</div>', unsafe_allow_html=True)
        if len(region_df_staff) > 0:
            metrics = ["医師数_per100床", "看護師数_per100床"]
            hosp_vals = region_df_staff[region_df_staff["医療機関名"] == hospital][metrics].squeeze()
            region_means = region_df_staff[metrics].mean()

            sv1, sv2 = st.columns(2)
            for sv, m, label in zip(
                [sv1, sv2], metrics, ["医師数（per 100床）", "看護師数（per 100床）"]
            ):
                if isinstance(hosp_vals, pd.Series) and m in hosp_vals:
                    hv = hosp_vals[m]
                    rv = region_means[m]
                    sv.metric(
                        label,
                        f"{hv:.1f}人",
                        f"{hv - rv:+.1f}（地域平均比）",
                    )


# ── TAB 6: 詳細分析 ────────────────────────────────────────

with tab6:
    ward_df = st.session_state.ward_df

    if ward_df is None:
        st.info("病棟単位の詳細データがありません。厚労省様式1・2病棟票を再読み込みしてください。")
    else:
        hosp_ward = ward_df[
            (ward_df["医療機関名"] == hospital) &
            (ward_df["報告年度"] == year)
        ]

        if hosp_ward.empty:
            st.info("選択した病院・年度の病棟データが見つかりません。データを再読み込みしてください。")
        else:
            st.markdown('<div class="section-header">入院基本料別病床数</div>', unsafe_allow_html=True)
            bed_tbl = detail_bed_type_table(hosp_ward, hospital)
            if not bed_tbl.empty:
                st.dataframe(bed_tbl, hide_index=True, use_container_width=True)
            else:
                st.info("病棟テーブルデータがありません。")

            st.markdown("<br>", unsafe_allow_html=True)

            c1, c2 = st.columns(2)
            with c1:
                st.plotly_chart(admission_route_pie(hosp_ward, hospital), use_container_width=True)
            with c2:
                st.plotly_chart(discharge_route_pie(hosp_ward, hospital), use_container_width=True)

            st.markdown('<div class="section-header">在宅復帰率</div>', unsafe_allow_html=True)
            total_taitou = float(hosp_ward["退棟患者数"].sum())
            total_katei  = float(hosp_ward["家庭退院数"].sum())
            home_rate = total_katei / total_taitou if total_taitou > 0 else 0

            hr1, hr2, hr3 = st.columns(3)
            hr1.metric("退棟患者数（年間）",   f"{int(total_taitou):,}人")
            hr2.metric("家庭退院数（年間）",   f"{int(total_katei):,}人")
            hr3.metric("在宅復帰率",           f"{home_rate * 100:.1f}%")

            st.markdown("<br>", unsafe_allow_html=True)

            st.markdown('<div class="section-header">地域内 在宅復帰率比較</div>', unsafe_allow_html=True)
            region_ward = ward_df[
                (ward_df["二次医療圏名"] == region) &
                (ward_df["報告年度"] == year)
            ] if "二次医療圏名" in ward_df.columns else ward_df[ward_df["報告年度"] == year]

            if not region_ward.empty:
                st.plotly_chart(
                    home_return_rate_bar(region_ward, hospital, region),
                    use_container_width=True,
                )
            else:
                st.info("地域内比較データがありません。")

    # ── 手術データセクション ──
    surgery_df = st.session_state.surgery_df
    st.divider()
    st.markdown('<div class="section-header">手術実績（様式2年間合計）</div>', unsafe_allow_html=True)

    if surgery_df is None:
        st.info("手術データがありません。「データを更新する」から再ダウンロードしてください。")
    else:
        hosp_surg = surgery_df[surgery_df["医療機関名"] == hospital]

        if hosp_surg.empty:
            st.info("この病院の手術データが見つかりません（手術件数0または非公表）。")
        else:
            surg_row = hosp_surg.iloc[0]

            SURG_COLS = {
                "手術総数":       "手術総数",
                "全身麻酔手術数": "全身麻酔",
                "腹腔鏡下手術数": "腹腔鏡下",
                "胸腔鏡下手術数": "胸腔鏡下",
                "ロボット支援手術数": "ロボット支援",
                "悪性腫瘍手術数": "悪性腫瘍",
                "脳血管内手術数": "脳血管内",
                "人工心肺手術数": "人工心肺",
            }

            kpi_cols = st.columns(4)
            for (col, label), kpi in zip(list(SURG_COLS.items())[:4], kpi_cols):
                val = _si(surg_row.get(col, 0))
                kpi.metric(label, f"{val:,}件")

            kpi_cols2 = st.columns(4)
            for (col, label), kpi in zip(list(SURG_COLS.items())[4:], kpi_cols2):
                val = _si(surg_row.get(col, 0))
                kpi.metric(label, f"{val:,}件")

            total = _si(surg_row.get("手術総数", 0))
            if total > 0:
                import plotly.graph_objects as go
                detail_cols = {k: v for k, v in SURG_COLS.items() if k != "手術総数"}
                vals = [_si(surg_row.get(c, 0)) for c in detail_cols]
                labels = list(detail_cols.values())
                pcts = [round(v / total * 100, 1) for v in vals]
                fig_surg = go.Figure(go.Bar(
                    x=vals, y=labels, orientation="h",
                    marker_color="#3498db",
                    text=[f"{v:,}件 ({p}%)" for v, p in zip(vals, pcts)],
                    textposition="auto",
                ))
                fig_surg.update_layout(
                    title=f"手術内訳（総数 {total:,}件）",
                    height=320, margin=dict(l=10, r=10, t=50, b=10),
                    font=dict(family="Meiryo, sans-serif"),
                    paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                )
                st.plotly_chart(fig_surg, use_container_width=True)

            st.markdown('<div class="section-header">二次医療圏内 手術数シェア</div>', unsafe_allow_html=True)
            region_surg = surgery_df[surgery_df["二次医療圏名"] == region] if "二次医療圏名" in surgery_df.columns else pd.DataFrame()

            if not region_surg.empty and region_surg["手術総数"].sum() > 0:
                import plotly.graph_objects as go
                region_total = region_surg["手術総数"].sum()
                region_surg = region_surg.copy()
                region_surg["シェア(%)"] = (region_surg["手術総数"] / region_total * 100).round(1)
                region_surg["全身麻酔率(%)"] = (
                    region_surg["全身麻酔手術数"] / region_surg["手術総数"].replace(0, np.nan) * 100
                ).round(1)
                region_surg = region_surg.sort_values("手術総数", ascending=True)

                colors = ["#e74c3c" if n == hospital else "#3498db" for n in region_surg["医療機関名"]]
                fig_share = go.Figure(go.Bar(
                    x=region_surg["手術総数"], y=region_surg["医療機関名"],
                    orientation="h",
                    marker_color=colors,
                    text=region_surg["シェア(%)"].apply(lambda v: f"{v:.1f}%"),
                    textposition="auto",
                    hovertemplate="%{y}: %{x:,}件<extra></extra>",
                ))
                fig_share.update_layout(
                    title=f"{region} 手術数比較（地域計 {int(region_total):,}件）",
                    height=max(350, len(region_surg) * 26 + 80),
                    margin=dict(l=10, r=10, t=50, b=10),
                    font=dict(family="Meiryo, sans-serif"),
                    paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                )
                st.plotly_chart(fig_share, use_container_width=True)

                tbl = region_surg[["医療機関名", "手術総数", "全身麻酔手術数", "シェア(%)", "全身麻酔率(%)"]].sort_values("手術総数", ascending=False).reset_index(drop=True)
                tbl.index += 1
                st.dataframe(tbl, use_container_width=True)
            else:
                st.info("この二次医療圏の手術データがありません。")
