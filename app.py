"""
病床機能報告 分析・比較ツール
"""
import streamlit as st
import streamlit.components.v1 as components
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
/* ── サイドバー 共通 ── */
div[data-testid="stSidebar"] .stButton button {
    text-align: left;
    font-size: 0.82rem;
    padding: 4px 8px;
    height: auto;
    white-space: normal;
    word-break: break-all;
}

/* ── アコーディオン: 非アクティブ（▶） ── */
div[data-testid="stSidebar"] .stButton > button[data-testid="baseButton-secondary"] {
    background: #f8fafc !important;
    border: 1px solid #e2e8f0 !important;
    border-left: 3px solid transparent !important;
    color: #64748b !important;
    font-weight: 500 !important;
    border-radius: 6px !important;
    font-size: 0.84rem !important;
    padding: 9px 12px !important;
}
div[data-testid="stSidebar"] .stButton > button[data-testid="baseButton-secondary"]:hover {
    background: #f1f5f9 !important;
    color: #1e293b !important;
    border-left-color: #94a3b8 !important;
}

/* ── アコーディオン: アクティブ（▼） ── */
div[data-testid="stSidebar"] .stButton > button[data-testid="baseButton-primary"] {
    background: #eff6ff !important;
    border: 1px solid #bfdbfe !important;
    border-left: 4px solid #2563eb !important;
    color: #1e40af !important;
    font-weight: 700 !important;
    border-radius: 6px !important;
    font-size: 0.84rem !important;
    padding: 9px 11px !important;
    box-shadow: 0 1px 4px rgba(37,99,235,0.10) !important;
}
div[data-testid="stSidebar"] .stButton > button[data-testid="baseButton-primary"]:hover {
    background: #dbeafe !important;
    border-left-color: #1d4ed8 !important;
    box-shadow: 0 2px 8px rgba(37,99,235,0.18) !important;
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
    .print-btn,
    iframe[title="streamlit_components_v1_html"] {
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

/* ══════════════════════════════════════════════════════
   スマートフォン対応
══════════════════════════════════════════════════════ */
@media (max-width: 768px) {

    /* ── コンテンツ余白縮小 ── */
    .main .block-container {
        padding-left: 0.6rem !important;
        padding-right: 0.6rem !important;
        padding-top: 0.5rem !important;
    }

    /* ── KPI カード: 5列を小画面向けに縮小 ── */
    .metric-card  { padding: 10px 6px !important; }
    .metric-value { font-size: 1.1rem !important; }
    .metric-label { font-size: 0.65rem !important; }
    .metric-sub   { font-size: 0.6rem  !important; }

    /* ── セクションヘッダー ── */
    .section-header { font-size: 0.92rem !important; }

    /* ── ページ見出し ── */
    h2 { font-size: 1.2rem !important; }

    /* ── タブ: 横スクロール（6タブがはみ出ないように） ── */
    .stTabs [role="tablist"] {
        overflow-x: auto !important;
        flex-wrap: nowrap !important;
        -webkit-overflow-scrolling: touch;
        scrollbar-width: none;
        padding-bottom: 2px;
    }
    .stTabs [role="tablist"]::-webkit-scrollbar { display: none; }
    .stTabs [role="tab"] {
        font-size: 0.7rem !important;
        padding: 6px 10px !important;
        white-space: nowrap !important;
        min-width: auto !important;
    }

    /* ── iOS入力ズーム防止 ──
       iOSはfont-size < 16pxの入力欄にフォーカスすると自動ズームする */
    input[type="text"],
    input[type="search"],
    input[type="number"],
    select,
    textarea {
        font-size: 16px !important;
    }

    /* ── ボタン: タップ領域を広げる（推奨44px以上） ── */
    .stButton > button {
        min-height: 44px !important;
    }
    /* サイドバーの検索結果ボタン */
    div[data-testid="stSidebar"] .stButton button {
        min-height: 38px !important;
        font-size: 0.82rem !important;
    }

    /* ── データフレーム: 横スクロール ── */
    [data-testid="stDataFrame"] {
        overflow-x: auto !important;
        -webkit-overflow-scrolling: touch;
    }

    /* ── 印刷ボタン（component iframe）: モバイルでは非表示 ── */
    iframe[title="streamlit_components_v1_html"] {
        display: none !important;
    }
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
if "_sb_open" not in st.session_state:
    st.session_state["_sb_open"] = "③"


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

    # ── データあり: アコーディオンナビゲーション ──
    if st.session_state.df is not None:
        _df_all = st.session_state.df
        _sb_open = st.session_state.get("_sb_open", "③")

        # ナビゲーションジャンプを最初に処理（どのパネルが開いていても機能する）
        _nav = st.session_state.pop("_nav_jump", None)
        if _nav:
            st.session_state["_sel_year"]     = int(_nav["year"])
            st.session_state["_sel_pref"]     = str(_nav["pref"])
            st.session_state["_sel_region"]   = str(_nav["region"])
            st.session_state["_sel_hospital"] = str(_nav["hospital"])
            st.session_state["_nav_done"]     = str(_nav["hospital"])
            st.session_state["_sb_open"]      = "③"
            st.rerun()

        # アコーディオンヘッダー補助関数
        def _sb_section(label: str, key: str) -> bool:
            is_open = _sb_open == key
            icon = "▼" if is_open else "▶"
            if st.button(f"{icon} {label}", use_container_width=True, key=f"_sbhdr_{key}",
                         type="primary" if is_open else "secondary"):
                st.session_state["_sb_open"] = key if not is_open else None
                st.rerun()
            return is_open

        st.divider()
        st.markdown(
            "<div style='font-size:0.82rem;font-weight:700;color:#2c3e50;"
            "margin-bottom:4px;'>Ⅰ. 病院の情報を調べる</div>",
            unsafe_allow_html=True,
        )

        # ── ① 病院名で探す ──
        if _sb_section("① 病院名で探す", "①"):
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
                            st.session_state["_nav_jump"] = {
                                "year":     int(_mrow["報告年度"]),
                                "pref":     str(_mrow["都道府県名"]),
                                "region":   str(_mrow["二次医療圏名"]),
                                "hospital": str(_mrow["医療機関名"]),
                            }
                            st.session_state["_view_mode"] = "detail"
                            st.session_state["_sb_open"]   = "③"
                            st.rerun()
                    if len(_matched) > 12:
                        st.caption(f"… 他 {len(_matched)-12}件（絞り込んでください）")
            else:
                st.caption("病院名の一部を入力してください")

        # ── ② 条件検索で探す ──
        if _sb_section("② 条件検索で探す", "②"):
            st.caption("手術・設備などの条件で絞り込み")
            _is_search = st.session_state.get("_view_mode") == "search"
            if _is_search:
                st.success("条件検索モード中")
                if st.button("← 病院詳細に戻る", use_container_width=True, type="secondary"):
                    st.session_state["_view_mode"] = "detail"
                    st.session_state["_sb_open"]   = "③"
                    st.rerun()
            else:
                if st.button("🔧 条件検索を開く", use_container_width=True, type="primary"):
                    st.session_state["_view_mode"] = "search"
                    st.rerun()

        # ── ③ 地域から探す ──
        if _sb_section("③ 地域から探す", "③"):
            with st.container(border=True):
                years = [int(y) for y in sorted(_df_all["報告年度"].dropna().unique(), reverse=True)]
                sel_year = st.selectbox("報告年度", years, key="_sel_year")

                prefs = _sort_prefs(_df_all["都道府県名"].unique())
                if st.session_state.get("_sel_pref") not in prefs:
                    st.session_state["_sel_pref"] = prefs[0] if prefs else None
                sel_pref = st.selectbox("都道府県", prefs, key="_sel_pref")

                regions = sorted(
                    r for r in _df_all[_df_all["都道府県名"] == sel_pref]["二次医療圏名"].unique()
                    if r != "不明"
                )
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

        else:
            # ③が閉じているとき: sel_* をセッションステートから復元
            _df_years = [int(y) for y in sorted(_df_all["報告年度"].dropna().unique(), reverse=True)]
            sel_year   = st.session_state.get("_sel_year",   _df_years[0] if _df_years else 2023)
            sel_pref   = st.session_state.get("_sel_pref",   _sort_prefs(_df_all["都道府県名"].unique())[0])
            _r_list    = sorted(
                r for r in _df_all[_df_all["都道府県名"] == sel_pref]["二次医療圏名"].unique()
                if r != "不明"
            )
            sel_region  = st.session_state.get("_sel_region",  _r_list[0] if _r_list else "")
            _h_list     = _df_all[
                (_df_all["報告年度"] == sel_year) &
                (_df_all["都道府県名"] == sel_pref) &
                (_df_all["二次医療圏名"] == sel_region)
            ]["医療機関名"].sort_values().tolist()
            sel_hospital = st.session_state.get("_sel_hospital", _h_list[0] if _h_list else "")

        st.caption(f"全 {len(_df_all[_df_all['報告年度']==sel_year]):,} 病院 | {sel_year}年度")

        # ── Ⅱ. 地域医療の状況を調べる ──
        st.divider()
        st.markdown(
            "<div style='font-size:0.82rem;font-weight:700;color:#2c3e50;"
            "margin-bottom:4px;'>Ⅱ. 地域医療の状況を調べる</div>",
            unsafe_allow_html=True,
        )

        _rv_years_list = [int(y) for y in sorted(_df_all["報告年度"].dropna().unique(), reverse=True)]
        rv_sel_year = st.selectbox(
            "分析年度", _rv_years_list, key="_rv_sel_year",
            help="地域構想分析の対象年度（病院選択とは独立して設定できます）",
        )

        _rv_all_prefs = _sort_prefs(_df_all["都道府県名"].unique())
        if st.session_state.get("_rv_sel_pref") not in _rv_all_prefs:
            st.session_state["_rv_sel_pref"] = _rv_all_prefs[0] if _rv_all_prefs else None
        rv_sel_pref = st.selectbox(
            "都道府県", _rv_all_prefs, key="_rv_sel_pref",
            help="地域構想分析の対象都道府県",
        )

        _rv_regions_list = sorted(
            r for r in _df_all[_df_all["都道府県名"] == rv_sel_pref]["二次医療圏名"].unique()
            if r != "不明"
        )
        if st.session_state.get("_rv_sel_region") not in _rv_regions_list:
            st.session_state["_rv_sel_region"] = _rv_regions_list[0] if _rv_regions_list else None
        rv_sel_region = st.selectbox(
            "二次医療圏", _rv_regions_list, key="_rv_sel_region",
            help="地域構想分析の対象二次医療圏",
        )

        _is_vision = st.session_state.get("_view_mode") == "region_vision"
        if _is_vision:
            if st.button("← 病院詳細に戻る", key="_vision_back_btn",
                         use_container_width=True, type="secondary"):
                st.session_state["_view_mode"] = "detail"
                st.rerun()
        else:
            if st.button("🗺️ 地域構想分析を見る", key="_vision_open_btn",
                         use_container_width=True, type="secondary"):
                st.session_state["_view_mode"] = "region_vision"
                st.rerun()

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
                        # サイドバーのselectbox描画後にwidgetキーを直接書き換えると
                        # StreamlitAPIExceptionが出るため _nav_jump 経由で渡す
                        st.session_state["_nav_jump"] = {
                            "year":     int(_hr["報告年度"]),
                            "pref":     str(_hr["都道府県名"]),
                            "region":   str(_hr["二次医療圏名"]),
                            "hospital": _hname,
                        }
                        st.session_state["_view_mode"] = "detail"
                        st.rerun()

        if len(_nav_hospitals) > 30:
            st.caption(f"※ 先頭30件を表示。全{len(_nav_hospitals)}件はCSVをダウンロードしてください。")

    # 検索モードはここで終了
    st.stop()


# ══════════════════════════════════════════════════════════
# 地域医療構想分析モード
# ══════════════════════════════════════════════════════════

if st.session_state.get("_view_mode") == "region_vision":
    import plotly.graph_objects as _go_rv

    # サイドバーの専用セレクタ値を使用（なければ現在の病院選択から補完）
    _rv_year   = int(st.session_state.get("_rv_sel_year",  year))
    _rv_pref   = str(st.session_state.get("_rv_sel_pref",  pref))
    _rv_region = str(st.session_state.get("_rv_sel_region", region))

    # ── ヘッダー
    st.markdown(f"## 🗺️ {_rv_region} 地域医療構想分析")
    st.caption(
        f"{_rv_year}年度データ　|　{_rv_pref}　{_rv_region}　"
        "※ 本分析は病床機能報告データに基づく参考情報です。"
        "実際の構想策定には一次データ・専門家の関与が必要です。"
    )

    # ── 地域内全病院データ
    rv_df = df[
        (df["報告年度"] == _rv_year) &
        (df["二次医療圏名"] == _rv_region)
    ].copy().reset_index(drop=True)

    if rv_df.empty:
        st.warning("選択した二次医療圏のデータが見つかりません。サイドバーで二次医療圏を選択してください。")
        st.stop()

    # 手術データマップ {医療機関名: 手術総数 / ロボット手術数}
    _surg_df_rv = st.session_state.get("surgery_df")
    _surg_map_rv   = {}   # 医療機関名 → 手術総数
    _robot_map_rv  = {}   # 医療機関名 → ロボット支援手術数
    if _surg_df_rv is not None and not _surg_df_rv.empty:
        _rv_smask = pd.Series(True, index=_surg_df_rv.index)
        if "二次医療圏名" in _surg_df_rv.columns:
            _rv_smask = _rv_smask & (_surg_df_rv["二次医療圏名"] == _rv_region)
        if "報告年度" in _surg_df_rv.columns:
            _rv_smask = _rv_smask & (_surg_df_rv["報告年度"] == _rv_year)
        for _, _sr in _surg_df_rv[_rv_smask].iterrows():
            _hn = str(_sr.get("医療機関名", ""))
            _surg_map_rv[_hn]  = int(_sr.get("手術総数", 0) or 0)
            _robot_map_rv[_hn] = int(_sr.get("ロボット支援手術数", 0) or 0)

    # ════════════════════════════════════
    # スコアリング・分類関数
    # ════════════════════════════════════

    def _kyoten_score_rv(row):
        """急性期拠点機能スコア（0〜100点）を計算して返す"""
        s = {}
        beds  = _si(row.get("合計_許可病床数", 0))
        koudo = _si(row.get("高度急性期_許可病床数", 0))
        docs  = _si(row.get("常勤医師数", 0))
        hn    = str(row.get("医療機関名", ""))

        # ① 病床規模 (0〜25点)
        if   beds >= 500: s["病床規模"] = 25
        elif beds >= 400: s["病床規模"] = 20
        elif beds >= 300: s["病床規模"] = 15
        elif beds >= 200: s["病床規模"] = 10
        elif beds >= 100: s["病床規模"] = 5
        else:             s["病床規模"] = 0

        # ② 高度急性期病床率 (0〜20点)
        koudo_r = koudo / beds if beds > 0 else 0
        if   koudo_r >= 0.30: s["高度急性期率"] = 20
        elif koudo_r >= 0.15: s["高度急性期率"] = 13
        elif koudo_r >= 0.05: s["高度急性期率"] = 6
        else:                 s["高度急性期率"] = 0

        # ③ 手術実績 (0〜25点)
        surg = _surg_map_rv.get(hn, 0)
        if   surg >= 3000: s["手術実績"] = 25
        elif surg >= 2000: s["手術実績"] = 20
        elif surg >= 1000: s["手術実績"] = 14
        elif surg >= 500:  s["手術実績"] = 8
        elif surg >= 100:  s["手術実績"] = 3
        else:              s["手術実績"] = 0

        # ④ 医師密度（常勤医師数 / 100床） (0〜20点)
        doc100 = docs / beds * 100 if beds > 0 else 0
        if   doc100 >= 25: s["医師密度"] = 20
        elif doc100 >= 15: s["医師密度"] = 14
        elif doc100 >= 8:  s["医師密度"] = 8
        elif doc100 >= 4:  s["医師密度"] = 4
        else:              s["医師密度"] = 0

        # ⑤ 高度設備 (0〜10点)
        eq = 0
        if _robot_map_rv.get(hn, 0) > 0:           eq += 4
        if _si(row.get("CT_64列以上", 0)) > 0:     eq += 2
        if _si(row.get("MRI_3T以上",  0)) > 0:     eq += 2
        _pet = _si(row.get("PET台数", 0)) + _si(row.get("PETCT台数", 0))
        if _pet > 0:                                eq += 2
        s["高度設備"] = min(eq, 10)

        return sum(s.values()), s

    def _classify_role_rv(row, rank, score, n_total):
        """機能方向性を分類してラベルとコメントを返す"""
        beds    = _si(row.get("合計_許可病床数", 0))
        koudo   = _si(row.get("高度急性期_許可病床数", 0))
        kyusei  = _si(row.get("急性期_許可病床数", 0))
        kaifuku = _si(row.get("回復期_許可病床数", 0))
        mansei  = _si(row.get("慢性期_許可病床数", 0))
        hn      = str(row.get("医療機関名", ""))

        if beds == 0:
            return "⚪ データ不足", "許可病床数データがありません。"

        acute_r    = (koudo + kyusei) / beds
        recovery_r = kaifuku / beds
        chronic_r  = mansei / beds
        surg_cnt   = _surg_map_rv.get(hn, 0)

        # 急性期拠点候補: 地域上位かつスコア水準を満たす
        _top_n = max(1, min(3, max(1, n_total // 4) + 1))
        if rank <= _top_n and score >= 38:
            return (
                "🏆 急性期拠点候補",
                f"スコア {score}点（地域 {rank}位）。病床規模・手術実績・医師密度から地域の急性期医療を"
                f"集約的に担う中核病院としての素地がある。"
                f"{'ロボット支援手術や高度画像設備も備え、高度急性期機能の集約先として有力。' if _robot_map_rv.get(hn,0)>0 else ''}"
            )

        # 地域急性期: 急性期系比率高く中〜大規模
        if acute_r >= 0.50 and beds >= 150:
            return (
                "🔴 地域急性期",
                f"急性期系病床 {acute_r*100:.0f}%（{int(beds*acute_r):,}床）。"
                f"地域急性期機能を担いつつ、急性期拠点病院との役割分担・連携強化が重要。"
                f"{'手術実績 ' + str(surg_cnt) + '件/年。' if surg_cnt > 0 else ''}"
            )

        # 高齢者救急: 急性期と回復期を両方持ち高齢患者対応に適した構成
        if acute_r >= 0.25 and (recovery_r >= 0.15 or beds < 300):
            return (
                "🚑 高齢者救急",
                f"急性期 {acute_r*100:.0f}% / 回復期 {recovery_r*100:.0f}%。"
                f"高齢者の軽〜中等症救急入院受け入れと在宅・施設からの後方支援を担う機能が有効。"
                f"2040年にかけて高齢者救急需要の増大が見込まれる。"
            )

        # 回復期強化
        if recovery_r >= 0.40:
            return (
                "🔄 回復期強化",
                f"回復期病床 {recovery_r*100:.0f}%（{int(beds*recovery_r):,}床）。"
                f"2040年に向けて高齢者リハビリ需要が大幅増大するため、回復期・地域包括ケア病棟機能の拡充が期待される。"
            )

        # 慢性期・在宅支援
        if chronic_r >= 0.35:
            return (
                "💊 慢性期・在宅支援",
                f"慢性期病床 {chronic_r*100:.0f}%（{int(beds*chronic_r):,}床）。"
                f"高齢化に伴う療養需要に対応しつつ、在宅療養支援機能や看取り対応の強化も重要。"
            )

        # 小規模
        if beds < 100:
            return (
                "🏠 専門・外来特化",
                f"小規模（{beds}床）。外来・専門診療への特化や在宅支援機能の強化、"
                f"大病院との連携・後方ベッドとしての役割が有効。"
            )

        return (
            "⚪ 機能転換検討中",
            f"急性期 {acute_r*100:.0f}% / 回復期 {recovery_r*100:.0f}% / 慢性期 {chronic_r*100:.0f}%。"
            f"病床機能の選択と集中や地域での役割分担について、調整会議での議論が必要。"
        )

    # ── スコア計算
    _rv_score_details = {}
    rv_df["_score"] = 0
    for _idx, _rrow in rv_df.iterrows():
        _tot, _det = _kyoten_score_rv(_rrow)
        rv_df.at[_idx, "_score"] = _tot
        _rv_score_details[_rrow["医療機関名"]] = _det
    rv_df = rv_df.sort_values("_score", ascending=False).reset_index(drop=True)
    rv_df["_rank"] = range(1, len(rv_df) + 1)

    # 機能方向性
    _n_hosp_rv = len(rv_df)
    rv_df[["_role", "_comment"]] = rv_df.apply(
        lambda r: pd.Series(_classify_role_rv(r, int(r["_rank"]), int(r["_score"]), _n_hosp_rv)),
        axis=1,
    )

    # ════════════════════════════════════
    # Section 1 : 地域現状スナップショット
    # ════════════════════════════════════
    st.markdown('<div class="section-header">📊 地域現状スナップショット</div>', unsafe_allow_html=True)

    _rv_bt_cols = [f"{t}_許可病床数" for t in BED_TYPES if f"{t}_許可病床数" in rv_df.columns]

    def _rv_tot(col):
        return int(rv_df[col].fillna(0).sum()) if col in rv_df.columns else 0

    _rv_beds_koudo   = _rv_tot("高度急性期_許可病床数")
    _rv_beds_kyusei  = _rv_tot("急性期_許可病床数")
    _rv_beds_kaifuku = _rv_tot("回復期_許可病床数")
    _rv_beds_mansei  = _rv_tot("慢性期_許可病床数")
    _rv_beds_total   = _rv_tot("合計_許可病床数")
    _rv_surg_total   = sum(_surg_map_rv.values())
    _rv_acute_total  = _rv_beds_koudo + _rv_beds_kyusei
    _rv_care_total   = _rv_beds_kaifuku + _rv_beds_mansei

    # 常勤医師数: 報告している病院のみ集計し、100床あたりで密度を算出
    if "常勤医師数" in rv_df.columns:
        _rv_doc_series    = pd.to_numeric(rv_df["常勤医師数"], errors="coerce")
        _rv_docs_reported = int(_rv_doc_series.notna().sum())   # 報告病院数
        _rv_docs_total    = int(_rv_doc_series.fillna(0).sum()) # 地域合計（参考）
    else:
        _rv_docs_reported = 0
        _rv_docs_total    = 0
    # 100床あたり医師数（地域全体の医師密度）
    _rv_doc_per_100bed = round(_rv_docs_total / _rv_beds_total * 100, 1) if _rv_beds_total > 0 else 0

    _rvc1, _rvc2, _rvc3, _rvc4 = st.columns(4)
    _rvc1.metric(
        "地域内病院数",
        f"{_n_hosp_rv} 病院",
        f"許可病床計 {_rv_beds_total:,}床",
        help="選択中の二次医療圏・年度のデータ（病床機能報告）",
    )
    _rvc2.metric(
        "急性期系病床",
        f"{_rv_acute_total:,} 床",
        f"高度急性期 {_rv_beds_koudo:,} + 急性期 {_rv_beds_kyusei:,}",
        help="高度急性期_許可病床数 ＋ 急性期_許可病床数 の地域合計",
    )
    _rvc3.metric(
        "回復期・慢性期",
        f"{_rv_care_total:,} 床",
        f"回復期 {_rv_beds_kaifuku:,} + 慢性期 {_rv_beds_mansei:,}",
        help="回復期_許可病床数 ＋ 慢性期_許可病床数 の地域合計",
    )
    _rv_doc_sub = (
        f"地域計 {_rv_docs_total:,}人"
        + (f"（{_rv_docs_reported}/{_n_hosp_rv}病院が報告）" if _rv_docs_reported < _n_hosp_rv else f"（{_rv_docs_reported}病院）")
    )
    _rvc4.metric(
        "医師密度（100床あたり）",
        f"{_rv_doc_per_100bed:.1f} 人",
        _rv_doc_sub,
        help=(
            "計算式: 地域の常勤医師数合計 ÷ 地域の許可病床数合計 × 100\n"
            "データ: 様式1（施設票）常勤医師数\n"
            "※ 各病院が施設単位で報告した常勤医師数の合計を使用。"
            "未報告の病院は分子に含まれないため、報告率が低い医療圏では実態より低く算出される場合があります。"
        ),
    )

    st.markdown("<br>", unsafe_allow_html=True)

    # 病床種別構成 — 病院別横向き積み上げバー
    if _rv_bt_cols:
        _rv_bar_src = rv_df[["医療機関名"] + _rv_bt_cols].copy()
        for _c in _rv_bt_cols:
            _rv_bar_src[_c] = pd.to_numeric(_rv_bar_src[_c], errors="coerce").fillna(0)
        _rv_bar_src["_total"] = _rv_bar_src[_rv_bt_cols].sum(axis=1)
        _rv_bar_src = _rv_bar_src.sort_values("_total", ascending=True)

        _rv_bt_colors = {"高度急性期": "#e74c3c", "急性期": "#e67e22",
                         "回復期": "#3498db", "慢性期": "#27ae60"}
        fig_rv_stack = _go_rv.Figure()
        for _bt in BED_TYPES:
            _col = f"{_bt}_許可病床数"
            if _col in _rv_bar_src.columns:
                fig_rv_stack.add_trace(_go_rv.Bar(
                    name=_bt,
                    x=_rv_bar_src[_col],
                    y=_rv_bar_src["医療機関名"],
                    orientation="h",
                    marker_color=_rv_bt_colors.get(_bt, "#999"),
                    hovertemplate=f"%{{y}}<br>{_bt}: %{{x:,}}床<extra></extra>",
                ))
        fig_rv_stack.update_layout(
            barmode="stack",
            title=f"{_rv_region} 病院別 病床種別構成（{_rv_year}年度）",
            height=max(380, _n_hosp_rv * 32 + 100),
            margin=dict(l=10, r=10, t=55, b=10),
            font=dict(family="Meiryo, sans-serif"),
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            legend=dict(orientation="h", y=1.10),
            xaxis_title="病床数（床）",
        )
        st.plotly_chart(fig_rv_stack, use_container_width=True)

    # ════════════════════════════════════
    # Section 2 : 急性期拠点スコアリング
    # ════════════════════════════════════
    st.markdown('<div class="section-header">🏆 急性期拠点機能スコアリング</div>', unsafe_allow_html=True)

    st.caption(
        "**評価項目（100点満点）**　"
        "①病床規模(25点) ②高度急性期病床率(20点) ③手術実績(25点) ④医師密度(20点) ⑤高度設備(10点)　"
        "※手術データが未登録の病院は③が0点になります"
    )

    # スコアテーブル
    _rv_score_rows = []
    for _, _rr in rv_df.iterrows():
        _hn_s = _rr["医療機関名"]
        _det  = _rv_score_details.get(_hn_s, {})
        _rv_score_rows.append({
            "病院名":         _hn_s,
            "総スコア":       int(_rr["_score"]),
            "機能方向性":     _rr["_role"],
            "①病床規模":     _det.get("病床規模", 0),
            "②高度急性期率": _det.get("高度急性期率", 0),
            "③手術実績":     _det.get("手術実績", 0),
            "④医師密度":     _det.get("医師密度", 0),
            "⑤高度設備":     _det.get("高度設備", 0),
            "許可病床数":     _si(_rr.get("合計_許可病床数", 0)),
            "高度急性期床":   _si(_rr.get("高度急性期_許可病床数", 0)),
            "手術総数":       _surg_map_rv.get(_hn_s, 0),
            "常勤医師数":     _si(_rr.get("常勤医師数", 0)),
        })
    _rv_score_tbl = pd.DataFrame(_rv_score_rows)
    st.dataframe(
        _rv_score_tbl,
        hide_index=True,
        use_container_width=True,
        column_config={
            "総スコア":       st.column_config.ProgressColumn("総スコア", max_value=100, format="%d 点"),
            "①病床規模":     st.column_config.ProgressColumn("①病床規模(25)", max_value=25, format="%d"),
            "②高度急性期率": st.column_config.ProgressColumn("②高度急性期(20)", max_value=20, format="%d"),
            "③手術実績":     st.column_config.ProgressColumn("③手術(25)", max_value=25, format="%d"),
            "④医師密度":     st.column_config.ProgressColumn("④医師密度(20)", max_value=20, format="%d"),
            "⑤高度設備":     st.column_config.ProgressColumn("⑤設備(10)", max_value=10, format="%d"),
            "許可病床数":     st.column_config.NumberColumn("許可病床", format="%d 床"),
            "高度急性期床":   st.column_config.NumberColumn("高度急性期", format="%d 床"),
            "手術総数":       st.column_config.NumberColumn("手術", format="%d 件"),
            "常勤医師数":     st.column_config.NumberColumn("医師", format="%d 人"),
        },
    )

    # ポジショニングマップ（病床数 vs 手術件数 / 医師密度）
    _rv_scatter_df = rv_df.copy()
    _rv_scatter_df["手術総数"] = _rv_scatter_df["医療機関名"].map(_surg_map_rv).fillna(0)
    _rv_scatter_df["合計_許可病床数_n"] = pd.to_numeric(
        _rv_scatter_df.get("合計_許可病床数", pd.Series(0, index=_rv_scatter_df.index)),
        errors="coerce",
    ).fillna(0)

    _rv_role_colors = {
        "🏆 急性期拠点候補":   "#e74c3c",
        "🔴 地域急性期":       "#e67e22",
        "🚑 高齢者救急":       "#f39c12",
        "🔄 回復期強化":       "#3498db",
        "💊 慢性期・在宅支援": "#27ae60",
        "🏠 専門・外来特化":   "#9b59b6",
        "⚪ 機能転換検討中":   "#95a5a6",
        "⚪ データ不足":       "#bdc3c7",
    }

    fig_rv_scatter = _go_rv.Figure()
    for _rl in _rv_scatter_df["_role"].unique():
        _sub = _rv_scatter_df[_rv_scatter_df["_role"] == _rl]
        fig_rv_scatter.add_trace(_go_rv.Scatter(
            x=_sub["合計_許可病床数_n"],
            y=_sub["手術総数"],
            mode="markers+text",
            name=_rl,
            marker=dict(
                size=14,
                color=_rv_role_colors.get(_rl, "#999"),
                opacity=0.85,
                line=dict(width=1.5, color="white"),
            ),
            text=_sub["医療機関名"],
            textposition="top center",
            textfont=dict(size=9),
            hovertemplate=(
                "%{text}<br>病床数: %{x:,}床<br>手術数: %{y:,}件<extra></extra>"
            ),
        ))
    fig_rv_scatter.update_layout(
        title=f"{_rv_region} 病院ポジショニングマップ（病床数 vs 年間手術件数）",
        xaxis_title="許可病床数（床）",
        yaxis_title="年間手術総数（件）",
        height=520,
        margin=dict(l=10, r=10, t=60, b=10),
        font=dict(family="Meiryo, sans-serif"),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        legend=dict(orientation="v", x=1.01, font=dict(size=11)),
    )
    st.plotly_chart(fig_rv_scatter, use_container_width=True)
    if _rv_surg_total == 0:
        st.caption("⚠️ 手術データが未登録のため Y軸（手術件数）はすべて 0 になっています")

    # ════════════════════════════════════
    # Section 3 : 各病院の機能方向性
    # ════════════════════════════════════
    st.markdown('<div class="section-header">🔭 各病院の機能方向性（参考）</div>', unsafe_allow_html=True)
    st.caption("スコアと病床構成から算出した、2040年地域医療構想に向けた各病院の機能方向性の参考分類です。")

    # ── 機能方向性の定義表
    with st.expander("📖 機能方向性の定義・判定基準", expanded=True):
        st.markdown("""
<style>
.role-def-table { width:100%; border-collapse:collapse; font-size:0.8rem; }
.role-def-table th {
    background:#f0f2f6; padding:7px 10px; text-align:left;
    border-bottom:2px solid #d0d3db; font-size:0.78rem; color:#444;
}
.role-def-table td { padding:7px 10px; border-bottom:1px solid #e8e8e8; vertical-align:top; }
.role-def-table tr:last-child td { border-bottom:none; }
.role-badge {
    display:inline-block; padding:2px 8px; border-radius:10px;
    font-weight:600; font-size:0.78rem; white-space:nowrap;
}
</style>
<table class="role-def-table">
<thead>
<tr>
  <th style="width:14%">分類</th>
  <th style="width:24%">定義</th>
  <th style="width:32%">このツールの判定基準</th>
  <th style="width:30%">2040年に向けた主な方向性</th>
</tr>
</thead>
<tbody>
<tr>
  <td><span class="role-badge" style="background:#fde8e8;color:#c0392b;">🏆 急性期拠点候補</span></td>
  <td>地域の高度・急性期医療を集約的に担う<b>中核病院の候補</b>。新地域医療構想の「急性期拠点病院」に相当。</td>
  <td>①スコアが地域内上位（病院数÷4程度の枠）、かつ②合計スコア<b>38点以上</b></td>
  <td>急性期・高度急性期機能を集約。地域内で1〜3病院程度に絞り込まれる想定。救命救急・専門医療の維持が使命。</td>
</tr>
<tr>
  <td><span class="role-badge" style="background:#fdf0e6;color:#c0392b;">🔴 地域急性期</span></td>
  <td>急性期医療を提供できる規模を持つが、<b>拠点集約の対象外</b>となる急性期病院。</td>
  <td>急性期系病床（高度急性期＋急性期）の<b>比率50%以上</b>かつ<b>150床以上</b></td>
  <td>急性期拠点病院と連携・機能分担しながら、地域の入院急性期需要を補完。選択と集中が今後の課題。</td>
</tr>
<tr>
  <td><span class="role-badge" style="background:#fef9e7;color:#d35400;">🚑 高齢者救急</span></td>
  <td>高齢者の<b>軽〜中等症救急</b>を受け入れ、在宅・介護施設への早期復帰を支援する病院。</td>
  <td>急性期系比率<b>25%以上</b>かつ（回復期比率<b>15%以上</b>または<b>300床未満</b>）</td>
  <td>2040年に向けて最も需要増が見込まれる機能。高齢者の生活機能維持・在宅復帰支援を軸に整備。</td>
</tr>
<tr>
  <td><span class="role-badge" style="background:#eaf4fb;color:#1a6fa8;">🔄 回復期強化</span></td>
  <td>リハビリテーション・<b>回復期機能を主軸</b>とする病院。地域包括ケア病棟を含む。</td>
  <td>回復期病床比率<b>40%以上</b></td>
  <td>術後・脳卒中・骨折後のリハビリ需要は2040年に向けて大幅増。地域包括ケア病棟の充実と急性期後連携が鍵。</td>
</tr>
<tr>
  <td><span class="role-badge" style="background:#e8f8f0;color:#1a7a4a;">💊 慢性期・在宅支援</span></td>
  <td>長期療養・慢性期入院や<b>在宅療養支援</b>を主体とする病院・診療所。</td>
  <td>慢性期病床比率<b>35%以上</b></td>
  <td>高齢化に伴う療養・看取り需要への対応。在宅療養支援病院機能との連携強化や、訪問診療・看取り体制の整備。</td>
</tr>
<tr>
  <td><span class="role-badge" style="background:#f4ecf7;color:#7d3c98;">🏠 専門・外来特化</span></td>
  <td>小規模で<b>専門診療・外来</b>、または在宅支援に特化した医療機関。</td>
  <td>許可病床数<b>100床未満</b></td>
  <td>入院機能を縮小・特化し、外来・専門診療への集中または地域包括ケアの担い手として大病院との後方連携強化。</td>
</tr>
<tr>
  <td><span class="role-badge" style="background:#f2f3f4;color:#666;">⚪ 機能転換検討中</span></td>
  <td>上記いずれの特徴も明確でなく、<b>機能の方向性の選択が課題</b>となっている病院。</td>
  <td>上記6分類のいずれの条件も非該当</td>
  <td>急性期から回復期・在宅支援への段階的転換、または地域での明確な役割分担について調整会議での検討が必要。</td>
</tr>
</tbody>
</table>
<p style="font-size:0.72rem;color:#999;margin-top:8px;">
※ 判定基準は病床機能報告の報告値のみを用いた参考分類です。実際の機能定義は都道府県の地域医療構想に基づきます。
</p>
        """, unsafe_allow_html=True)

    for _, _rr in rv_df.iterrows():
        _hn_r   = _rr["医療機関名"]
        _role_r = _rr["_role"]
        _comm_r = _rr["_comment"]
        _scr_r  = int(_rr["_score"])
        _beds_r = _si(_rr.get("合計_許可病床数", 0))
        _surg_r = _surg_map_rv.get(_hn_r, 0)
        _bc_r   = _rv_role_colors.get(_role_r, "#bdc3c7")

        _surg_txt = f" | 手術 {_surg_r:,}件/年" if _surg_r > 0 else ""
        _docs_r   = _si(_rr.get("常勤医師数", 0))
        _docs_txt = f" | 医師 {_docs_r}人" if _docs_r > 0 else ""

        st.markdown(f"""
        <div style="border-left:4px solid {_bc_r}; padding:10px 14px; margin:8px 0;
                    background:#f8f9fa; border-radius:0 8px 8px 0;">
          <div style="font-weight:600; font-size:0.95rem;">{_hn_r}</div>
          <div style="font-size:0.82rem; color:#444; margin:4px 0;">
            {_role_r} &nbsp;
            <span style="color:#999;">
              スコア {_scr_r}点 | {_beds_r:,}床{_surg_txt}{_docs_txt}
            </span>
          </div>
          <div style="font-size:0.8rem; color:#555; line-height:1.6;">{_comm_r}</div>
        </div>
        """, unsafe_allow_html=True)

    # ════════════════════════════════════
    # Section 4 : 2040年病床需要の方向性（試算）
    # ════════════════════════════════════
    st.markdown('<div class="section-header">📅 2040年に向けた病床需要の方向性（試算）</div>', unsafe_allow_html=True)

    st.info(
        "⚠️ **試算の前提**: 国の地域医療構想（2024年）の方向性と全国トレンドをもとに、"
        "**全国平均的な係数**を機械的に乗じた参考値です。"
        "実際の数値は都道府県が公表する**地域医療構想調整会議の推計**を参照してください。"
    )

    _rv_proj_cfg = {
        "高度急性期": (
            _rv_beds_koudo,
            int(_rv_beds_koudo * 0.95),
            "集約化・効率化により微減（▲5%）",
            "#e74c3c",
        ),
        "急性期": (
            _rv_beds_kyusei,
            int(_rv_beds_kyusei * 0.80),
            "在院日数短縮・集約化で大幅減（▲20%）",
            "#e67e22",
        ),
        "回復期": (
            _rv_beds_kaifuku,
            int(_rv_beds_kaifuku * 1.35),
            "高齢者需要大幅増（＋35%）",
            "#3498db",
        ),
        "慢性期": (
            _rv_beds_mansei,
            int(_rv_beds_mansei * 1.15),
            "高齢者需要増（＋15%）",
            "#27ae60",
        ),
    }

    _rv_proj_rows = []
    for _bt, (_cur, _fut, _note, _clr) in _rv_proj_cfg.items():
        if _cur > 0 or _fut > 0:
            _ch  = _fut - _cur
            _pct = (_fut / _cur - 1) * 100 if _cur > 0 else 0
            _rv_proj_rows.append({
                "病床種別": _bt,
                "現状（床）": _cur,
                "2040年試算（床）": _fut,
                "増減（床）": _ch,
                "増減率": f"{_pct:+.0f}%",
                "背景・根拠": _note,
            })

    if _rv_proj_rows:
        _rv_proj_tbl = pd.DataFrame(_rv_proj_rows)
        st.dataframe(
            _rv_proj_tbl,
            hide_index=True,
            use_container_width=True,
            column_config={
                "現状（床）":      st.column_config.NumberColumn(format="%d 床"),
                "2040年試算（床）": st.column_config.NumberColumn(format="%d 床"),
                "増減（床）":      st.column_config.NumberColumn(format="%+d 床"),
            },
        )

        # 現状 vs 2040年試算 比較バー
        fig_rv_proj = _go_rv.Figure()
        for _row_p in _rv_proj_rows:
            _clr_p = _rv_proj_cfg[_row_p["病床種別"]][3]
            fig_rv_proj.add_trace(_go_rv.Bar(
                name=f"{_row_p['病床種別']}（現状）",
                x=["現状"],
                y=[_row_p["現状（床）"]],
                marker_color=_clr_p,
                opacity=0.55,
            ))
            fig_rv_proj.add_trace(_go_rv.Bar(
                name=f"{_row_p['病床種別']}（2040試算）",
                x=["2040年試算"],
                y=[_row_p["2040年試算（床）"]],
                marker_color=_clr_p,
                opacity=1.0,
            ))
        fig_rv_proj.update_layout(
            barmode="stack",
            title=f"{_rv_region} 病床需要の現状 vs 2040年試算",
            height=420,
            margin=dict(l=10, r=10, t=55, b=10),
            font=dict(family="Meiryo, sans-serif"),
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            yaxis_title="病床数（床）",
            legend=dict(orientation="v", x=1.01, font=dict(size=10)),
        )
        st.plotly_chart(fig_rv_proj, use_container_width=True)

    # 注意書き
    st.markdown("""
    <div style="font-size:0.78rem; color:#888; margin-top:20px; padding:12px 14px;
                background:#f0f0f0; border-radius:6px; line-height:1.7;">
    📌 <b>本分析の注意点</b><br>
    ・スコアリングは病床機能報告データのみを用いた参考値です。救急搬送実績・地域連携体制・財務状況等は含まれていません。<br>
    ・「急性期拠点候補」はスコアと地域内順位から算出したものであり、行政・調整会議の公式認定ではありません。<br>
    ・2040年試算は全国トレンド係数を一律適用したものです。地域の年齢構成・人口動態・患者流出入は反映されていません。<br>
    ・実際の地域医療構想の策定・協議は、都道府県・医療機関・地域住民が参加する調整会議で行われます。
    </div>
    """, unsafe_allow_html=True)

    # 地域構想モードはここで終了
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
    components.html(
        """
        <style>
        button {
            background: #f0f2f6;
            border: 1px solid #d0d3db;
            border-radius: 6px;
            padding: 6px 14px;
            font-size: 0.84rem;
            color: #444;
            cursor: pointer;
            float: right;
            margin-top: 14px;
            font-family: sans-serif;
        }
        button:hover { background: #e0e3ea; }
        </style>
        <button onclick="window.parent.print()">🖨️ 印刷</button>
        """,
        height=52,
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

tab1, tab7, tab2, tab3, tab4, tab5, tab6 = st.tabs([
    "📊 病院概要",
    "🗺️ 地図",
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

            def _pct(n, tot):
                return f"{n / tot * 100:.1f}%" if tot > 0 else "—"

            def _si0(series, col):
                return int(series[col].sum()) if col in series.columns else 0

            c1, c2 = st.columns(2)
            with c1:
                st.plotly_chart(admission_route_pie(hosp_ward, hospital), use_container_width=True)
                # ── 入院経路 件数表 ──
                _adm_total  = _si0(hosp_ward, "新規入棟患者数")
                _adm_kyukyu = _si0(hosp_ward, "救急入院患者数")
                _adm_other  = max(_adm_total - _adm_kyukyu, 0)
                _adm_tbl = pd.DataFrame([
                    {"入院経路": "予定外救急入院",   "件数（人）": _adm_kyukyu, "割合": _pct(_adm_kyukyu, _adm_total)},
                    {"入院経路": "予定・院内転棟等", "件数（人）": _adm_other,  "割合": _pct(_adm_other,  _adm_total)},
                    {"入院経路": "合計",             "件数（人）": _adm_total,  "割合": "100.0%" if _adm_total > 0 else "—"},
                ])
                st.dataframe(
                    _adm_tbl,
                    hide_index=True,
                    use_container_width=True,
                    column_config={"件数（人）": st.column_config.NumberColumn(format="%d 人")},
                )

            with c2:
                st.plotly_chart(discharge_route_pie(hosp_ward, hospital), use_container_width=True)
                # ── 退院経路 件数表 ──
                _dis_total    = _si0(hosp_ward, "退棟患者数")
                _dis_katei    = _si0(hosp_ward, "家庭退院数")
                _dis_tain     = _si0(hosp_ward, "他院転院数")
                _dis_shisetsu = _si0(hosp_ward, "施設入所数")
                _dis_shibo    = _si0(hosp_ward, "死亡退院数")
                _dis_other    = max(_dis_total - _dis_katei - _dis_tain - _dis_shisetsu - _dis_shibo, 0)
                _dis_tbl = pd.DataFrame([
                    {"退院経路": "家庭退院", "件数（人）": _dis_katei,    "割合": _pct(_dis_katei,    _dis_total)},
                    {"退院経路": "他院転院", "件数（人）": _dis_tain,     "割合": _pct(_dis_tain,     _dis_total)},
                    {"退院経路": "施設入所", "件数（人）": _dis_shisetsu, "割合": _pct(_dis_shisetsu, _dis_total)},
                    {"退院経路": "死亡退院", "件数（人）": _dis_shibo,    "割合": _pct(_dis_shibo,    _dis_total)},
                    {"退院経路": "その他",   "件数（人）": _dis_other,    "割合": _pct(_dis_other,    _dis_total)},
                    {"退院経路": "合計",     "件数（人）": _dis_total,    "割合": "100.0%" if _dis_total > 0 else "—"},
                ])
                st.dataframe(
                    _dis_tbl,
                    hide_index=True,
                    use_container_width=True,
                    column_config={"件数（人）": st.column_config.NumberColumn(format="%d 人")},
                )

            st.markdown('<div class="section-header">在宅復帰率</div>', unsafe_allow_html=True)

            # ── 数値取得 ──
            total_taitou = float(hosp_ward["退棟患者数"].sum())
            total_katei  = float(hosp_ward["家庭退院数"].sum())
            total_shibo  = float(hosp_ward["死亡退院数"].sum()) if "死亡退院数" in hosp_ward.columns else 0.0

            # 正しい計算式: 家庭退院数 ÷ (退棟患者数 − 死亡退院数) × 100
            _denom = max(total_taitou - total_shibo, 0)
            home_rate = total_katei / _denom if _denom > 0 else 0

            hr1, hr2, hr3, hr4 = st.columns(4)
            hr1.metric("退棟患者数（年間）", f"{int(total_taitou):,}人",
                help="様式1（病棟票）\nデータ列: 退棟患者数\n分母の基数となる全退棟患者数")
            hr2.metric("死亡退院数（年間）", f"{int(total_shibo):,}人",
                help="様式1（病棟票）\nデータ列: 死亡退院数\n分母から除外される死亡退院患者数")
            hr3.metric("家庭退院数（年間）", f"{int(total_katei):,}人",
                help="様式1（病棟票）\nデータ列: 家庭退院数\n分子となる自宅退院患者数")
            hr4.metric("在宅復帰率", f"{home_rate * 100:.1f}%",
                help="計算式: 在宅復帰率 = 家庭退院数 ÷（退棟患者数 − 死亡退院数）× 100\n"
                     "データ: 様式1（病棟票）退棟先区分\n"
                     "使用列: 家庭退院数（分子）／ 退棟患者数・死亡退院数（分母）\n"
                     "※ 正式定義では分子は「自宅・居住系介護施設等への退院数」です")

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
        # 年度フィルター（surgery_dfに報告年度列がある場合は絞り込む）
        if "報告年度" in surgery_df.columns:
            hosp_surg = surgery_df[
                (surgery_df["医療機関名"] == hospital) &
                (surgery_df["報告年度"] == year)
            ]
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
            # 年度フィルター（複数年度データが混在するとバーが重複するため）
            if "二次医療圏名" in surgery_df.columns:
                _rsurg_mask = surgery_df["二次医療圏名"] == region
                if "報告年度" in surgery_df.columns:
                    _rsurg_mask = _rsurg_mask & (surgery_df["報告年度"] == year)
                region_surg = surgery_df[_rsurg_mask]
            else:
                region_surg = pd.DataFrame()

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


# ── TAB 7: 地図 ─────────────────────────────────────────────

with tab7:
    st.markdown("### 🗺️ 病院マップ")
    st.caption("病院名・都道府県名からジオコーディング（OpenStreetMap）して地図に表示します。初回のみ時間がかかります。")

    try:
        import folium
        from streamlit_folium import st_folium as _st_folium
        from geocoder import geocode_batch, load_cached_coords, count_uncached, has_official_locations
        _MAP_OK = True
    except ImportError as _e:
        _MAP_OK = False
        st.error(f"地図表示に必要なライブラリが見つかりません: {_e}")

    if _MAP_OK:
        if not DB_PATH.exists():
            st.warning("地図機能はDuckDBデータ使用時のみ利用できます。")
        else:
            # 公式座標（医療情報ネット）の有無を表示
            if has_official_locations(str(DB_PATH)):
                st.success("✅ 厚労省 医療情報ネットの公式座標データ読み込み済み")
            else:
                st.info(
                    "💡 **公式座標データを取り込むと精度が大幅に向上します**\n\n"
                    "ローカルで以下を実行してください:\n"
                    "```\npython build_master.py\n```\n"
                    "（厚労省 医療情報ネット オープンデータを自動ダウンロードします）"
                )
            # ── 表示範囲の選択 ──
            map_scope = st.radio(
                "表示範囲",
                ["選択中の都道府県", "選択中の二次医療圏"],
                horizontal=True,
                key="map_scope",
            )

            if map_scope == "選択中の都道府県":
                map_df = df[(df["都道府県名"] == pref) & (df["報告年度"] == year)].copy()
                map_title = f"{pref}（{year}年度）"
            else:
                map_df = df[
                    (df["都道府県名"] == pref)
                    & (df["二次医療圏名"] == region)
                    & (df["報告年度"] == year)
                ].copy()
                map_title = f"{pref} {region}（{year}年度）"

            n_total = len(map_df)
            n_uncached = count_uncached(str(DB_PATH), map_df["医療機関名"].tolist(), pref)
            n_cached = n_total - n_uncached

            st.markdown(f"**対象: {map_title} — {n_total:,}病院**")

            mi1, mi2 = st.columns(2)
            mi1.metric("キャッシュ済み", f"{n_cached:,}件")
            mi2.metric("未ジオコーディング", f"{n_uncached:,}件")

            if n_uncached > 0:
                est_min = n_uncached * 1.2 / 60
                st.warning(
                    f"⏱️ {n_uncached:,}件のジオコーディングが必要です（推定 {est_min:.1f}〜{est_min*1.5:.1f}分）。\n\n"
                    "結果はDBにキャッシュされるため、次回以降は即座に表示されます。"
                )
                if st.button("📍 ジオコーディング実行", type="primary", key="run_geocoding"):
                    _prog_bar = st.progress(0)
                    _prog_txt = st.empty()

                    def _prog_cb(done, total):
                        _prog_bar.progress(done / total)
                        _prog_txt.text(f"処理中... {done}/{total}")

                    geocode_batch(map_df, str(DB_PATH), progress_cb=_prog_cb)
                    _prog_txt.text("✅ 完了!")
                    st.rerun()

            # ── キャッシュ済み座標を読み込んで地図を描画 ──
            geo_dict = load_cached_coords(str(DB_PATH), pref)
            map_df["lat"] = map_df["医療機関名"].map(lambda n: geo_dict.get(n, (None, None))[0])
            map_df["lon"] = map_df["医療機関名"].map(lambda n: geo_dict.get(n, (None, None))[1])
            map_valid = map_df.dropna(subset=["lat", "lon"])

            if map_valid.empty:
                st.info("表示できる病院がまだありません。「ジオコーディング実行」で座標を取得してください。")
            else:
                st.success(f"✅ {len(map_valid):,}病院を地図に表示")

                center_lat = float(map_valid["lat"].mean())
                center_lon = float(map_valid["lon"].mean())
                zoom = 11 if map_scope == "選択中の二次医療圏" else 9

                _m = folium.Map(
                    location=[center_lat, center_lon],
                    zoom_start=zoom,
                    tiles="CartoDB positron",
                )

                _max_beds = max(int(map_valid["合計_許可病床数"].max() or 1), 1)

                for _, _r in map_valid.iterrows():
                    _beds = int(_r.get("合計_許可病床数", 0) or 0)
                    _kado = int(_r.get("合計_稼働病床数", 0) or 0)
                    _occ  = f"{_kado / _beds * 100:.1f}%" if _beds > 0 else "—"
                    _radius = max(5, min(22, _beds / _max_beds * 22))

                    if _beds >= 500:
                        _color = "#e74c3c"
                    elif _beds >= 300:
                        _color = "#e67e22"
                    elif _beds >= 100:
                        _color = "#2ecc71"
                    else:
                        _color = "#3498db"

                    _is_sel = _r["医療機関名"] == hospital
                    _popup_html = (
                        f'<div style="font-family:Meiryo,sans-serif;min-width:190px">'
                        f'<b style="font-size:13px">{_r["医療機関名"]}</b><br>'
                        f'<span style="color:#666;font-size:11px">{_r["都道府県名"]} {_r["二次医療圏名"]}</span>'
                        f'<hr style="margin:5px 0">'
                        f'許可病床数: <b>{_beds:,}床</b><br>'
                        f'稼働率: <b>{_occ}</b>'
                        f'</div>'
                    )

                    folium.CircleMarker(
                        location=[float(_r["lat"]), float(_r["lon"])],
                        radius=_radius,
                        color="#c0392b" if _is_sel else "#555",
                        weight=3 if _is_sel else 1,
                        fill=True,
                        fill_color=_color,
                        fill_opacity=0.75,
                        popup=folium.Popup(_popup_html, max_width=260),
                        tooltip=f"{_r['医療機関名']}（{_beds:,}床）",
                    ).add_to(_m)

                _legend = """
                <div style="position:fixed;bottom:30px;right:10px;background:white;
                            padding:10px 14px;border-radius:8px;
                            box-shadow:2px 2px 8px rgba(0,0,0,0.25);
                            font-size:12px;font-family:Meiryo,sans-serif;z-index:9999">
                  <b>許可病床数</b><br>
                  <span style="color:#e74c3c;font-size:16px">●</span> 500床以上<br>
                  <span style="color:#e67e22;font-size:16px">●</span> 300〜499床<br>
                  <span style="color:#2ecc71;font-size:16px">●</span> 100〜299床<br>
                  <span style="color:#3498db;font-size:16px">●</span> 100床未満
                </div>
                """
                _m.get_root().html.add_child(folium.Element(_legend))

                _st_folium(_m, width="100%", height=600, returned_objects=[])
