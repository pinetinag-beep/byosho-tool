"""
病床機能報告 分析・比較ツール
"""
import os
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
    region_share, hospital_trend, hospital_los_trend, bed_composition,
    load_hospitals_from_db, load_wards_from_db, load_surgery_from_db, get_db_meta,
    BED_TYPES, BED_COLORS, PREF_CODE_MAP,
)

# 都道府県コード順（北から南）のソートキー
_PREF_ORDER = {name: code for code, name in PREF_CODE_MAP.items()}

def _sort_prefs(pref_list):
    """都道府県名リストを都道府県コード順に並べる"""
    return sorted(pref_list, key=lambda p: _PREF_ORDER.get(p, "99"))


def _reiwa_nendo(seireki_year: int) -> str:
    """西暦の報告年度 → 「令和X年度」（令和1=2019）。"""
    try:
        y = int(seireki_year)
    except (ValueError, TypeError):
        return ""
    r = y - 2018
    return f"令和{r}年度" if r >= 1 else f"平成{y - 1988}年度"


def _byosho_source(year: int) -> str:
    """病床機能報告データの出典ラベル（例：令和5年度病床機能報告）。"""
    return f"{_reiwa_nendo(year)}病床機能報告"


def _dpc_source(year: int) -> str:
    """DPC調査データの出典ラベル（例：令和6年度 DPC導入の影響評価調査）。"""
    return f"{_reiwa_nendo(year)} DPC導入の影響評価に係る調査"


def _source_tag(text: str) -> str:
    """リスト右上などに置く控えめな出典ラベル（右寄せHTML）。"""
    return (
        f"<div style='text-align:right;font-size:0.72rem;color:#6E6A5E;"
        f"margin:-6px 0 2px;'>データ出典：{text}</div>"
    )

def _normalize_name(name: str) -> str:
    """病院名の表記揺れを正規化（全角→半角、スペース除去、小文字化）"""
    if not isinstance(name, str):
        return ""
    name = unicodedata.normalize("NFKC", name)
    name = re.sub(r'[\s　・]', '', name)
    name = name.lower()
    return name

_LEGAL_PREFIXES_SK = [
    "独立行政法人国立病院機構", "国家公務員共済組合連合会", "地方独立行政法人",
    "社会医療法人財団", "社会医療法人", "国立大学法人", "公立大学法人",
    "医療法人社団", "医療法人財団", "公益財団法人", "一般財団法人",
    "公益社団法人", "一般社団法人", "社会福祉法人", "特定医療法人", "医療法人",
    "学校法人", "宗教法人",
]

def _normalize_hospital_for_match(name: str) -> str:
    """施設基準届出情報とのマッチング用：法人格プレフィックスを除去して正規化"""
    if not isinstance(name, str):
        return ""
    name = unicodedata.normalize("NFKC", name).strip()
    for prefix in _LEGAL_PREFIXES_SK:
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    return re.sub(r'[\s　]', '', name).lower()

from charts import (
    bed_donut, occupancy_gauge, bed_type_occupancy_bar,
    regional_bed_comparison, occupancy_scatter, share_bar, ranking_table_fig,
    trend_beds, trend_occupancy, trend_staff, trend_los, trend_dpc_cases,
    staff_scatter, staff_bar_region,
    detail_bed_type_table, admission_route_pie, discharge_route_pie, home_return_rate_bar,
)
from sample_data import generate_sample_data

@st.cache_data(ttl=3600, show_spinner=False)
def _cached_geocode_address(text: str):
    """住所テキストを緯度経度に変換（1時間キャッシュ）"""
    try:
        from geocoder import geocode_address
        return geocode_address(text)
    except Exception:
        return None

# ── ページ設定 ─────────────────────────────────────────────

st.set_page_config(
    page_title="病床機能報告 分析ツール",
    page_icon="🏥",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── ブラウザ自動翻訳の抑止 ──────────────────────────────────
# Chrome等の自動翻訳が日本語ページを誤って再翻訳し、「地域包括ケア」が
# 「地域含むケア」のように書き換わってしまう事象への対策。
# st.markdown内の<script>はinnerHTML経由のため実行されないので、
# components.html（同一オリジンiframe）経由でwindow.parentのDOMを操作する。
components.html("""
<script>
if (!window.parent.document.querySelector('meta[name="google"]')) {
    const meta = window.parent.document.createElement('meta');
    meta.name = 'google';
    meta.content = 'notranslate';
    window.parent.document.head.appendChild(meta);
}
window.parent.document.documentElement.lang = "ja";
window.parent.document.documentElement.classList.add("notranslate");
</script>
""", height=0)

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


def _render_header():
    """パンくずナビゲーションヘッダー（ステップ表示なし）"""
    mode = st.session_state.get("_view_mode", "home")
    _hospital = st.session_state.get("_sel_hospital", "")
    _pref     = st.session_state.get("_sel_pref", "")
    _region   = st.session_state.get("_sel_region", "")

    if mode == "home":
        st.markdown(
            "<div style='padding:10px 0 2px;'>"
            "<span style='font-size:1.05rem;font-weight:800;color:#111827;'>"
            "🏥 ホーム</span></div>",
            unsafe_allow_html=True,
        )
    else:
        _hc1, _hc2 = st.columns([1.2, 8.8])
        with _hc1:
            if st.button("← ホーム", key="_hdr_home_btn"):
                st.session_state["_view_mode"] = "home"
                st.session_state["_hospital_chosen"] = False
                st.rerun()
        with _hc2:
            _sep = "<span style='color:#d1d5db;margin:0 5px;'>›</span>"
            _parts = ["<span style='color:#9ca3af;'>🏥 ホーム</span>"]
            if mode == "detail":
                if _pref:     _parts.append(f"<span style='color:#6b7280;'>{_pref}</span>")
                if _region:   _parts.append(f"<span style='color:#6b7280;'>{_region}</span>")
                if _hospital: _parts.append(f"<strong style='color:#111827;'>{_hospital}</strong>")
            elif mode == "region":
                _rg_pref   = st.session_state.get("_rg_pref", "")
                _rg_region = st.session_state.get("_rg_region", "")
                if _rg_pref:   _parts.append(f"<span style='color:#6b7280;'>{_rg_pref}</span>")
                if _rg_region: _parts.append(f"<span style='color:#6b7280;'>{_rg_region}</span>")
                _parts.append("<span style='color:#111827;font-weight:600;'>地域から選ぶ</span>")
            elif mode == "map":
                _ms_pref = st.session_state.get("_ms_pref", "")
                if _ms_pref: _parts.append(f"<span style='color:#6b7280;'>{_ms_pref}</span>")
                _parts.append("<span style='color:#111827;font-weight:600;'>地図で探す</span>")
            elif mode == "distance":
                _parts.append("<span style='color:#111827;font-weight:600;'>距離・所要時間で探す</span>")
            elif mode == "search":
                _parts.append("<span style='color:#111827;font-weight:600;'>設備・手術条件で探す</span>")
            elif mode == "region_vision":
                _rv_p = st.session_state.get("_rv_sel_pref", "")
                _rv_r = st.session_state.get("_rv_sel_region", "")
                if _rv_p: _parts.append(f"<span style='color:#6b7280;'>{_rv_p}</span>")
                if _rv_r: _parts.append(f"<span style='color:#6b7280;'>{_rv_r}</span>")
                _parts.append("<span style='color:#111827;font-weight:600;'>地域医療構想分析</span>")
            st.markdown(
                f"<div style='padding:7px 0 0;font-size:0.82rem;'>{_sep.join(_parts)}</div>",
                unsafe_allow_html=True,
            )

    st.markdown(
        "<hr style='margin:4px 0 14px;border:none;border-top:1px solid #f3f4f6;'>",
        unsafe_allow_html=True,
    )


def _render_footer():
    """全ページ共通のフッター"""
    st.markdown("<br><br>", unsafe_allow_html=True)
    st.markdown("---")

    _fa, _fb = st.columns(2)
    with _fa:
        with st.expander("⚠️ 免責事項"):
            st.markdown("""
<div style="font-size:0.85rem; color:#555; line-height:1.6;">

本ツールは、厚生労働省が公表する**病床機能報告**のデータをもとに集計・分析を行うものです。

**ご利用にあたっての注意事項：**

- 原データ（報告値）に誤りや未報告が含まれる場合があり、分析結果が実態と異なることがあります。
- 本ツールの分析結果は参考情報であり、医療機関の評価・優劣を示すものではありません。
- 経営判断・医療政策の立案などに利用する場合は、必ず一次データや専門家の助言を合わせてご確認ください。
- 本ツールの利用によって生じたいかなる損害についても、作成者は責任を負いません。
- データは報告年度時点のものであり、現在の状況と異なる場合があります。

</div>
""", unsafe_allow_html=True)

    with _fb:
        with st.expander("🔧 管理者"):
            st.caption("データの再読み込み / キャッシュ管理")
            if st.button("キャッシュをクリアして再読み込み", use_container_width=True, key="_ftr_cache_clear"):
                st.cache_data.clear()
                for key in ["df", "ward_df", "surgery_df", "_yoshiki2_parquet"]:
                    st.session_state.pop(key, None)
                st.rerun()
            st.divider()
            st.caption("🔬 手術データの取り込み（様式2）")
            _y2_year_sel = st.selectbox("取り込む年度", [2023, 2022, 2021], key="_ftr_yoshiki2_year")
            _y2_hint = "2021年は7地域ファイルを全て選択" if _y2_year_sel == 2021 else "全国1ファイルを選択"
            _y2_files = st.file_uploader(
                f"Excelファイルを選択（{_y2_hint}）",
                type=["xlsx", "xls"],
                accept_multiple_files=True,
                key="_ftr_yoshiki2_upload",
            )
            if _y2_files and st.button(f"手術データを取り込む（{len(_y2_files)}ファイル）", use_container_width=True, key="_ftr_yoshiki2_import"):
                _prog = st.progress(0, text="処理開始...")
                _status = st.empty()
                try:
                    _parts = []
                    for _i, _f in enumerate(_y2_files):
                        _prog.progress((_i) / len(_y2_files), text=f"読み込み中: {_f.name}")
                        _fb_bytes = _f.read()
                        _part = load_mhlw_yoshiki2(_fb_bytes, year=_y2_year_sel)
                        _status.caption(f"✔ {_f.name}: {len(_part):,} 病院")
                        _parts.append(_part)
                    _prog.progress(1.0, text="集計中...")
                    if not _parts:
                        _prog.empty()
                        st.error("データが空です。")
                    else:
                        _surg_new = pd.concat(_parts, ignore_index=True).drop_duplicates(subset=["医療機関名", "都道府県名"])
                        _existing = st.session_state.get("surgery_df")
                        if _existing is not None and not _existing.empty:
                            _existing = _existing[_existing["報告年度"] != _y2_year_sel]
                            _merged = pd.concat([_existing, _surg_new], ignore_index=True)
                        else:
                            _merged = _surg_new
                        st.session_state["surgery_df"] = _merged
                        import io as _io2
                        _pbuf = _io2.BytesIO()
                        _merged.to_parquet(_pbuf, index=False)
                        st.session_state["_yoshiki2_parquet"] = _pbuf.getvalue()
                        _prog.empty()
                        _status.empty()
                        st.success(f"✅ {_y2_year_sel}年: {len(_surg_new):,} 病院取り込み完了")
                except Exception as _e:
                    st.error(f"エラー: {_e}")
            if st.session_state.get("_yoshiki2_parquet"):
                st.download_button(
                    "📥 surgery_cache.parquet をダウンロード",
                    data=st.session_state["_yoshiki2_parquet"],
                    file_name="surgery_cache.parquet",
                    mime="application/octet-stream",
                    use_container_width=True,
                    type="primary",
                    key="_ftr_dl_parquet",
                )
            st.divider()
            st.caption("📊 DPC・病床機能・施設基準届出 統合表")
            _df_for_export = st.session_state.get("df")
            if _df_for_export is not None and not _df_for_export.empty:
                if st.button("統合表を生成する", use_container_width=True, key="_ftr_gen_integrated"):
                    with st.spinner("生成中..."):
                        try:
                            _xl_bytes = _build_integrated_excel(_df_for_export)
                            st.session_state["_integrated_excel"] = _xl_bytes
                        except Exception as _e:
                            st.error(f"エラー: {_e}")
                if st.session_state.get("_integrated_excel"):
                    _yr = int(_df_for_export["報告年度"].max())
                    st.download_button(
                        "📥 統合表 Excel をダウンロード",
                        data=st.session_state["_integrated_excel"],
                        file_name=f"医療機関統合表_{_yr}年度.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        use_container_width=True,
                        type="primary",
                        key="_ftr_dl_integrated",
                    )
            else:
                st.caption("データ読み込み後に使用できます")

    st.markdown(
        "<div style='text-align:center;font-size:0.7rem;color:#c0c4cc;padding:16px 0;'>"
        "© 病床機能報告 分析ツール — データ出典: 厚生労働省「病床機能報告」</div>",
        unsafe_allow_html=True,
    )


st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Zen+Kaku+Gothic+New:wght@400;500;700&family=Zen+Maru+Gothic:wght@500;700;900&display=swap');

/* ══════════════════════════════════════════════════════
   デザイントークン（シビック・フレンドリー）
   - 温かみのある紙色の背景 × 深緑のブランドカラー1色
   - 見出しは丸ゴシック（Zen Maru Gothic）で親しみを、
     本文・数値は角ゴシック（Zen Kaku Gothic New）で明瞭さを出す
   - 色は「ブランド緑＝操作・強調」「意味色（緑/橙/赤）＝状態」のみに限定
══════════════════════════════════════════════════════ */
:root {
    --ink:        #26251F;   /* 本文 */
    --ink-muted:  #6E6A5E;   /* 補足テキスト */
    --paper:      #FAF9F6;   /* ページ背景（温かみのある紙色） */
    --card:       #FFFFFF;   /* カード背景 */
    --line:       #E8E4DB;   /* 罫線（ウォームグレー） */
    --brand:      #12886D;   /* ブランド（深緑） */
    --brand-deep: #0B6653;
    --brand-tint: #EAF4F0;   /* ブランドの淡背景 */
    --brand-line: #BFDFD4;
    --ok:     #1A7F4B;
    --warn:   #A8630A;
    --danger: #B3362B;
    --shadow-card: 0 1px 2px rgba(70,60,35,.05), 0 6px 20px rgba(70,60,35,.06);
    --radius-card: 14px;
}

/* ── 全体フォント（Material Icons を上書きしないよう text要素のみ対象）── */
body, .main .block-container,
p, li, label, input, select, textarea, caption,
[data-testid="stMarkdownContainer"],
[data-testid="stCaptionContainer"],
[data-testid="stText"],
.stSelectbox label, .stTextInput label, .stNumberInput label,
.stRadio label, .stCheckbox label,
div[data-testid="stSidebar"] label,
div[data-testid="stSidebar"] p,
div[data-testid="stSidebar"] span:not([class*="material"]) {
    font-family: 'Zen Kaku Gothic New', 'Hiragino Kaku Gothic ProN', 'メイリオ', sans-serif !important;
    color: var(--ink);
}

/* ── 見出しは丸ゴシックで親しみを出す ── */
h1, h2, h3, h4, h5, h6,
.section-header, .landing-group-title,
.method-card .mc-title {
    font-family: 'Zen Maru Gothic', 'Hiragino Maru Gothic ProN', 'Zen Kaku Gothic New', sans-serif !important;
    color: var(--ink);
}

/* ── ヘッダーバー・背景を紙色に統一 ── */
.stApp { background: var(--paper); }
header[data-testid="stHeader"] { background: var(--paper) !important; }

/* ── KPI数値は等幅フィーチャーを有効化 ── */
.metric-value {
    font-variant-numeric: tabular-nums;
    letter-spacing: -0.02em;
}

/* ── KPIカード（色付きボーダーを廃止し、静かな白カードに統一）
     ※各カードにはインラインの border-top-color 指定が残っているため
       !important で無効化する（色は意味を運ぶ時だけ使う方針） ── */
.metric-card {
    background: var(--card);
    border: 1px solid var(--line) !important;
    border-radius: var(--radius-card);
    padding: 18px 12px 14px;
    text-align: center;
    box-shadow: var(--shadow-card);
}
.metric-label {
    font-size: 0.78rem; color: var(--ink-muted); margin-bottom: 6px;
    letter-spacing: 0.05em; font-weight: 700;
}
.metric-value { font-size: 2.0rem; font-weight: 700; color: var(--ink); line-height: 1.1; }
.metric-sub   { font-size: 0.82rem; color: var(--ink-muted); margin-top: 5px; }

/* ── セクション見出し（青下線 → ブランド緑の左バー） ── */
.section-header {
    font-size: 1.05rem; font-weight: 700; color: var(--ink);
    border-left: 4px solid var(--brand);
    border-bottom: none;
    border-radius: 2px;
    padding: 2px 0 2px 10px;
    margin: 30px 0 14px;
    line-height: 1.35;
}

/* ── 印刷ボタン（画面表示用） ── */
.print-btn {
    display: inline-block;
    padding: 6px 16px;
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 10px;
    font-size: 0.85rem;
    color: var(--ink-muted);
    cursor: pointer;
    text-decoration: none;
}
.print-btn:hover { background: var(--brand-tint); border-color: var(--brand-line); }

/* ── 検索メソッドカード ── */
.method-card {
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 16px;
    padding: 22px 18px 14px;
    box-shadow: var(--shadow-card);
    min-height: 158px;
    transition: box-shadow .15s, transform .15s, border-color .15s;
    margin-bottom: 4px;
}
.method-card:hover {
    box-shadow: 0 4px 18px rgba(70,60,35,.12);
    border-color: var(--brand-line);
    transform: translateY(-1px);
}
.method-card .mc-icon { line-height: 1; margin-bottom: 12px; }
.method-card .mc-icon svg { stroke: var(--brand); width: 30px; height: 30px; }
.method-card .mc-title { font-size: 0.98rem; font-weight: 700; margin-bottom: 7px; }
.method-card .mc-desc  { font-size: 0.8rem; color: var(--ink-muted); line-height: 1.7; }

/* ── カード全体をクリック可能にする（stretched-link パターン） ── */
.method-card--link {
    position: relative;
    display: flex;
    flex-direction: column;
    cursor: pointer;
}
.method-card--link .mc-stretch {
    position: absolute;
    inset: 0;
    z-index: 1;
    border-radius: inherit;
    text-decoration: none;
}
.method-card--link .mc-cta {
    margin-top: auto;
    padding-top: 12px;
    font-size: 0.84rem;
    font-weight: 700;
    color: var(--brand);
    letter-spacing: 0.02em;
    transition: transform .15s;
}
.method-card--link:hover .mc-cta { transform: translateX(3px); }
.method-card--link:hover .mc-icon svg { stroke: var(--brand-deep); }

/* ── ランディングのグループ見出し（絵文字 → ブランド緑のドット） ── */
.landing-group-title { font-size: 1.08rem; font-weight: 700; color: var(--ink); margin: 0 0 4px; }
.landing-group-title::before {
    content: ""; display: inline-block; width: 9px; height: 9px;
    border-radius: 3px; background: var(--brand);
    margin-right: 9px; vertical-align: 1px;
}
.landing-group-desc  { font-size: 0.83rem; color: var(--ink-muted); margin: 0 0 14px 18px; }

/* ── 絞り込みボックス（操作パネル＝ブランド緑の淡背景・全検索ページ共通） ── */
.st-key-ns_filter_box,
.st-key-rg_filter_box,
.st-key-ms_filter_box,
.st-key-dist_filter_box,
.st-key-rv_filter_box,
.st-key-cs_filter_box {
    border-color: var(--brand-line) !important;
    background: var(--brand-tint) !important;
    border-radius: var(--radius-card) !important;
}

/* ── ボタン・入力の角丸を統一 ── */
.stButton button, .stDownloadButton button { border-radius: 10px !important; }
div[data-baseweb="select"] > div, .stTextInput input, .stNumberInput input {
    border-radius: 10px !important;
}
div[data-testid="stExpander"] { border-radius: 12px !important; }

/* ── 検索バー（ホーム画面） ── */
.home-search-wrap input {
    font-size: 1.05rem !important;
    padding: 14px 18px !important;
    border-radius: 10px !important;
}

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
    .metric-card  { padding: 10px 6px !important; border-top-width: 2px !important; }
    .metric-value { font-size: 1.4rem !important; }
    .metric-label { font-size: 0.72rem !important; }
    .metric-sub   { font-size: 0.72rem !important; }

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
        font-size: 0.79rem !important;
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

/* ── サイドバー開閉ボタン ─────────────────────────────────
   「«」→「✕ 閉じる」、「»」→「☰ メニュー」に置き換え   */

/* サイドバー内の閉じるボタン « */
[data-testid="stSidebarHeader"] button svg,
[data-testid="stSidebarCollapseButton"] svg {
    display: none;
}
[data-testid="stSidebarHeader"] button::after,
[data-testid="stSidebarCollapseButton"] button::after {
    content: "✕ 閉じる";
    font-size: 0.82rem;
    font-weight: 700;
    color: #64748b;
    letter-spacing: 0.03em;
    white-space: nowrap;
}
[data-testid="stSidebarHeader"] button,
[data-testid="stSidebarCollapseButton"] button {
    width: auto !important;
    padding: 4px 8px !important;
    background: #f1f5f9 !important;
    border: 1px solid #e2e8f0 !important;
    border-radius: 6px !important;
}

/* メインエリア側の開くボタン » */
[data-testid="collapsedControl"] svg,
[data-testid="stSidebarCollapsedControl"] svg {
    display: none;
}
[data-testid="collapsedControl"]::after,
[data-testid="stSidebarCollapsedControl"]::after {
    content: "☰ メニュー";
    font-size: 0.82rem;
    font-weight: 700;
    color: #64748b;
    letter-spacing: 0.03em;
    white-space: nowrap;
}
[data-testid="collapsedControl"],
[data-testid="stSidebarCollapsedControl"] {
    width: auto !important;
    padding: 6px 10px !important;
    background: #f1f5f9 !important;
    border: 1px solid #e2e8f0 !important;
    border-radius: 0 6px 6px 0 !important;
}

@media (max-width: 768px) {
}

/* ── データフレーム（テーブル）フォント拡大 ── */
[data-testid="stDataFrame"] [role="gridcell"],
[data-testid="stDataFrame"] [role="columnheader"],
[data-testid="stDataFrame"] .dvn-scroller {
    font-size: 13.5px !important;
}

/* ── Streamlit デフォルトテキスト底上げ ── */
[data-testid="stMarkdownContainer"] p,
[data-testid="stMarkdownContainer"] li {
    font-size: 0.95rem;
    line-height: 1.75;
}
[data-testid="stCaptionContainer"] p {
    font-size: 0.82rem !important;
}
</style>
""", unsafe_allow_html=True)


# ── DuckDB パス ────────────────────────────────────────────

DB_PATH      = Path(__file__).parent / "data" / "byosho.duckdb"
_LOCS_PARQUET = Path(__file__).parent / "locations_cache.parquet"

CACHE_FILE         = Path(__file__).parent / "data_cache.parquet"
CACHE_FILE_WARD    = Path(__file__).parent / "ward_cache.parquet"
CACHE_FILE_SURGERY = Path(__file__).parent / "surgery_cache.parquet"

_DPC_DIR = Path(__file__).parent
DPC_PARQUET_MATCH    = _DPC_DIR / "dpc_match.parquet"
DPC_PARQUET_HOSP     = _DPC_DIR / "dpc_hospitals.parquet"
DPC_PARQUET_PROC     = _DPC_DIR / "dpc_procedure_stats.parquet"
DPC_PARQUET_MDC_RATIO= _DPC_DIR / "dpc_mdc_ratio.parquet"
DPC_PARQUET_MDC_CASES= _DPC_DIR / "dpc_mdc_cases.parquet"
DPC_PARQUET_READM    = _DPC_DIR / "dpc_readmission.parquet"
DPC_PARQUET_SURG     = _DPC_DIR / "dpc_surgery_detail.parquet"

MDC_LABELS = {
    "MDC01":"神経系", "MDC02":"眼科系", "MDC03":"耳鼻咽喉科系",
    "MDC04":"呼吸器系", "MDC05":"循環器系", "MDC06":"消化器系",
    "MDC07":"筋骨格系", "MDC08":"皮膚・皮下組織", "MDC09":"乳房",
    "MDC10":"内分泌・代謝", "MDC11":"腎・尿路系", "MDC12":"女性生殖器系",
    "MDC13":"血液・造血器", "MDC14":"新生児・先天性", "MDC15":"小児疾患",
    "MDC16":"外傷・熱傷・中毒", "MDC17":"精神疾患", "MDC18":"その他",
}


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


_DPC_MATCH_MTIME: float = DPC_PARQUET_MATCH.stat().st_mtime if DPC_PARQUET_MATCH.exists() else 0.0

@st.cache_data(show_spinner=False)
def _load_dpc_match(_mtime: float = 0.0):
    return pd.read_parquet(DPC_PARQUET_MATCH) if DPC_PARQUET_MATCH.exists() else None

@st.cache_data(show_spinner=False)
def _load_dpc_hospitals():
    return pd.read_parquet(DPC_PARQUET_HOSP) if DPC_PARQUET_HOSP.exists() else None

@st.cache_data(show_spinner=False)
def _load_dpc_procedure_stats():
    return pd.read_parquet(DPC_PARQUET_PROC) if DPC_PARQUET_PROC.exists() else None

@st.cache_data(show_spinner=False)
def _load_dpc_mdc_ratio():
    return pd.read_parquet(DPC_PARQUET_MDC_RATIO) if DPC_PARQUET_MDC_RATIO.exists() else None

@st.cache_data(show_spinner=False)
def _load_dpc_mdc_cases():
    return pd.read_parquet(DPC_PARQUET_MDC_CASES) if DPC_PARQUET_MDC_CASES.exists() else None

@st.cache_data(show_spinner=False)
def _load_dpc_readmission():
    return pd.read_parquet(DPC_PARQUET_READM) if DPC_PARQUET_READM.exists() else None

_DPC_SURG_MTIME: float = DPC_PARQUET_SURG.stat().st_mtime if DPC_PARQUET_SURG.exists() else 0.0

@st.cache_data(show_spinner=False)
def _load_dpc_surgery_detail(_mtime: float = 0.0):
    if not DPC_PARQUET_SURG.exists():
        return None
    df = pd.read_parquet(DPC_PARQUET_SURG)
    # 疾患名まで含めた完全重複のみ除去（同一dpc6でも疾患名が異なるレコードは別行）
    return df.drop_duplicates(subset=["年度", "告示番号", "MDC", "dpc6", "疾患名"], keep="first")

SHISETSU_KIJUN_PARQUET = Path(__file__).parent / "shisetsu_kijun_cache.parquet"

@st.cache_data(show_spinner=False)
def _load_shisetsu_kijun():
    if not SHISETSU_KIJUN_PARQUET.exists():
        return None
    df = pd.read_parquet(SHISETSU_KIJUN_PARQUET)
    # 同一病院×同一届出が複数月分存在するため、検索用途に必要な一意組み合わせに圧縮。
    # 受理番号も含めるのは、同一届出名称でも病棟ごとに区分（急性期一般入院料１〜６等）
    # が異なる別registrationのケースがあるため（受理番号がその識別子になる）。
    _dedup_cols = ["都道府県コード", "医療機関番号", "受理届出名称"]
    if "受理番号" in df.columns:
        _dedup_cols.append("受理番号")
    df = df.drop_duplicates(subset=_dedup_cols)
    for col in ["都道府県コード", "都道府県名", "受理届出名称"]:
        if col in df.columns:
            df[col] = df[col].astype("category")
    return df


def _build_integrated_excel(df_all: pd.DataFrame) -> bytes:
    """病床機能報告 × DPC × 施設基準届出 の統合表を Excel バイト列で返す。"""
    import io as _io

    # ── 最新年度の病床機能報告 ──
    latest_year = int(df_all["報告年度"].max())
    base = df_all[df_all["報告年度"] == latest_year].copy()

    base_cols = [
        "医療機関コード", "医療機関名", "都道府県名", "二次医療圏名",
        "合計_許可病床数", "合計_稼働病床数",
        "高度急性期_許可病床数", "急性期_許可病床数",
        "回復期_許可病床数", "慢性期_許可病床数",
        "常勤医師数", "非常勤医師数",
        "常勤看護師数", "非常勤看護師数",
        "常勤理学療法士数", "常勤作業療法士数", "常勤言語聴覚士数",
        "救急搬送件数",
        "CT台数", "MRI台数", "内視鏡手術支援機器台数",
        "PET台数", "PETCT台数", "ガンマナイフ台数",
    ]
    base = base[[c for c in base_cols if c in base.columns]].copy()
    base.insert(0, "報告年度", latest_year)

    # ── DPC 結合 ──
    dpc_match = _load_dpc_match(_DPC_MATCH_MTIME)
    dpc_hosp  = _load_dpc_hospitals()
    if dpc_match is not None and dpc_hosp is not None:
        _dpc_latest = dpc_hosp[dpc_hosp["年度"] == dpc_hosp["年度"].max()].copy()
        _dpc_merged = dpc_match.merge(
            _dpc_latest[["施設名", "病院類型", "DPC算定病床数"]],
            left_on="DPC施設名", right_on="施設名", how="left"
        )
        base = base.merge(
            _dpc_merged[["病床報告施設名", "病院類型", "DPC算定病床数"]],
            left_on="医療機関名", right_on="病床報告施設名", how="left"
        ).drop(columns=["病床報告施設名"], errors="ignore")
        base.insert(
            base.columns.get_loc("合計_許可病床数"),
            "DPC対象",
            base["DPC算定病床数"].notna().map({True: "○", False: "×"}),
        )

    # ── 施設基準届出 ピボット ──
    _KIJUN_ITEMS = [
        ("一般病棟入院基本料",              "一般病棟入院基本料"),
        ("特定機能病院",                    "特定機能病院入院基本料"),
        ("地域包括ケア病棟",                "地域包括ケア病棟入院料"),
        ("地域包括医療病棟",                "地域包括医療病棟入院料"),
        ("回復期リハビリ病棟",              "回復期リハビリテーション病棟入院料"),
        ("緩和ケア病棟",                    "緩和ケア病棟入院料"),
        ("精神病棟",                        "精神病棟入院基本料"),
        ("救急医療管理加算",                "救急医療管理加算"),
        ("超急性期脳卒中加算(tPA)",         "超急性期脳卒中加算"),
        ("ICU",                             "集中治療室管理料"),
        ("HCU",                             "ハイケアユニット入院医療管理料"),
        ("脳血管疾患等リハビリ(Ⅰ)",        "脳血管疾患等リハビリテーション料（Ⅰ）"),
        ("運動器リハビリ(Ⅰ)",              "運動器リハビリテーション料（Ⅰ）"),
        ("呼吸器リハビリ(Ⅰ)",              "呼吸器リハビリテーション料（Ⅰ）"),
        ("がん患者指導管理料",              "がん患者指導管理料"),
        ("外来化学療法加算",                "外来化学療法加算"),
        ("ロボット手術",                    "ロボット支援下内視鏡手術用支援機器加算"),
        ("在宅療養後方支援病院",            "在宅療養後方支援病院"),
        ("データ提出加算",                  "データ提出加算"),
        ("薬剤管理指導料",                  "薬剤管理指導料"),
    ]

    sk_df = _load_shisetsu_kijun()
    if sk_df is not None:
        _pref_to_code = {v: k for k, v in PREF_CODE_MAP.items()}
        for col_name, kw in _KIJUN_ITEMS:
            _sub = sk_df[sk_df["受理届出名称"].str.contains(kw, na=False, regex=False)]
            _sub_set: dict[str, set[str]] = {}
            for _, r in _sub[["都道府県コード", "医療機関名_正規化"]].drop_duplicates().iterrows():
                _sub_set.setdefault(r["都道府県コード"], set()).add(r["医療機関名_正規化"])

            def _has_kijun(row, _ss=_sub_set):
                _c = _pref_to_code.get(row["都道府県名"], "")
                _n = _normalize_hospital_for_match(row["医療機関名"])
                if not _n or _c not in _ss:
                    return "×"
                _names = _ss[_c]
                return "○" if (_n in _names or any(sn.endswith(_n) for sn in _names)) else "×"

            base[col_name] = base.apply(_has_kijun, axis=1)

    buf = _io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        base.to_excel(writer, sheet_name=f"統合表_{latest_year}年度", index=False)
        ws = writer.sheets[f"統合表_{latest_year}年度"]
        for col_cells in ws.columns:
            max_len = max(len(str(c.value or "")) for c in col_cells)
            ws.column_dimensions[col_cells[0].column_letter].width = min(max_len + 2, 40)
    return buf.getvalue()

_PREF_CODE_TO_NAME = {
    "01":"北海道","02":"青森県","03":"岩手県","04":"宮城県","05":"秋田県",
    "06":"山形県","07":"福島県","08":"茨城県","09":"栃木県","10":"群馬県",
    "11":"埼玉県","12":"千葉県","13":"東京都","14":"神奈川県","15":"新潟県",
    "16":"富山県","17":"石川県","18":"福井県","19":"山梨県","20":"長野県",
    "21":"岐阜県","22":"静岡県","23":"愛知県","24":"三重県","25":"滋賀県",
    "26":"京都府","27":"大阪府","28":"兵庫県","29":"奈良県","30":"和歌山県",
    "31":"鳥取県","32":"島根県","33":"岡山県","34":"広島県","35":"山口県",
    "36":"徳島県","37":"香川県","38":"愛媛県","39":"高知県","40":"福岡県",
    "41":"佐賀県","42":"長崎県","43":"熊本県","44":"大分県","45":"宮崎県",
    "46":"鹿児島県","47":"沖縄県",
}

@st.cache_data(show_spinner=False)
def _build_dpc_hosp_info() -> pd.DataFrame:
    """告示番号 → 施設名・病院区分・都道府県名・二次医療圏名 のマッピングテーブルを構築。
    市町村番号（JIS都道府県コード）から都道府県名を導出するため geo_map 誤突合の影響を受けない。
    二次医療圏名は DPC調査データ自体には無いため、dpc_match（DPC施設名⇔病床報告施設名の
    対応表）経由で病床機能報告データと突合して取得する。"""
    if not DPC_PARQUET_HOSP.exists():
        return pd.DataFrame()
    hosp_df  = pd.read_parquet(DPC_PARQUET_HOSP)
    hosp_uniq = hosp_df.sort_values("年度", ascending=False).drop_duplicates("告示番号").copy()

    def _pref(code):
        try:
            return _PREF_CODE_TO_NAME.get(str(int(float(code))).zfill(5)[:2], "")
        except Exception:
            return ""

    def _classify(t):
        t = str(t)
        if "DPC参加" in t: return "DPC算定病院"
        if "準備"   in t: return "DPC準備病院"
        if "出来高" in t: return "出来高算定病院"
        return "その他"

    hosp_uniq["都道府県名"] = hosp_uniq["市町村番号"].apply(_pref)
    hosp_uniq["病院区分"]   = hosp_uniq["病院類型"].apply(_classify)

    dpc_match = _load_dpc_match(_DPC_MATCH_MTIME)
    if dpc_match is not None and CACHE_FILE.exists():
        _matched = dpc_match[~dpc_match["マッチ状態"].astype(str).str.contains("未結合", na=False)]
        _byosho = pd.read_parquet(CACHE_FILE, columns=["医療機関名", "二次医療圏名", "報告年度"])
        _byosho_latest = _byosho.sort_values("報告年度", ascending=False).drop_duplicates("医療機関名")
        _region_map = _matched.merge(
            _byosho_latest[["医療機関名", "二次医療圏名"]],
            left_on="病床報告施設名", right_on="医療機関名", how="left",
        )[["DPC施設名", "二次医療圏名"]]
        hosp_uniq = hosp_uniq.merge(_region_map, left_on="施設名", right_on="DPC施設名", how="left")
    if "二次医療圏名" not in hosp_uniq.columns:
        hosp_uniq["二次医療圏名"] = ""
    hosp_uniq["二次医療圏名"] = hosp_uniq["二次医療圏名"].fillna("")

    return hosp_uniq[["告示番号", "病院区分", "都道府県名", "二次医療圏名"]].reset_index(drop=True)


@st.cache_data(ttl=3600 * 24 * 7, show_spinner=False)
def _gen_rv_ai_comments(records_json: str, api_key: str) -> dict:
    """機能方向性の短評をClaude APIで並列生成（全セッション共有・7日キャッシュ）"""
    import json
    import anthropic
    from concurrent.futures import ThreadPoolExecutor, as_completed
    records = json.loads(records_json)
    client  = anthropic.Anthropic(api_key=api_key)
    out: dict = {}
    def _call(rec):
        try:
            r = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=150,
                messages=[{"role": "user", "content": rec["prompt"]}],
            )
            return rec["hn"], r.content[0].text.strip()
        except Exception:
            return rec["hn"], None
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(_call, r): r["hn"] for r in records}
        for fut in as_completed(futures):
            hn, txt = fut.result()
            out[hn] = txt
    return out


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
# 表示モード: "home" / "detail" / "search" / "map" / "distance" / "region_vision"
if "_view_mode" not in st.session_state:
    st.session_state["_view_mode"] = "home"
# ユーザーが病院を明示的に選択したかどうか
if "_hospital_chosen" not in st.session_state:
    st.session_state["_hospital_chosen"] = False


# ── NaN → int ヘルパー ─────────────────────────────────────
def _si(val):
    """NaN / None / 文字列を安全に int に変換"""
    try:
        return int(val or 0)
    except (ValueError, TypeError):
        return 0


# ── メインエリア ───────────────────────────────────────────


# ── データなし ───────────────────────────────────────────────

if st.session_state.df is None:
    _render_header()
    st.markdown("## 全国の病院の情報を調べる")
    _d1, _d2 = st.columns(2)
    with _d1:
        st.info("""
**このツールでできること**
- 選択した病院の病床種別・稼働率を可視化
- 同二次医療圏内でのベンチマーク比較
- 地域内順位・シェアの把握
- 経年変化トレンドの確認
- 医療スタッフ配置の地域比較
        """)
    with _d2:
        st.markdown("#### データを読み込む")
        _load_tab_s, _load_tab_m = st.tabs(["🎮 サンプルデータ", "📁 Excelアップロード"])
        with _load_tab_s:
            st.caption("デモ用のサンプルデータ（4年分）を生成します")
            if st.button("サンプルデータを使う", type="primary", use_container_width=True, key="_load_sample"):
                with st.spinner("生成中..."):
                    df_loaded = generate_sample_data()
                    st.session_state.df = df_loaded
                    st.session_state.ward_df = None
                    st.session_state.surgery_df = None
                    st.session_state._datasrc = "sample"
                st.rerun()
        with _load_tab_m:
            st.caption("様式1 Excelをアップロード")
            report_year_inp = st.number_input("報告年度", value=2023, min_value=2010, max_value=2030, step=1, key="_load_year")
            uploaded_files_inp = st.file_uploader(
                "Excelファイル（複数可）",
                type=["xlsx", "xls"],
                accept_multiple_files=True,
                key="_load_files",
            )
            if uploaded_files_inp and st.button("読み込む", type="primary", use_container_width=True, key="_load_btn"):
                with st.spinner(f"{len(uploaded_files_inp)}ファイルを処理中..."):
                    try:
                        fb = [(f.name, f.read()) for f in uploaded_files_inp]
                        df_loaded, ward_loaded = load_multiple_mhlw_extended(fb, year=int(report_year_inp))
                        st.session_state.df = df_loaded
                        st.session_state.ward_df = ward_loaded
                        st.success(f"{len(df_loaded):,}病院を読み込みました")
                        st.rerun()
                    except Exception as e:
                        st.error(f"読み込みエラー: {e}")
    _render_footer()
    st.stop()


# ── _nav_jump 処理（サイドバー廃止に伴い、メインエリアで処理）────
_df_all = st.session_state.df

# ?hospital=NAME クエリパラメータ経由のナビゲーション（地図ポップアップリンク）
_qp_hosp = st.query_params.get("hospital")
if _qp_hosp:
    import urllib.parse as _uparse
    _qp_hosp = _uparse.unquote(_qp_hosp)
    _qp_rows = _df_all[_df_all["医療機関名"] == _qp_hosp].sort_values("報告年度", ascending=False)
    if not _qp_rows.empty:
        _qpr = _qp_rows.iloc[0]
        st.session_state["_nav_jump"] = {
            "year":     int(_qpr["報告年度"]),
            "pref":     str(_qpr["都道府県名"]),
            "region":   str(_qpr["二次医療圏名"]),
            "hospital": _qp_hosp,
        }
    st.query_params.clear()
    st.rerun()

# ?go=MODE クエリパラメータ経由の画面遷移（ホーム画面のカードリンク）
_qp_go = st.query_params.get("go")
if _qp_go:
    _GO_MODES = {
        "name_search", "region", "map", "distance", "search",
        "dpc_search", "clinic_search", "region_vision",
    }
    if _qp_go in _GO_MODES:
        st.session_state["_view_mode"] = _qp_go
        st.session_state["_scroll_to_top"] = True
    st.query_params.clear()
    st.rerun()

_nav = st.session_state.pop("_nav_jump", None)
if _nav:
    st.session_state["_sel_year"]        = int(_nav["year"])
    st.session_state["_sel_pref"]        = str(_nav["pref"])
    st.session_state["_sel_region"]      = str(_nav["region"])
    st.session_state["_sel_hospital"]    = str(_nav["hospital"])
    st.session_state["_nav_done"]        = str(_nav["hospital"])
    st.session_state["_scroll_to_top"]   = True
    st.session_state["_hospital_chosen"] = True
    st.session_state["_view_mode"]       = "detail"
    st.rerun()

# ── sel_* 変数の計算 ──────────────────────────────────────────
_df_years    = [int(y) for y in sorted(_df_all["報告年度"].dropna().unique(), reverse=True)]
sel_year     = int(st.session_state.get("_sel_year",     _df_years[0] if _df_years else 2023))
sel_pref     = str(st.session_state.get("_sel_pref",     _sort_prefs(_df_all["都道府県名"].unique())[0]))
_r_list      = sorted(
    r for r in _df_all[_df_all["都道府県名"] == sel_pref]["二次医療圏名"].unique()
    if r != "不明"
)
sel_region   = str(st.session_state.get("_sel_region",   _r_list[0]  if _r_list  else ""))
_h_list      = _df_all[
    (_df_all["報告年度"] == sel_year) &
    (_df_all["都道府県名"] == sel_pref) &
    (_df_all["二次医療圏名"] == sel_region)
]["医療機関名"].sort_values().tolist()
sel_hospital = str(st.session_state.get("_sel_hospital", _h_list[0]  if _h_list  else ""))

# ── ヘッダー（全ページ共通）────────────────────────────────
_render_header()

# ══════════════════════════════════════════════════════════
# ホーム（ランディング画面）
# ══════════════════════════════════════════════════════════

if st.session_state.get("_view_mode") == "home":
    # ── ヒーロー ─────────────────────────────────────────────
    st.markdown(
        f"""
<div style="text-align:center;padding:46px 0 34px;">
  <p style="font-size:0.8rem;font-weight:700;color:#0B6653;letter-spacing:0.18em;margin:0 0 10px;">
    地域の医療をひらく、公的データのまど
  </p>
  <h1 style="font-size:2.3rem;font-weight:900;color:#26251F;margin:0 0 14px;line-height:1.25;
             letter-spacing:0.01em;">
    全国の病院の情報を調べる
  </h1>
  <p style="font-size:0.98rem;font-weight:500;color:#6E6A5E;margin:0;">どうやって探しますか？</p>
</div>""",
        unsafe_allow_html=True,
    )

    # ── 検索メソッドカード（「何がしたいか」で3グループに分類）──
    # go を渡すとカード全体がクリック可能になる。Streamlitのmarkdownは
    # <a>（インライン）の中にブロック<div>を入れると構造が壊れるため、
    # カードは<div>のままにして、透明な<a>を absolute で全面に重ねる
    # （stretched-link パターン）。
    def _method_card(icon, title, desc, go=None):
        _link = (
            f"<a class='mc-stretch' href='?go={go}' target='_self' "
            f"aria-label='{title}'></a>" if go else ""
        )
        _cta = "<div class='mc-cta'>ひらく →</div>" if go else ""
        _cls = "method-card method-card--link" if go else "method-card"
        return (
            f"<div class='{_cls}'>"
            f"{_link}"
            f"<div class='mc-icon'>{icon}</div>"
            f"<div class='mc-title'>{title}</div>"
            f"<div class='mc-desc'>{desc}</div>"
            f"{_cta}"
            f"</div>"
        )

    # 線画SVGアイコン（feather/lucide系・24x24 stroke）。絵文字は環境依存で
    # 見た目が変わり品位も落ちるため、カードのアイコンはSVGに統一する。
    def _svg(paths: str) -> str:
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
            'fill="none" stroke-width="2" stroke-linecap="round" '
            f'stroke-linejoin="round">{paths}</svg>'
        )

    _ICON_SEARCH  = _svg('<circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>')
    _ICON_REGION  = _svg('<path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><rect x="8" y="2" width="8" height="4" rx="1" ry="1"/><line x1="8" y1="11" x2="16" y2="11"/><line x1="8" y1="16" x2="13" y2="16"/>')
    _ICON_MAP     = _svg('<polygon points="1 6 8 2 16 6 23 2 23 18 16 22 8 18 1 22"/><line x1="8" y1="2" x2="8" y2="18"/><line x1="16" y1="6" x2="16" y2="22"/>')
    _ICON_CLOCK   = _svg('<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>')
    _ICON_SLIDERS = _svg('<line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/>')
    _ICON_PULSE   = _svg('<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>')
    _ICON_STETHO  = _svg('<path d="M4.8 2.3A.3.3 0 1 0 5 2H4a2 2 0 0 0-2 2v5a6 6 0 0 0 6 6v0a6 6 0 0 0 6-6V4a2 2 0 0 0-2-2h-1a.2.2 0 1 0 .3.3"/><path d="M8 15v1a6 6 0 0 0 6 6v0a6 6 0 0 0 6-6v-4"/><circle cx="20" cy="10" r="2"/>')
    _ICON_CHART   = _svg('<line x1="12" y1="20" x2="12" y2="10"/><line x1="18" y1="20" x2="18" y2="4"/><line x1="6" y1="20" x2="6" y2="16"/>')

    def _landing_group_header(title, desc):
        st.markdown(
            f"<div class='landing-group-title'>{title}</div>"
            f"<div class='landing-group-desc'>{desc}</div>",
            unsafe_allow_html=True,
        )

    # グループ1: 特定の病院を調べる ──────────────────────────
    _landing_group_header("特定の病院を調べる", "病院名・地域・地図から、個別の病院ページを開きます")
    _mc1, _mc2, _mc3 = st.columns(3, gap="medium")
    with _mc1:
        st.markdown(_method_card(_ICON_SEARCH, "病院名で探す",
            "病院名の一部を入力して<br>候補をリストアップします",
            go="name_search"), unsafe_allow_html=True)
    with _mc2:
        st.markdown(_method_card(_ICON_REGION, "地域から選ぶ",
            "都道府県・二次医療圏・病院名を<br>選択して詳細を確認します",
            go="region"), unsafe_allow_html=True)
    with _mc3:
        st.markdown(_method_card(_ICON_MAP, "地図で探す",
            "都道府県・二次医療圏を選択し<br>病院の分布を地図で確認します",
            go="map"), unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

    # グループ2: 条件で絞り込む ──────────────────────────────
    _landing_group_header("条件で絞り込む", "距離・設備・疾患などの条件で、全国の病院を横断的に絞り込みます")
    _mc4, _mc5, _mc6 = st.columns(3, gap="medium")
    with _mc4:
        st.markdown(_method_card(_ICON_CLOCK, "距離・所要時間で探す",
            "住所やランドマークから<br>N分以内の病院を一覧表示します",
            go="distance"), unsafe_allow_html=True)
    with _mc5:
        st.markdown(_method_card(_ICON_SLIDERS, "設備・手術条件で探す",
            "CT/MRI台数・手術件数・<br>スタッフ数などで全国を絞り込み",
            go="search"), unsafe_allow_html=True)
    with _mc6:
        if DPC_PARQUET_SURG.exists():
            st.markdown(_method_card(_ICON_PULSE, "DPC疾患別 病院検索",
                "手術件数・在院日数を疾患ごとに<br>全国・都道府県・二次医療圏で比較",
                go="dpc_search"), unsafe_allow_html=True)

    if SHISETSU_KIJUN_PARQUET.exists():
        st.markdown("<br>", unsafe_allow_html=True)
        _mc10, _mc11, _mc12 = st.columns(3, gap="medium")
        with _mc10:
            st.markdown(_method_card(_ICON_STETHO, "診療所を探す",
                "有床・無床診療所を含め、<br>施設基準届出データから直接検索します",
                go="clinic_search"), unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

    # グループ3: 地域全体を分析する ────────────────────────────
    _landing_group_header("地域全体を分析する", "二次医療圏単位で、地域医療構想の視点から俯瞰します")
    _mc7, _mc8, _mc9 = st.columns(3, gap="medium")
    with _mc7:
        st.markdown(_method_card(_ICON_CHART, "地域医療構想を分析",
            "二次医療圏ごとの急性期拠点・<br>機能分担をスコアリングします",
            go="region_vision"), unsafe_allow_html=True)

    _render_footer()
    st.stop()


# ── データ準備 ─────────────────────────────────────────────

if st.session_state.pop("_scroll_to_top", False):
    components.html(
        "<script>window.parent.document.querySelector('.main .block-container').scrollTo(0,0);</script>",
        height=0,
    )

df = st.session_state.df
year     = sel_year
pref     = sel_pref
region   = sel_region
hospital = sel_hospital


# ══════════════════════════════════════════════════════════
# 病院名で探すモード
# ══════════════════════════════════════════════════════════

if st.session_state.get("_view_mode") == "name_search":
    st.markdown("## 病院名で探す")

    _ns_df   = st.session_state.df
    _ns_year = int(_ns_df["報告年度"].max())

    with st.container(border=True, key="ns_filter_box"):
        _ns_kw = st.text_input(
            "🔍 病院名キーワード（部分一致）",
            placeholder="例：聖路加、旭川赤十字、大学病院",
            key="_ns_kw",
        )

    st.markdown("<div style='height:8px;'></div>", unsafe_allow_html=True)
    st.markdown('<div class="section-header">検索結果</div>', unsafe_allow_html=True)

    if not _ns_kw:
        st.info("病院名の一部を入力してください。")
    else:
        _ns_norm = _normalize_name(_ns_kw)
        _ns_year_df = _ns_df[_ns_df["報告年度"] == _ns_year].copy()
        _ns_year_df["_norm"] = _ns_year_df["医療機関名"].apply(_normalize_name)
        _ns_hits = _ns_year_df[_ns_year_df["_norm"].str.contains(_ns_norm, na=False)]

        if _ns_hits.empty:
            st.warning("見つかりませんでした。別のキーワードをお試しください。")
        else:
            _ns_hits = _ns_hits.sort_values("合計_許可病床数", ascending=False)
            st.markdown(_source_tag(_byosho_source(_ns_year)), unsafe_allow_html=True)
            st.caption(f"**{len(_ns_hits):,}件**")
            _NS_PAGE = 60
            _ns_cols = st.columns(3)
            for _ni, (_, _nr) in enumerate(_ns_hits.head(_NS_PAGE).iterrows()):
                with _ns_cols[_ni % 3]:
                    _nbeds = _nr.get("合計_許可病床数")
                    _stat = f"🛏 {int(_nbeds):,}床" if pd.notna(_nbeds) else "🛏 -床"
                    st.caption(f"{_stat}　{_nr['都道府県名']}　{_nr['二次医療圏名']}")
                    if st.button(f"🏥 {_nr['医療機関名']}", key=f"_ns_btn_{_ni}",
                                 use_container_width=True):
                        st.session_state["_nav_jump"] = {
                            "year": int(_nr["報告年度"]), "pref": str(_nr["都道府県名"]),
                            "region": str(_nr["二次医療圏名"]), "hospital": str(_nr["医療機関名"]),
                        }
                        st.session_state["_hospital_chosen"] = True
                        st.session_state["_view_mode"] = "detail"
                        st.rerun()
            if len(_ns_hits) > _NS_PAGE:
                st.caption(f"… 他 {len(_ns_hits)-_NS_PAGE:,}件（キーワードを絞り込んでください）")

    _render_footer()
    st.stop()


# ══════════════════════════════════════════════════════════
# 地域から選ぶモード
# ══════════════════════════════════════════════════════════

if st.session_state.get("_view_mode") == "region":

    st.markdown("## 地域から病院を選ぶ")

    with st.container(border=True, key="rg_filter_box"):
        st.markdown(
            "<div style='font-size:0.78rem;font-weight:700;color:#0B6653;"
            "letter-spacing:.03em;margin-bottom:10px;'>"
            "①年度 → ②都道府県 → ③二次医療圏 の順に絞り込んでください</div>",
            unsafe_allow_html=True,
        )
        _rg_c1, _rg_c2, _rg_c3 = st.columns(3)
        with _rg_c1:
            _rg_years = [int(y) for y in sorted(_df_all["報告年度"].unique(), reverse=True)]
            _rg_year  = st.selectbox("📅 年度", _rg_years, key="_rg_year")
        with _rg_c2:
            _rg_prefs = _sort_prefs(_df_all["都道府県名"].unique())
            if st.session_state.get("_rg_pref") not in _rg_prefs:
                st.session_state["_rg_pref"] = _rg_prefs[0] if _rg_prefs else None
            _rg_pref = st.selectbox("🗾 都道府県", _rg_prefs, key="_rg_pref")
        with _rg_c3:
            _rg_regions = sorted(
                r for r in _df_all[_df_all["都道府県名"] == _rg_pref]["二次医療圏名"].unique()
                if r != "不明"
            )
            if st.session_state.get("_rg_region") not in _rg_regions:
                st.session_state["_rg_region"] = _rg_regions[0] if _rg_regions else None
            _rg_region = st.selectbox("🏘️ 二次医療圏", _rg_regions, key="_rg_region")

    st.markdown("<div style='height:10px;'></div>", unsafe_allow_html=True)
    st.markdown('<div class="section-header">検索結果</div>', unsafe_allow_html=True)

    # 病院一覧
    _rg_extra_cols = ["医療機関名", "合計_許可病床数"]
    for _ec in ["合計稼働率", "救急搬送件数"]:
        if _ec in _df_all.columns:
            _rg_extra_cols.append(_ec)
    _rg_list = (
        _df_all[
            (_df_all["報告年度"] == _rg_year) &
            (_df_all["都道府県名"] == _rg_pref) &
            (_df_all["二次医療圏名"] == _rg_region)
        ][_rg_extra_cols]
        .sort_values("合計_許可病床数", ascending=False)
        .reset_index(drop=True)
    )

    if _rg_list.empty:
        st.info("この年度・地域のデータがありません")
    else:
        st.markdown(_source_tag(_byosho_source(_rg_year)), unsafe_allow_html=True)
        st.caption(f"{_rg_region}　**{len(_rg_list)}院** が該当")
        _rg_cols = st.columns(3)
        for _ri, _rrow in enumerate(_rg_list.itertuples(index=False)):
            _rname = _rg_list.iloc[_ri]["医療機関名"]
            _rbeds = _rg_list.iloc[_ri]["合計_許可病床数"]
            with _rg_cols[_ri % 3]:
                _stat_parts = [f"🛏 {int(_rbeds):,}床" if pd.notna(_rbeds) else "🛏 -床"]
                if "合計稼働率" in _rg_list.columns:
                    _rocc = _rg_list.iloc[_ri]["合計稼働率"]
                    if _rocc is not None and not pd.isna(_rocc):
                        _stat_parts.append(f"稼働{_rocc:.0f}%")
                if "救急搬送件数" in _rg_list.columns:
                    _remg = _rg_list.iloc[_ri]["救急搬送件数"]
                    if _remg is not None and not pd.isna(_remg) and _remg > 0:
                        _stat_parts.append("🚑救急")
                st.caption("　".join(_stat_parts))
                if st.button(
                    f"🏥 {_rname}",
                    key=f"_rg_nav_{_ri}",
                    use_container_width=True,
                ):
                    _rg_row = _df_all[
                        (_df_all["医療機関名"] == _rname) &
                        (_df_all["報告年度"]  == _rg_year)
                    ]
                    if not _rg_row.empty:
                        _rr = _rg_row.iloc[0]
                        st.session_state["_nav_jump"] = {
                            "year":     int(_rg_year),
                            "pref":     str(_rr["都道府県名"]),
                            "region":   str(_rr["二次医療圏名"]),
                            "hospital": _rname,
                        }
                        st.rerun()

    _render_footer()
    st.stop()


# ══════════════════════════════════════════════════════════
# 地図検索モード
# ══════════════════════════════════════════════════════════

if st.session_state.get("_view_mode") == "map":

    st.markdown("## 地図で病院を探す")
    st.caption("都道府県・二次医療圏を選択して病院の分布を地図上で確認できます。マーカーをクリックして「詳細を見る」で病院詳細に移動します。")

    try:
        import folium
        from streamlit_folium import st_folium as _st_folium_ms
        from geocoder import (
            load_cached_coords, count_uncached, has_official_locations,
            geocode_batch, geocode_address as _gc_addr, haversine_km as _hkm,
            load_all_hospital_coords, load_coords_from_parquet,
        )
        _MS_OK = True
    except ImportError as _me:
        _MS_OK = False
        st.error(f"地図ライブラリが見つかりません: {_me}")

    if _MS_OK:
        _ms_has_db  = DB_PATH.exists()
        _ms_has_loc = _LOCS_PARQUET.exists()

        if not _ms_has_db and not _ms_has_loc:
            st.warning("地図機能は公式座標データ（locations_cache.parquet）またはDuckDBがある場合のみ利用できます。")
        else:
            with st.container(border=True, key="ms_filter_box"):
                _ms_c1, _ms_c2, _ms_c3 = st.columns(3)
                with _ms_c1:
                    _ms_all_prefs = _sort_prefs(_df_all["都道府県名"].unique())
                    if st.session_state.get("_ms_pref") not in _ms_all_prefs:
                        st.session_state["_ms_pref"] = _ms_all_prefs[0] if _ms_all_prefs else None
                    _ms_pref = st.selectbox("🗾 都道府県", _ms_all_prefs, key="_ms_pref")
                with _ms_c2:
                    _ms_years = [int(y) for y in sorted(_df_all["報告年度"].unique(), reverse=True)]
                    _ms_year = st.selectbox("📅 年度", _ms_years, key="_ms_year")
                with _ms_c3:
                    _ms_scope = st.radio("表示範囲", ["都道府県全体", "二次医療圏を絞る"], horizontal=True, key="_ms_scope")

                _ms_regions = sorted(
                    r for r in _df_all[_df_all["都道府県名"] == _ms_pref]["二次医療圏名"].unique()
                    if r != "不明"
                )
                if _ms_scope == "二次医療圏を絞る" and _ms_regions:
                    if st.session_state.get("_ms_region") not in _ms_regions:
                        st.session_state["_ms_region"] = _ms_regions[0]
                    _ms_region = st.selectbox("🏘️ 二次医療圏", _ms_regions, key="_ms_region")
                    _ms_df = _df_all[
                        (_df_all["都道府県名"] == _ms_pref) &
                        (_df_all["二次医療圏名"] == _ms_region) &
                        (_df_all["報告年度"] == _ms_year)
                    ].copy()
                else:
                    _ms_region = None
                    _ms_df = _df_all[
                        (_df_all["都道府県名"] == _ms_pref) &
                        (_df_all["報告年度"] == _ms_year)
                    ].copy()

            # 座標読み込み
            _ms_pref_code = _PREF_ORDER.get(_ms_pref, _ms_pref)
            _ms_norm_geo: dict = {}
            if _ms_has_loc:
                try:
                    _ms_lp = pd.read_parquet(str(_LOCS_PARQUET), columns=["施設名", "lat", "lon", "都道府県名"])
                    _ms_lp = _ms_lp[_ms_lp["都道府県名"] == _ms_pref_code].dropna(subset=["施設名", "lat", "lon"])
                    _ms_lp["_norm"] = _ms_lp["施設名"].apply(_normalize_name)
                    _ms_norm_geo = dict(zip(_ms_lp["_norm"], zip(_ms_lp["lat"].astype(float), _ms_lp["lon"].astype(float))))
                except Exception:
                    pass
            _ms_geo_cache: dict = {}
            if _ms_has_db:
                _ms_geo_cache = load_cached_coords(str(DB_PATH), _ms_pref)

            def _ms_lookup(name):
                if name in _ms_geo_cache: return _ms_geo_cache[name]
                n = _normalize_name(name)
                if n in _ms_norm_geo: return _ms_norm_geo[n]
                return (None, None)

            _ms_df["lat"] = _ms_df["医療機関名"].map(lambda n: _ms_lookup(n)[0])
            _ms_df["lon"] = _ms_df["医療機関名"].map(lambda n: _ms_lookup(n)[1])
            _ms_valid = _ms_df.dropna(subset=["lat", "lon"])

            st.markdown("<div style='height:8px;'></div>", unsafe_allow_html=True)
            st.markdown('<div class="section-header">検索結果</div>', unsafe_allow_html=True)
            st.markdown(_source_tag(_byosho_source(_ms_year)), unsafe_allow_html=True)
            st.caption(f"{_ms_pref}{'　' + _ms_region if _ms_region else ''} — **{len(_ms_df):,}病院** / 座標あり {len(_ms_valid):,}病院")

            if _ms_valid.empty:
                st.info("表示できる病院がありません。座標データが必要です。")
            else:
                _ms_center_lat = float(_ms_valid["lat"].mean())
                _ms_center_lon = float(_ms_valid["lon"].mean())
                _ms_zoom = 10 if _ms_region else 8

                _ms_m = folium.Map(location=[_ms_center_lat, _ms_center_lon], zoom_start=_ms_zoom, tiles="CartoDB positron")
                _ms_max_beds = max(int(_ms_valid["合計_許可病床数"].max() or 1), 1)

                for _, _mr in _ms_valid.iterrows():
                    _mb = int(_mr.get("合計_許可病床数", 0) or 0)
                    _mk = int(_mr.get("合計_稼働病床数", 0) or 0)
                    _mo = f"{_mk / _mb * 100:.1f}%" if _mb > 0 else "—"
                    _mrad = max(5, min(22, _mb / _ms_max_beds * 22))
                    _mcol = "#e74c3c" if _mb >= 500 else "#e67e22" if _mb >= 300 else "#2ecc71" if _mb >= 100 else "#3498db"
                    folium.CircleMarker(
                        location=[float(_mr["lat"]), float(_mr["lon"])],
                        radius=_mrad, color="#555", weight=1,
                        fill=True, fill_color=_mcol, fill_opacity=0.75,
                        popup=folium.Popup(
                            (lambda _n, _mb, _mo, _pref, _reg: (
                                f'<div style="font-family:Meiryo,sans-serif;min-width:200px;line-height:1.6">'
                                f'<b style="font-size:13px">{_n}</b><br>'
                                f'<span style="color:#666;font-size:11px">{_pref} {_reg}</span>'
                                f'<hr style="margin:6px 0">許可病床数: <b>{_mb:,}床</b><br>稼働率: <b>{_mo}</b>'
                                f'<br><a href="#"'
                                f' onclick="window.open(window.top.location.origin+\'/?hospital=\'+encodeURIComponent(\'{_n}\'),\'_blank\');return false;"'
                                f' style="display:block;margin-top:10px;padding:7px 12px;'
                                f'background:#12886D;color:#fff;border-radius:8px;'
                                f'text-align:center;text-decoration:none;font-size:12px;font-weight:700;">'
                                f'詳細を見る →</a>'
                                f'</div>'
                            ))(_mr["医療機関名"], _mb, _mo, _mr["都道府県名"], _mr["二次医療圏名"]),
                            max_width=260
                        ),
                        tooltip=f"{_mr['医療機関名']}（{_mb:,}床）",
                    ).add_to(_ms_m)

                _ms_last = st.session_state.get("_ms_last_clicked")

                _ms_map_data = _st_folium_ms(_ms_m, width="100%", height=600, returned_objects=["last_object_clicked_tooltip"])
                _ms_tip = (_ms_map_data or {}).get("last_object_clicked_tooltip") or ""
                if _ms_tip:
                    _ms_clicked_name = re.sub(r"（[\d,]+床）$", "", _ms_tip).strip()
                    if _ms_clicked_name and (_ms_clicked_name in _ms_valid["医療機関名"].values):
                        st.session_state["_ms_last_clicked"] = _ms_clicked_name
                        _ms_last = _ms_clicked_name

                # クリック済みマーカーのアクション（地図の下）
                if _ms_last and (_ms_last in _ms_valid["医療機関名"].values):
                    _ms_cr = _ms_valid[_ms_valid["医療機関名"] == _ms_last].iloc[0]
                    _ms_beds = int(_ms_cr["合計_許可病床数"]) if pd.notna(_ms_cr.get("合計_許可病床数")) else 0
                    _ms_occ  = f'{_ms_cr["合計稼働率"]:.0f}%' if "合計稼働率" in _ms_cr and pd.notna(_ms_cr["合計稼働率"]) else "—"
                    st.markdown(
                        f'<div style="margin-top:12px;padding:16px 20px;'
                        f'background:#EAF4F0;border:2px solid #12886D;border-radius:12px;'
                        f'display:flex;align-items:center;gap:16px;">'
                        f'<div style="flex:1;">'
                        f'<div style="font-size:0.75rem;color:#0B6653;font-weight:700;letter-spacing:0.05em;margin-bottom:4px;">選択中の病院</div>'
                        f'<div style="font-size:1rem;font-weight:800;color:#111827;">{_ms_last}</div>'
                        f'<div style="font-size:0.8rem;color:#6b7280;margin-top:2px;">'
                        f'{_ms_cr["都道府県名"]} {_ms_cr["二次医療圏名"]}　🛏 {_ms_beds:,}床　稼働率 {_ms_occ}</div>'
                        f'</div></div>',
                        unsafe_allow_html=True,
                    )
                    if st.button("この病院の詳細を見る →", key="_ms_goto_detail", type="primary", use_container_width=True):
                        st.session_state["_nav_jump"] = {
                            "hospital": _ms_last,
                            "pref": str(_ms_cr["都道府県名"]),
                            "region": str(_ms_cr["二次医療圏名"]),
                            "year": int(_ms_year),
                        }
                        st.session_state.pop("_ms_last_clicked", None)
                        st.rerun()

    _render_footer()
    st.stop()


# ══════════════════════════════════════════════════════════
# 距離・所要時間検索モード
# ══════════════════════════════════════════════════════════

if st.session_state.get("_view_mode") == "distance":
    from geocoder import geocode_address as _dist_gc, haversine_km as _dist_hkm, osrm_durations as _dist_osrm

    st.markdown("## 距離・所要時間で病院を探す")

    with st.container(border=True, key="dist_filter_box"):
        _dist_addr = st.text_input(
            "📍 出発地（住所・ランドマーク）",
            placeholder="例: 東京都新宿区西新宿2丁目8",
            key="_dist_addr",
        )

        _dist_c1, _dist_c2 = st.columns([2, 4])
        with _dist_c1:
            _dist_max = st.number_input("所要時間（分以内）", min_value=5, max_value=180, value=30, step=5,
                key="_dist_max")
        with _dist_c2:
            _dist_mode = st.radio("移動手段", ["🚗 車", "🚌 電車・バス"],
                horizontal=True, key="_dist_mode")

    _dist_years = [int(y) for y in sorted(_df_all["報告年度"].unique(), reverse=True)]
    _dist_year  = _dist_years[0]
    _dist_pref  = "全都道府県"

    _dist_has_coords = DB_PATH.exists() or _LOCS_PARQUET.exists()
    if not _dist_has_coords:
        st.warning("距離検索には公式座標データ（locations_cache.parquet）またはDuckDBが必要です。")
    elif not _dist_addr:
        st.info("出発地を入力してください。")
    else:
        _dist_col_options = {
            "救急搬送件数":         "救急搬送件数",
            "稼働率":               "合計稼働率",
            "高度急性期（病床数）": "高度急性期_許可病床数",
            "急性期（病床数）":     "急性期_許可病床数",
            "回復期（病床数）":     "回復期_許可病床数",
            "慢性期（病床数）":     "慢性期_許可病床数",
            "常勤医師数":           "常勤医師数",
            "CT台数":              "CT台数",
            "MRI台数":             "MRI台数",
            "手術総数":             "手術総数",
            "全身麻酔手術数":       "全身麻酔手術数",
        }
        with st.expander("＋ 詳細条件を追加", expanded=False):
            _fcy1, _fcy2 = st.columns([1, 5])
            with _fcy1:
                _dist_year = st.selectbox("年度", _dist_years, key="_dist_year",
                    help="使用するデータの報告年度（デフォルト: 最新年度）")
            st.divider()
            _fca, _fcb, _fcc = st.columns(3)
            with _fca:
                st.markdown("**🚑 救急**")
                st.number_input("救急搬送件数（件以上）", min_value=0, step=100, key="_dist_f_emg_min",
                    help="救急搬送件数が指定値以上の病院のみ表示（0で条件なし）")
                st.markdown("**🔬 設備**")
                st.checkbox("CT 保有（1台以上）", key="_dist_f_ct",
                    help="CT台数合計 ≥ 1（様式1 施設票）")
                st.checkbox("MRI 保有（1台以上）", key="_dist_f_mri",
                    help="MRI台数合計 ≥ 1（様式1 施設票）")
                st.checkbox("手術支援ロボット", key="_dist_f_robot",
                    help="内視鏡手術支援機器台数 ≥ 1（様式1 施設票）")
                st.checkbox("アンギオ（血管連続撮影）", key="_dist_f_angio",
                    help="血管連続撮影装置台数 ≥ 1（様式1 施設票）")
            with _fcb:
                st.markdown("**🏥 病床種別（あり）**")
                st.checkbox("高度急性期", key="_dist_f_kodo",
                    help="高度急性期_許可病床数 ≥ 1 の病院のみ表示")
                st.checkbox("急性期",     key="_dist_f_kyusei",
                    help="急性期_許可病床数 ≥ 1 の病院のみ表示")
                st.checkbox("回復期",     key="_dist_f_kaifuku",
                    help="回復期_許可病床数 ≥ 1 の病院のみ表示")
                st.checkbox("慢性期",     key="_dist_f_mansei",
                    help="慢性期_許可病床数 ≥ 1 の病院のみ表示")
            with _fcc:
                st.markdown("**✂️ 手術（様式2）**")
                st.number_input("手術総数（件以上）", min_value=0, step=100, key="_dist_f_surg_min",
                    help="手術総数が指定値以上の病院のみ表示（0で条件なし）")
                st.number_input("全身麻酔手術（件以上）", min_value=0, step=50, key="_dist_f_zensui_min",
                    help="全身麻酔手術数が指定値以上の病院のみ表示（0で条件なし）")
                st.caption("臓器別（1件以上）")
                _foa, _fob = st.columns(2)
                with _foa:
                    st.checkbox("皮膚・皮下組織",   key="_dist_f_hifuka")
                    st.checkbox("筋骨格系・四肢",   key="_dist_f_kinkot")
                    st.checkbox("神経系・頭蓋",     key="_dist_f_shinkei")
                    st.checkbox("眼",               key="_dist_f_me")
                    st.checkbox("耳鼻咽喉",         key="_dist_f_jibika")
                    st.checkbox("顔面・口腔・頸部", key="_dist_f_ganmen")
                with _fob:
                    st.checkbox("胸部",       key="_dist_f_kyobu")
                    st.checkbox("心・脈管",   key="_dist_f_shin")
                    st.checkbox("腹部",       key="_dist_f_fukubu")
                    st.checkbox("尿路系・副腎", key="_dist_f_nyo")
                    st.checkbox("性器",       key="_dist_f_seiki")
                    st.checkbox("歯科",       key="_dist_f_shika")
            st.divider()
            st.markdown("**📊 結果テーブルの追加列**")
            st.caption("固定列: 医療機関名 / 直線距離 / 所要時間 / 都道府県 / 二次医療圏 / 許可病床数")
            st.multiselect(
                "追加表示列",
                options=list(_dist_col_options.keys()),
                default=["救急搬送件数", "稼働率"],
                key="_dist_result_cols",
                label_visibility="collapsed",
            )

        if st.button("🔍 検索する", type="primary", key="_dist_search_btn"):
            _origin = _cached_geocode_address(_dist_addr)
            if _origin is None:
                st.error(f"「{_dist_addr}」の座標が取得できませんでした。住所をより具体的に入力してください。")
            else:
                # 対象病院絞り込み
                _dist_df_base = _df_all[_df_all["報告年度"] == _dist_year].copy()
                if _dist_pref != "全都道府県":
                    _dist_df_base = _dist_df_base[_dist_df_base["都道府県名"] == _dist_pref]
                # 詳細条件フィルター（設備・救急・病床種別）
                def _fnum(col):
                    return pd.to_numeric(_dist_df_base[col], errors="coerce").fillna(0)
                if bool(st.session_state.get("_dist_f_ct")) and "CT台数" in _dist_df_base.columns:
                    _dist_df_base = _dist_df_base[_fnum("CT台数") >= 1]
                if bool(st.session_state.get("_dist_f_mri")) and "MRI台数" in _dist_df_base.columns:
                    _dist_df_base = _dist_df_base[_fnum("MRI台数") >= 1]
                if bool(st.session_state.get("_dist_f_robot")) and "内視鏡手術支援機器台数" in _dist_df_base.columns:
                    _dist_df_base = _dist_df_base[_fnum("内視鏡手術支援機器台数") >= 1]
                if bool(st.session_state.get("_dist_f_angio")) and "血管連続撮影装置台数" in _dist_df_base.columns:
                    _dist_df_base = _dist_df_base[_fnum("血管連続撮影装置台数") >= 1]
                _f_emg_min = int(st.session_state.get("_dist_f_emg_min") or 0)
                if _f_emg_min > 0 and "救急搬送件数" in _dist_df_base.columns:
                    _dist_df_base = _dist_df_base[_fnum("救急搬送件数") >= _f_emg_min]
                for _bt, _bkey in [("高度急性期", "_dist_f_kodo"), ("急性期", "_dist_f_kyusei"),
                                    ("回復期", "_dist_f_kaifuku"), ("慢性期", "_dist_f_mansei")]:
                    _bc = f"{_bt}_許可病床数"
                    if bool(st.session_state.get(_bkey)) and _bc in _dist_df_base.columns:
                        _dist_df_base = _dist_df_base[_fnum(_bc) >= 1]
                # 手術フィルター（surgery_df マージ）
                _f_surg_min   = int(st.session_state.get("_dist_f_surg_min") or 0)
                _f_zensui_min = int(st.session_state.get("_dist_f_zensui_min") or 0)
                _dist_organ_map = [
                    ("_dist_f_hifuka",  "皮膚・皮下組織"),
                    ("_dist_f_kinkot",  "筋骨格系・四肢・体幹"),
                    ("_dist_f_shinkei", "神経系・頭蓋"),
                    ("_dist_f_me",      "眼"),
                    ("_dist_f_jibika",  "耳鼻咽喉"),
                    ("_dist_f_ganmen",  "顔面・口腔・頸部"),
                    ("_dist_f_kyobu",   "胸部"),
                    ("_dist_f_shin",    "心・脈管"),
                    ("_dist_f_fukubu",  "腹部"),
                    ("_dist_f_nyo",     "尿路系・副腎"),
                    ("_dist_f_seiki",   "性器"),
                    ("_dist_f_shika",   "歯科"),
                ]
                _any_surg_filter = _f_surg_min > 0 or _f_zensui_min > 0 or any(
                    bool(st.session_state.get(k)) for k, _ in _dist_organ_map
                )
                _dist_auto_surg_cols = []  # 結果テーブルに自動追加する手術列
                if _any_surg_filter:
                    _surg_state = st.session_state.get("surgery_df")
                    if _surg_state is not None and not _surg_state.empty:
                        _sy = (_surg_state[_surg_state["報告年度"] == _dist_year]
                               if "報告年度" in _surg_state.columns else _surg_state)
                        _surg_need = (
                            [c for c in ["手術総数", "全身麻酔手術数"] if c in _sy.columns]
                            + [f"手術_{lb}" for _, lb in _dist_organ_map if f"手術_{lb}" in _sy.columns]
                        )
                        if _surg_need:
                            _jk = ("医療機関コード"
                                   if "医療機関コード" in _sy.columns and "医療機関コード" in _dist_df_base.columns
                                   else "医療機関名")
                            _sy_m = _sy[[_jk] + _surg_need].drop_duplicates(_jk).copy()
                            _sy_m[_jk] = _sy_m[_jk].astype(str).str.strip()
                            _dist_df_base = _dist_df_base.merge(_sy_m, on=_jk, how="left", suffixes=("", "_sy"))
                            for _sc in _surg_need:
                                _dist_df_base[_sc] = pd.to_numeric(_dist_df_base[_sc], errors="coerce").fillna(0)
                        if _f_surg_min > 0 and "手術総数" in _dist_df_base.columns:
                            _dist_df_base = _dist_df_base[
                                pd.to_numeric(_dist_df_base["手術総数"], errors="coerce").fillna(0) >= _f_surg_min]
                            _dist_auto_surg_cols.append("手術総数")
                        if _f_zensui_min > 0 and "全身麻酔手術数" in _dist_df_base.columns:
                            _dist_df_base = _dist_df_base[
                                pd.to_numeric(_dist_df_base["全身麻酔手術数"], errors="coerce").fillna(0) >= _f_zensui_min]
                            _dist_auto_surg_cols.append("全身麻酔手術数")
                        for _okey, _olabel in _dist_organ_map:
                            _ocol = f"手術_{_olabel}"
                            if bool(st.session_state.get(_okey)) and _ocol in _dist_df_base.columns:
                                _dist_df_base = _dist_df_base[
                                    pd.to_numeric(_dist_df_base[_ocol], errors="coerce").fillna(0) >= 1]
                                _dist_auto_surg_cols.append(_ocol)
                    else:
                        st.warning("⚠️ 手術条件を指定しましたが手術データが読み込まれていません。手術フィルターは無効です。")

                from geocoder import load_all_hospital_coords as _dist_lac
                _dist_all_coords = _dist_lac(
                    db_path=str(DB_PATH) if DB_PATH.exists() else None,
                    parquet_path=str(_LOCS_PARQUET) if _LOCS_PARQUET.exists() else None,
                )

                def _dist_lookup(name):
                    return _dist_all_coords.get(name) or _dist_all_coords.get(_normalize_name(name))

                _dist_names = _dist_df_base["医療機関名"].tolist()
                _dist_known = [(n, _dist_lookup(n)) for n in _dist_names if _dist_lookup(n)]
                _dist_max_sec = _dist_max * 60

                if not _dist_known:
                    st.warning("座標データが取得できる病院がありません。")
                else:
                    _dist_dests = [coords for _, coords in _dist_known]
                    if "車" in _dist_mode:
                        with st.spinner("OSRM で所要時間を計算中..."):
                            _dist_durs = _dist_osrm(_origin[0], _origin[1], _dist_dests)
                        _transit_note = False
                    else:
                        _dist_durs = [
                            _dist_hkm(_origin[0], _origin[1], lat, lon) / 25.0 * 3600
                            for lat, lon in _dist_dests
                        ]
                        _transit_note = True

                    _dist_rows = []
                    for (name, coords), dur in zip(_dist_known, _dist_durs):
                        km = _dist_hkm(_origin[0], _origin[1], *coords)
                        mins = round(dur / 60, 1) if dur is not None else None
                        if mins is not None and dur <= _dist_max_sec:
                            _dist_rows.append({"医療機関名": name, "直線距離(km)": round(km, 1), "所要時間(分)": mins})

                    if not _dist_rows:
                        st.info(f"{_dist_max}分以内に見つかりません。上限時間を広げてみてください。")
                    else:
                        _dist_result = pd.DataFrame(_dist_rows).sort_values("所要時間(分)").reset_index(drop=True)
                        _dist_extra_cols = ["医療機関名", "都道府県名", "二次医療圏名", "合計_許可病床数"]
                        for _sel_label in st.session_state.get("_dist_result_cols", []):
                            _sel_col = _dist_col_options.get(_sel_label)
                            if _sel_col and _sel_col in _dist_df_base.columns:
                                _dist_extra_cols.append(_sel_col)
                        for _asc in _dist_auto_surg_cols:
                            if _asc not in _dist_extra_cols and _asc in _dist_df_base.columns:
                                _dist_extra_cols.append(_asc)
                        _dist_result = _dist_result.merge(
                            _dist_df_base[_dist_extra_cols].drop_duplicates("医療機関名"),
                            on="医療機関名", how="left"
                        )
                        _dist_result.index += 1

                        st.markdown("<div style='height:8px;'></div>", unsafe_allow_html=True)
                        st.markdown('<div class="section-header">検索結果</div>', unsafe_allow_html=True)
                        st.markdown(_source_tag(_byosho_source(_dist_year)), unsafe_allow_html=True)
                        if _transit_note:
                            st.caption("※ 公共交通は直線距離÷25km/hの近似値です")
                        st.caption(f"**{len(_dist_result):,}病院** が {_dist_max}分以内 — 出発地: {_dist_addr}")
                        _dist_col_cfg = {
                            "直線距離(km)":         st.column_config.NumberColumn("直線距離",     format="%.1f km"),
                            "所要時間(分)":         st.column_config.NumberColumn("所要時間",     format="%.1f 分"),
                            "合計_許可病床数":       st.column_config.NumberColumn("許可病床数",   format="%,d 床"),
                            "高度急性期_許可病床数": st.column_config.NumberColumn("高度急性期",   format="%,d 床"),
                            "急性期_許可病床数":     st.column_config.NumberColumn("急性期",       format="%,d 床"),
                            "回復期_許可病床数":     st.column_config.NumberColumn("回復期",       format="%,d 床"),
                            "慢性期_許可病床数":     st.column_config.NumberColumn("慢性期",       format="%,d 床"),
                            "合計稼働率":            st.column_config.ProgressColumn("稼働率", format="%.1f%%", min_value=0, max_value=100),
                            "救急搬送件数":          st.column_config.NumberColumn("救急搬送",     format="%,d 件"),
                            "常勤医師数":            st.column_config.NumberColumn("常勤医師数",   format="%,d 人"),
                            "CT台数":               st.column_config.NumberColumn("CT台数",       format="%,d 台"),
                            "MRI台数":              st.column_config.NumberColumn("MRI台数",      format="%,d 台"),
                            "手術総数":              st.column_config.NumberColumn("手術総数",     format="%,d 件"),
                            "全身麻酔手術数":        st.column_config.NumberColumn("全身麻酔手術", format="%,d 件"),
                        }
                        for _asc in _dist_auto_surg_cols:
                            if _asc.startswith("手術_"):
                                _dist_col_cfg[_asc] = st.column_config.NumberColumn(
                                    _asc.replace("手術_", ""), format="%,d 件")
                        st.dataframe(_dist_result, use_container_width=True, column_config=_dist_col_cfg)
                        st.divider()
                        st.markdown("### 🏥 病院を選んで詳細を見る")
                        _dist_nav_cols = st.columns(3)
                        for _di, _dname in enumerate(_dist_result["医療機関名"].tolist()[:30]):
                            with _dist_nav_cols[_di % 3]:
                                if st.button(f"🏥 {_dname}", key=f"_dist_nav_{_di}", use_container_width=True):
                                    _drow = _dist_df_base[_dist_df_base["医療機関名"] == _dname]
                                    if not _drow.empty:
                                        _dr = _drow.iloc[0]
                                        st.session_state["_nav_jump"] = {
                                            "year": int(_dr["報告年度"]),
                                            "pref": str(_dr["都道府県名"]),
                                            "region": str(_dr["二次医療圏名"]),
                                            "hospital": _dname,
                                        }
                                        st.rerun()

    _render_footer()
    st.stop()


# ══════════════════════════════════════════════════════════
# 診療所検索モード（施設基準届出データを直接検索）
# ══════════════════════════════════════════════════════════
# 既存の病院検索・地域から選ぶ等は病床機能報告データが起点のため、
# ほぼ「病院」しか検索対象に載らない（有床診療所は施設基準届出データの
# 3,916件のうち病床機能報告と名称一致するのは1件のみ）。有床・無床
# 診療所を探すには、施設基準届出データ（shisetsu_kijun_cache.parquet）
# を直接ベースにした、この専用の検索フローが必要。

if st.session_state.get("_view_mode") == "clinic_search":
    st.markdown("## 診療所を探す")
    st.caption(
        "病床機能報告（病院が中心）ではカバーされない有床診療所・無床診療所を含め、"
        "施設基準届出データから直接検索します。"
    )

    _cs_df = _load_shisetsu_kijun()
    if _cs_df is None:
        st.warning("施設基準届出データが見つかりません。")
    else:
        with st.container(border=True, key="cs_filter_box"):
            _cs_c1, _cs_c2 = st.columns(2)
            with _cs_c1:
                _cs_prefs = ["全都道府県"] + _sort_prefs(_cs_df["都道府県名"].unique())
                _cs_pref = st.selectbox("🗾 都道府県", _cs_prefs, key="cs_pref")
            with _cs_c2:
                _cs_fac_sel = st.multiselect(
                    "🏷️ 施設種別",
                    options=["病院", "有床診療所", "無床診療所"],
                    default=["有床診療所", "無床診療所"],
                    key="cs_fac_type",
                    help="入院基本料の届出パターンから判定（届出が無い場合は無床診療所と推定）",
                )
            _cs_kw = st.text_input(
                "医療機関名キーワード", placeholder="例：〇〇クリニック、△△医院", key="cs_kw")
            _cs_kijun_kw = st.text_input(
                "届出名称キーワード（部分一致）", placeholder="例：在宅療養支援診療所", key="cs_kijun_kw")

        # ── フィルタリング ──
        _cs_sub = _cs_df.copy()
        if _cs_pref != "全都道府県":
            _cs_sub = _cs_sub[_cs_sub["都道府県名"] == _cs_pref]
        if _cs_fac_sel:
            _cs_sub = _cs_sub[_cs_sub["施設種別"].isin(_cs_fac_sel)]
        if _cs_kw:
            _cs_norm_kw = _normalize_name(_cs_kw)
            _cs_sub = _cs_sub[_cs_sub["医療機関名称"].apply(_normalize_name).str.contains(_cs_norm_kw, na=False)]
        if _cs_kijun_kw.strip():
            _cs_sub = _cs_sub[_cs_sub["受理届出名称"].str.contains(_cs_kijun_kw.strip(), na=False)]

        st.markdown("<div style='height:8px;'></div>", unsafe_allow_html=True)
        st.markdown('<div class="section-header">🩺 検索結果</div>', unsafe_allow_html=True)

        _cs_key_cols = ["都道府県コード", "医療機関番号"]
        _cs_insts = (
            _cs_sub[_cs_key_cols + ["都道府県名", "医療機関名称", "住所", "施設種別"]]
            .drop_duplicates(subset=_cs_key_cols)
            .sort_values("医療機関名称")
        )
        st.caption(f"**{len(_cs_insts):,}件** が該当")

        if _cs_insts.empty:
            st.info("条件に一致する医療機関が見つかりませんでした。絞り込み条件を減らしてみてください。")
        else:
            _CS_FAC_COLOR = {"病院": "#3b82f6", "有床診療所": "#f59e0b", "無床診療所": "#6b7280"}
            _CS_PAGE_SIZE = 100
            _cs_page = _cs_insts.head(_CS_PAGE_SIZE)

            # 届出項目一覧は表示対象（最大100件）分だけに絞ってからgroupbyする
            # （マッチ件数が多い場合、全件に対してapplyすると非常に遅くなるため）。
            # 都道府県コードはcategory dtypeなのでstrに変換してから連結する。
            _cs_page_key = _cs_page["都道府県コード"].astype(str) + "_" + _cs_page["医療機関番号"].astype(str)
            _cs_sub_key  = _cs_sub["都道府県コード"].astype(str) + "_" + _cs_sub["医療機関番号"].astype(str)
            _cs_sub_page = _cs_sub[_cs_sub_key.isin(set(_cs_page_key))]
            _cs_items_by_inst = _cs_sub_page.groupby(_cs_key_cols)["受理届出名称"].apply(
                lambda s: sorted(s.dropna().unique())
            ).to_dict()

            for _, _cr in _cs_page.iterrows():
                _fac_color = _CS_FAC_COLOR.get(_cr["施設種別"], "#6b7280")
                # 住所の先頭（市区町村名部分）だけをタイトルに出す
                _cs_city = re.match(r"^.{0,10}?[市区町村]", str(_cr.get("住所") or ""))
                _cs_city_str = _cs_city.group() if _cs_city else ""
                with st.expander(f"{_cr['医療機関名称']}　（{_cr['都道府県名']}{'　' + _cs_city_str if _cs_city_str else ''}）"):
                    st.markdown(
                        f'<span style="display:inline-block;background:{_fac_color}22;'
                        f'color:{_fac_color};border:1px solid {_fac_color}55;'
                        f'border-radius:10px;padding:3px 10px;font-size:0.78rem;font-weight:700;">'
                        f'🏷️ {_cr["施設種別"]}</span>',
                        unsafe_allow_html=True,
                    )
                    _cs_full_addr = str(_cr.get("住所") or "").strip()
                    if _cs_full_addr:
                        st.caption(f"📍 {_cs_full_addr}")
                    _cs_items = _cs_items_by_inst.get(
                        (_cr["都道府県コード"], _cr["医療機関番号"]), []
                    )
                    st.markdown(f"**届出項目（{len(_cs_items)}件）**")
                    for _it in _cs_items:
                        st.markdown(f"- {_it}")

            if len(_cs_insts) > _CS_PAGE_SIZE:
                st.caption(f"… 他 {len(_cs_insts) - _CS_PAGE_SIZE:,}件（都道府県やキーワードで絞り込んでください）")

    _render_footer()
    st.stop()


# ══════════════════════════════════════════════════════════
# 詳細条件検索モード
# ══════════════════════════════════════════════════════════

if st.session_state.get("_view_mode") == "search":
    st.markdown("## 条件で病院を検索")

    # ════════════════════════════════════════════════
    # STEP 1: エリアを絞り込む
    # ════════════════════════════════════════════════
    st.markdown(
        "<div style='border-left:4px solid #12886D;padding:8px 14px;background:#EAF4F0;"
        "border-radius:0 6px 6px 0;margin-bottom:12px;'>"
        "<span style='font-weight:700;font-size:1rem;color:#1e3a5f;'>① エリアを絞り込む</span>"
        "<span style='color:#6b7280;font-size:0.8rem;margin-left:10px;'>"
        "都道府県・二次医療圏・病院名はそれぞれ単独でも組み合わせても使えます（すべて省略で全国対象）</span>"
        "</div>",
        unsafe_allow_html=True,
    )
    _sa, _sb, _sc = st.columns([1, 2, 2])
    with _sa:
        s_years_list = [int(y) for y in sorted(df["報告年度"].dropna().unique(), reverse=True)]
        s_year = st.selectbox("📅 年度", s_years_list, key="s_year")
    with _sb:
        s_all_prefs = ["全都道府県"] + _sort_prefs(df["都道府県名"].unique())
        s_pref = st.selectbox("🗾 都道府県", s_all_prefs, key="s_pref")
    with _sc:
        if s_pref != "全都道府県":
            s_all_regions = ["全二次医療圏"] + sorted(
                r for r in df[df["都道府県名"] == s_pref]["二次医療圏名"].unique()
                if r != "不明"
            )
        else:
            s_all_regions = ["全二次医療圏"]
        if st.session_state.get("s_region") not in s_all_regions:
            st.session_state["s_region"] = "全二次医療圏"
        s_region = st.selectbox("🏘️ 二次医療圏", s_all_regions, key="s_region")
    s_kw = st.text_input(
        "病院名キーワード",
        placeholder="例: 大学病院、聖路加、赤十字　（部分一致）",
        key="s_kw",
    )

    # 所要時間（折りたたみ）
    _tt_db_ok = DB_PATH.exists() or _LOCS_PARQUET.exists()
    _tt_has_input = bool(st.session_state.get("s_tt_addr", ""))
    with st.expander("📍 出発地からの所要時間で絞り込む", expanded=_tt_has_input):
        if not _tt_db_ok:
            st.caption("座標データ（locations_cache.parquet）が必要です")
        s_tt_addr = st.text_input(
            "出発地（住所・ランドマーク）",
            placeholder="例: 東京都新宿区西新宿2丁目8",
            key="s_tt_addr",
            disabled=not _tt_db_ok,
        )
        if s_tt_addr and _tt_db_ok:
            _tt1, _tt2 = st.columns(2)
            with _tt1:
                s_tt_mode = st.radio("移動手段", ["車（OSRM）", "公共交通（近似）"], horizontal=True, key="s_tt_mode")
            with _tt2:
                s_tt_max = st.slider("上限（分）", 15, 90, 30, step=15, key="s_tt_max")
        else:
            s_tt_mode = st.session_state.get("s_tt_mode", "車（OSRM）")
            s_tt_max  = st.session_state.get("s_tt_max",  30)

    # ════════════════════════════════════════════════
    # STEP 2: 絞り込み条件を選ぶ
    # ════════════════════════════════════════════════
    st.markdown("<div style='margin-top:32px;'></div>", unsafe_allow_html=True)
    st.divider()
    st.markdown(
        "<div style='border-left:4px solid #f59e0b;padding:8px 14px;background:#fffbeb;"
        "border-radius:0 6px 6px 0;margin-bottom:16px;'>"
        "<span style='font-weight:700;font-size:1rem;color:#78350f;'>② 絞り込み条件を選ぶ</span>"
        "<span style='color:#6b7280;font-size:0.8rem;margin-left:10px;'>"
        "タブを切り替えて条件を設定（複数タブの条件はAND）</span>"
        "</div>",
        unsafe_allow_html=True,
    )
    st.markdown("""
<style>
/* 検索条件タブをフォルダ型に */
div[data-testid="stTabs"] > div:first-child {
    gap: 4px;
}
div[data-testid="stTabs"] button[data-baseweb="tab"] {
    font-size: 0.9rem !important;
    font-weight: 600 !important;
    padding: 8px 20px !important;
    border-radius: 8px 8px 0 0 !important;
    border: 1.5px solid #e5e7eb !important;
    border-bottom: none !important;
    background: #f9fafb !important;
    color: #6b7280 !important;
    margin-bottom: -1px;
}
div[data-testid="stTabs"] button[data-baseweb="tab"][aria-selected="true"] {
    background: #ffffff !important;
    color: #1f2937 !important;
    border-color: #d1d5db !important;
    border-bottom: 2px solid #ffffff !important;
}
div[data-testid="stTabPanel"] {
    border: 1.5px solid #e5e7eb;
    border-radius: 0 8px 8px 8px;
    padding: 16px !important;
    background: #ffffff;
}
</style>
""", unsafe_allow_html=True)
    _tab_equip, _tab_surg, _tab_kijun = st.tabs(["🏥 医療設備", "✂️ 手術", "📋 施設基準届出"])

    with _tab_equip:
        _eq1, _eq2, _eq3 = st.columns(3)
        with _eq1:
            st.markdown("**CT**")
            ct_filter = st.radio(
                "CT", ["指定なし", "CTあり（合計）", "CTなし（合計）", "スペック別"],
                key="ct_filter", label_visibility="collapsed",
            )
            s_ck_ct64 = s_ck_ct16p = s_ck_ct16m = False
            if ct_filter == "スペック別":
                s_ck_ct64  = st.checkbox("64列以上",  key="s_ck_ct64")
                s_ck_ct16p = st.checkbox("16〜64列",  key="s_ck_ct16p")
                s_ck_ct16m = st.checkbox("16列未満",  key="s_ck_ct16m")
            st.markdown("**MRI**")
            mri_filter = st.radio(
                "MRI", ["指定なし", "MRIあり（合計）", "MRIなし（合計）", "スペック別"],
                key="mri_filter", label_visibility="collapsed",
            )
            s_ck_mri3t = s_ck_mri15p = s_ck_mri15m = False
            if mri_filter == "スペック別":
                s_ck_mri3t  = st.checkbox("3T以上",   key="s_ck_mri3t")
                s_ck_mri15p = st.checkbox("1.5〜3T",  key="s_ck_mri15p")
                s_ck_mri15m = st.checkbox("1.5T未満", key="s_ck_mri15m")
        with _eq2:
            st.markdown("**放射線治療**")
            s_has_imrt       = st.checkbox("IMRT（強度変調放射線治療）あり", key="s_has_imrt")
            s_has_cyberknife = st.checkbox("サイバーナイフあり",             key="s_has_cyberknife")
            s_has_gamma      = st.checkbox("ガンマナイフあり",               key="s_has_gamma")
            st.markdown("**核医学**")
            s_has_pet  = st.checkbox("PET / PET-CTあり", key="s_has_pet")
            s_has_spect = st.checkbox("SPECTあり",       key="s_has_spect")
        with _eq3:
            st.markdown("**手術・カテーテル**")
            s_has_robot_eq = st.checkbox("手術支援ロボットあり",          key="s_has_robot_eq")
            s_has_angio    = st.checkbox("アンギオ（血管連続撮影）あり",  key="s_has_angio")
            st.markdown("**その他**")
            s_has_mammo = st.checkbox("マンモグラフィあり", key="s_has_mammo")

    with _tab_surg:
        _sg1, _sg2 = st.columns([1, 1])
        with _sg1:
            s_surg_mode = st.radio(
                "集計対象", ["手術（全数）", "全身麻酔の手術"], key="s_surg_mode",
                help="・手術（全数）→ データ列: 手術_[臓器名]\n・全身麻酔の手術 → データ列: 全麻_[臓器名]",
            )
            s_surg_logic = st.radio(
                "複数選択の扱い", ["AND（すべて該当）", "OR（いずれか該当）"], key="s_surg_logic",
            )
            st.caption("術式（1件以上）")
            s_ck_robot_s = st.checkbox("ロボット支援手術", key="s_ck_robot_s")
            s_ck_fuku    = st.checkbox("腹腔鏡下手術",     key="s_ck_fuku")
            s_ck_kyou    = st.checkbox("胸腔鏡下手術",     key="s_ck_kyou")
        with _sg2:
            st.caption("臓器別（1件以上）")
            _oa, _ob = st.columns(2)
            with _oa:
                s_ck_hifuka  = st.checkbox("皮膚・皮下組織",   key="s_ck_hifuka")
                s_ck_kinkot  = st.checkbox("筋骨格系・四肢",   key="s_ck_kinkot")
                s_ck_shinkei = st.checkbox("神経系・頭蓋",     key="s_ck_shinkei")
                s_ck_me      = st.checkbox("眼",               key="s_ck_me")
                s_ck_jibika  = st.checkbox("耳鼻咽喉",         key="s_ck_jibika")
                s_ck_ganmen  = st.checkbox("顔面・口腔・頸部", key="s_ck_ganmen")
            with _ob:
                s_ck_kyobu   = st.checkbox("胸部",             key="s_ck_kyobu")
                s_ck_shin    = st.checkbox("心・脈管",          key="s_ck_shin")
                s_ck_fukubu  = st.checkbox("腹部",             key="s_ck_fukubu")
                s_ck_nyo     = st.checkbox("尿路系・副腎",     key="s_ck_nyo")
                s_ck_seiki   = st.checkbox("性器",             key="s_ck_seiki")
                s_ck_shika   = st.checkbox("歯科",             key="s_ck_shika")

    with _tab_kijun:
        _sk_df_filt = _load_shisetsu_kijun()
        if _sk_df_filt is not None:
            # カテゴリ別届出グループ定義
            _SK_SEARCH_GROUPS = [
                ("入院体制・病棟", [
                    ("一般病棟入院基本料",          "一般病棟入院基本料"),
                    ("特定機能病院",                "特定機能病院入院基本料"),
                    ("地域包括ケア病棟",            "地域包括ケア病棟入院料"),
                    ("地域包括医療病棟",            "地域包括医療病棟入院料"),
                    ("回復期リハビリ病棟",          "回復期リハビリテーション病棟入院料"),
                    ("緩和ケア病棟",                "緩和ケア病棟入院料"),
                    ("療養病棟",                    "療養病棟入院基本料"),
                    ("障害者施設等病棟",            "障害者施設等入院基本料"),
                    ("急性期充実体制加算",          "急性期充実体制加算"),
                    ("総合入院体制加算",            "総合入院体制加算"),
                    ("精神病棟",                    "精神病棟入院基本料"),
                ]),
                ("看護体制・人員", [
                    ("医師事務作業補助体制加算",    "医師事務作業補助体制加算"),
                    ("看護補助加算",                "看護補助加算"),
                    ("急性期看護補助体制加算",      "急性期看護補助体制加算"),
                    ("夜間急性期看護補助体制加算",  "夜間急性期看護補助体制加算"),
                    ("地域医療体制確保加算",        "地域医療体制確保加算"),
                    ("夜間看護体制加算",            "夜間看護体制加算"),
                ]),
                ("救急・集中治療", [
                    ("救急医療管理加算",            "救急医療管理加算"),
                    ("救命救急入院料",              "救命救急入院料"),
                    ("超急性期脳卒中加算(tPA)",     "超急性期脳卒中加算"),
                    ("ICU",                         "集中治療室管理料"),
                    ("HCU",                         "ハイケアユニット入院医療管理料"),
                    ("NICU",                        "新生児集中治療室管理料"),
                    ("脳卒中ケアユニット",          "脳卒中ケアユニット入院医療管理料"),
                ]),
                ("手術・麻酔・カテーテル", [
                    ("麻酔管理料(Ⅰ)",              "麻酔管理料（Ⅰ）"),
                    ("ロボット手術",                "ロボット支援下内視鏡手術用支援機器加算"),
                    ("心臓カテーテル",              "心臓カテーテル法による諸検査"),
                    ("経皮的冠動脈形成術",          "経皮的冠動脈形成術"),
                    ("人工心肺",                    "人工心肺"),
                    ("大動脈バルーンパンピング法",  "大動脈バルーンパンピング法"),
                    ("体外衝撃波腎・尿管結石破砕術", "体外衝撃波腎・尿管結石破砕術"),
                    ("周術期薬学管理料",            "周術期薬学管理料"),
                    ("輸血管理料",                  "輸血管理料"),
                ]),
                ("放射線治療・画像診断", [
                    ("放射線治療（体外照射）",      "放射線治療（体外照射）"),
                    ("粒子線治療",                  "粒子線治療"),
                    ("外来放射線照射診療料",        "外来放射線照射診療料"),
                    ("放射線治療専任加算",          "放射線治療専任加算"),
                    ("画像診断管理加算",            "画像診断管理加算"),
                ]),
                ("リハビリ", [
                    ("脳血管疾患等リハビリ(Ⅰ)",    "脳血管疾患等リハビリテーション料（Ⅰ）"),
                    ("脳血管疾患等リハビリ(Ⅱ)",    "脳血管疾患等リハビリテーション料（Ⅱ）"),
                    ("運動器リハビリ(Ⅰ)",          "運動器リハビリテーション料（Ⅰ）"),
                    ("呼吸器リハビリ(Ⅰ)",          "呼吸器リハビリテーション料（Ⅰ）"),
                    ("心大血管リハビリ(Ⅰ)",        "心大血管疾患リハビリテーション料（Ⅰ）"),
                    ("がん患者リハビリ",            "がん患者リハビリテーション料"),
                    ("廃用症候群リハビリ(Ⅰ)",      "廃用症候群リハビリテーション料（Ⅰ）"),
                ]),
                ("がん", [
                    ("がん患者指導管理料",          "がん患者指導管理料"),
                    ("外来化学療法加算",            "外来化学療法加算"),
                    ("外来腫瘍化学療法診療料",      "外来腫瘍化学療法診療料"),
                    ("がん治療連携指導料",          "がん治療連携指導料"),
                    ("がんゲノムプロファイリング",  "がんゲノムプロファイリング評価提供料"),
                    ("がん拠点病院加算",            "がん拠点病院加算"),
                ]),
                ("産科・周産期・小児", [
                    ("ハイリスク分娩管理加算",      "ハイリスク分娩管理加算"),
                    ("ハイリスク妊娠管理加算",      "ハイリスク妊娠管理加算"),
                    ("総合周産期母子医療センター",  "総合周産期母子医療センター"),
                    ("周産期母子医療センター",      "周産期母子医療センター"),
                    ("小児入院医療管理料",          "小児入院医療管理料"),
                    ("新生児入院医療管理加算",      "新生児入院医療管理加算"),
                ]),
                ("精神・認知症", [
                    ("精神科救急入院料",            "精神科救急入院料"),
                    ("精神科急性期治療病棟",        "精神科急性期治療病棟入院料"),
                    ("精神科リエゾン",              "精神科リエゾンチーム加算"),
                    ("認知症ケア加算",              "認知症ケア加算"),
                    ("精神科訪問看護",              "精神科訪問看護・指導料"),
                    ("依存症集団療法",              "依存症集団療法"),
                    ("通院・在宅精神療法",          "通院・在宅精神療法"),
                ]),
                ("感染症・透析・内視鏡", [
                    ("感染対策向上加算",            "感染対策向上加算"),
                    ("抗菌薬適正使用支援加算",      "抗菌薬適正使用支援加算"),
                    ("人工腎臓（透析）",            "人工腎臓"),
                    ("腹膜透析",                    "腹膜透析"),
                    ("内視鏡的粘膜下層剥離術",      "内視鏡的粘膜下層剥離術"),
                    ("消化器内視鏡",                "消化器内視鏡"),
                ]),
                ("栄養・褥瘡・糖尿病", [
                    ("栄養サポートチーム加算",      "栄養サポートチーム加算"),
                    ("褥瘡ハイリスク患者ケア加算",  "褥瘡ハイリスク患者ケア加算"),
                    ("糖尿病合併症管理料",          "糖尿病合併症管理料"),
                    ("糖尿病透析予防指導管理料",    "糖尿病透析予防指導管理料"),
                    ("後発医薬品使用体制加算",      "後発医薬品使用体制加算"),
                ]),
                ("在宅・外来・地域連携", [
                    ("在宅療養後方支援病院",        "在宅療養後方支援病院"),
                    ("地域包括診療料",              "地域包括診療料"),
                    ("在宅患者訪問診療料",          "在宅患者訪問診療料"),
                    ("地域連携診療計画加算",        "地域連携診療計画加算"),
                    ("在宅医療DX情報活用加算",      "在宅医療ＤＸ情報活用加算"),
                    ("外来・在宅ベースアップ評価料", "外来・在宅ベースアップ評価料"),
                ]),
                ("入院管理・安全・データ", [
                    ("データ提出加算",              "データ提出加算"),
                    ("薬剤管理指導料",              "薬剤管理指導料"),
                    ("医療安全対策加算",            "医療安全対策加算"),
                    ("入退院支援加算",              "入退院支援加算"),
                    ("院内トリアージ実施料",        "院内トリアージ実施料"),
                    ("患者サポート体制充実加算",    "患者サポート体制充実加算"),
                    ("術後疼痛管理チーム加算",      "術後疼痛管理チーム加算"),
                    ("医療ＤＸ推進体制整備加算",    "医療ＤＸ推進体制整備加算"),
                ]),
            ]
            _sk_label_to_kw = {lb: kw for _, items in _SK_SEARCH_GROUPS for lb, kw in items}

            # 「一般病棟入院基本料」等を選んだ直後に区分（急性期一般入院料１〜等）を
            # 追加で絞り込めるようにする。地方厚生局によっては届出名称のみで区分を
            # 公表していないため、選択時は病床機能報告データを直接参照して絞り込む。
            _NYUIN_KUBUN_OPTIONS = {
                "一般病棟入院基本料": [
                    "急性期一般入院料１", "急性期一般入院料２", "急性期一般入院料３",
                    "急性期一般入院料４", "急性期一般入院料５", "急性期一般入院料６", "急性期一般入院料７",
                    "地域一般入院料１", "地域一般入院料２", "地域一般入院料３",
                    "一般病棟特別入院基本料", "特定一般病棟入院料１", "特定一般病棟入院料２",
                ],
                "療養病棟": ["療養病棟入院料１", "療養病棟入院料２", "療養病棟特別入院基本料"],
                "障害者施設等病棟": [
                    "障害者施設等７対１入院基本料", "障害者施設等10対１入院基本料",
                    "障害者施設等13対１入院基本料", "障害者施設等15対１入院基本料",
                    "障害者施設等特定入院基本料",
                ],
            }

            # 2列でグループ表示
            _kg1, _kg2 = st.columns(2)
            _s_kijun_sel: list[str] = []
            _s_kubun_sel: list[str] = []
            for _gi, (_grp_name, _grp_items) in enumerate(_SK_SEARCH_GROUPS):
                _col = _kg1 if _gi % 2 == 0 else _kg2
                with _col:
                    _sel = st.multiselect(
                        _grp_name,
                        options=[lb for lb, _ in _grp_items],
                        key=f"s_kijun_g{_gi}",
                        placeholder="選択…",
                        label_visibility="visible",
                    )
                    _s_kijun_sel.extend(_sel)
                    # 選んだ届出名称に区分の選択肢があれば、その場ですぐ下に「派生条件」
                    # として視覚的に分かるボックス（枠線＋アイコン＋補足文）で表示する。
                    # 周囲の選択肢グループと同じ見た目だと出現に気づかれないため、
                    # st.container(border=True) で明確に区別する。
                    for _sel_label in _sel:
                        _kopts = _NYUIN_KUBUN_OPTIONS.get(_sel_label)
                        if _kopts:
                            with st.container(border=True):
                                st.markdown(
                                    f"<div style='font-size:0.82rem;font-weight:600;"
                                    f"color:#b45309;margin-bottom:4px;'>"
                                    f"🔎 「{_sel_label}」を選択中 → さらに区分で絞り込めます</div>",
                                    unsafe_allow_html=True,
                                )
                                _ksel = st.multiselect(
                                    "区分（任意）",
                                    options=_kopts,
                                    key=f"s_kubun_{_sel_label}",
                                    placeholder="指定なし（すべて含む）",
                                    help="病床機能報告データから直接絞り込みます",
                                    label_visibility="collapsed",
                                )
                                _s_kubun_sel.extend(_ksel)

            st.markdown("---")
            _s_kijun_kw_text = st.text_input(
                "届出名称キーワード（部分一致・1語）",
                placeholder="例: ロボット支援下　または　ハイケアユニット",
                key="s_kijun_kw_text",
                help="受理届出名称に含まれる語句で検索（1フィールド・完全部分一致）",
            )
        else:
            _sk_label_to_kw   = {}
            _s_kijun_sel      = []
            _s_kijun_kw_text  = ""
            _s_kubun_sel      = []

    # ── フィルタリング処理 ──
    s_df = df[df["報告年度"] == s_year].copy()

    if s_pref != "全都道府県":
        s_df = s_df[s_df["都道府県名"] == s_pref]
    if s_region != "全二次医療圏":
        s_df = s_df[s_df["二次医療圏名"] == s_region]
    if s_kw:
        _norm_kw = _normalize_name(s_kw)
        s_df = s_df[s_df["医療機関名"].apply(_normalize_name).str.contains(_norm_kw, na=False)]

    # 施設基準届出フィルター
    if _sk_df_filt is not None and (_s_kijun_sel or _s_kijun_kw_text.strip()):
        _kw_list   = [_sk_label_to_kw[lb] for lb in _s_kijun_sel]
        _text_kw   = _s_kijun_kw_text.strip()
        _sk_sub    = _sk_df_filt.copy()
        if _kw_list:
            _sk_sub = _sk_sub[_sk_sub["受理届出名称"].apply(
                lambda x: any(kw in str(x) for kw in _kw_list)
            )]
        if _text_kw:
            _sk_sub = _sk_sub[_sk_sub["受理届出名称"].str.contains(_text_kw, na=False)]
        # (都道府県コード, 正規化名称) の集合を構築
        _sk_matched_set: dict[str, set[str]] = {}
        for _, _r in _sk_sub[["都道府県コード", "医療機関名_正規化"]].drop_duplicates().iterrows():
            _sk_matched_set.setdefault(_r["都道府県コード"], set()).add(_r["医療機関名_正規化"])
        _pref_name_to_code = {v: k for k, v in PREF_CODE_MAP.items()}
        def _in_sk(row):
            _c = _pref_name_to_code.get(row.get("都道府県名", ""), "")
            _n = _normalize_hospital_for_match(row.get("医療機関名", ""))
            if not _n or _c not in _sk_matched_set:
                return False
            _names = _sk_matched_set[_c]
            return _n in _names or any(sn.endswith(_n) for sn in _names)
        s_df = s_df[s_df.apply(_in_sk, axis=1)]

    # 入院基本料の区分フィルター（病床機能報告データから絞り込み。届出名称のみで
    # 区分を公表しない地方厚生局があるため、こちらは全国データで確実に絞り込める）
    if _s_kubun_sel:
        _ward_df_kubun = st.session_state.get("ward_df")
        if _ward_df_kubun is not None and not _ward_df_kubun.empty:
            _kubun_matched = _ward_df_kubun[
                (_ward_df_kubun["入院基本料"].isin(_s_kubun_sel))
                & (_ward_df_kubun["報告年度"] == s_year)
            ]
            _kubun_hosp_names = set(_kubun_matched["医療機関名"].unique())
            s_df = s_df[s_df["医療機関名"].isin(_kubun_hosp_names)]

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
        if not _sy.empty:
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
    _shiki_cols_exist = any(c in s_df.columns for c in ["ロボット支援手術数", "腹腔鏡下手術数", "胸腔鏡下手術数"])
    _any_organ_checked = any(ck for ck, _ in _organ_checks)
    _any_shiki_checked = s_ck_robot_s or s_ck_fuku or s_ck_kyou

    _surg_filter_used = _any_organ_checked or _any_shiki_checked
    _surg_no_data = _surg_state is None or (hasattr(_surg_state, "empty") and _surg_state.empty)
    _surg_no_year = (
        not _surg_no_data
        and "報告年度" in _surg_state.columns
        and (not (_surg_state["報告年度"] == s_year).any())
    )

    if _surg_filter_used and (_surg_no_data or _surg_no_year):
        _reason = f"{s_year}年度の手術データがありません" if _surg_no_year else "手術データが読み込まれていません"
        st.warning(f"⚠️ {_reason}。手術フィルターは無効です（絞り込みは行われません）。")
    elif _any_organ_checked and not _organ_cols_exist:
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
                _v = pd.to_numeric(s_df[_col], errors="coerce").fillna(0)
                _or_mask = _or_mask | (_v != 0)  # -1（マスク値）も「あり」として含む
            s_df = s_df[_or_mask]
        else:  # AND（すべて該当）
            for _, _col in _active_surg_checks:
                _v = pd.to_numeric(s_df[_col], errors="coerce").fillna(0)
                s_df = s_df[_v != 0]  # -1（マスク値）も「あり」として含む

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
    if s_has_spect and "SPECT台数" in s_df.columns:
        s_df = s_df[pd.to_numeric(s_df["SPECT台数"], errors="coerce").fillna(0) > 0]
    if s_has_robot_eq and "内視鏡手術支援機器台数" in s_df.columns:
        s_df = s_df[pd.to_numeric(s_df["内視鏡手術支援機器台数"], errors="coerce").fillna(0) > 0]
    if s_has_angio and "血管連続撮影装置台数" in s_df.columns:
        s_df = s_df[pd.to_numeric(s_df["血管連続撮影装置台数"], errors="coerce").fillna(0) > 0]
    if s_has_imrt and "IMRT台数" in s_df.columns:
        s_df = s_df[pd.to_numeric(s_df["IMRT台数"], errors="coerce").fillna(0) > 0]
    if s_has_cyberknife and "サイバーナイフ台数" in s_df.columns:
        s_df = s_df[pd.to_numeric(s_df["サイバーナイフ台数"], errors="coerce").fillna(0) > 0]
    if s_has_gamma and "ガンマナイフ台数" in s_df.columns:
        s_df = s_df[pd.to_numeric(s_df["ガンマナイフ台数"], errors="coerce").fillna(0) > 0]
    if s_has_mammo and "マンモグラフィ台数" in s_df.columns:
        s_df = s_df[pd.to_numeric(s_df["マンモグラフィ台数"], errors="coerce").fillna(0) > 0]

    # ── 所要時間フィルター ──
    _tt_applied = False
    _tt_dist_col: dict[str, float] = {}   # {医療機関名: 分}
    _tt_km_col:   dict[str, float] = {}   # {医療機関名: km}
    if s_tt_addr and (DB_PATH.exists() or _LOCS_PARQUET.exists()):
        _origin = _cached_geocode_address(s_tt_addr)
        if _origin is None:
            st.warning(f"⚠️ 出発地「{s_tt_addr}」の座標が取得できませんでした。住所をより具体的に入力してください。")
        else:
            _all_coords = load_all_hospital_coords(
                db_path=str(DB_PATH) if DB_PATH.exists() else None,
                parquet_path=str(_LOCS_PARQUET) if _LOCS_PARQUET.exists() else None,
            )
            _hosp_names = s_df["医療機関名"].tolist()
            # 正規化名でも検索（施設名と医療機関名の表記ゆれ対策）
            def _tt_lookup(name):
                return _all_coords.get(name) or _all_coords.get(_normalize_name(name))
            _known_pairs = [(n, _tt_lookup(n)) for n in _hosp_names if _tt_lookup(n)]
            _no_coord = [n for n in _hosp_names if not _tt_lookup(n)]
            if _no_coord:
                st.caption(f"ℹ️ 座標未取得のため除外対象外: {len(_no_coord)}病院")

            if _known_pairs:
                _dests = [coords for _, coords in _known_pairs]
                _max_sec = s_tt_max * 60

                if s_tt_mode == "車（OSRM）":
                    with st.spinner("OSRM で所要時間を計算中..."):
                        _durations = osrm_durations(_origin[0], _origin[1], _dests)
                    _transit_note = False
                else:
                    _speed_kmph = 25.0
                    _durations = [
                        haversine_km(_origin[0], _origin[1], lat, lon) / _speed_kmph * 3600
                        for lat, lon in _dests
                    ]
                    _transit_note = True

                _keep_names: set[str] = set()
                for (name, (lat, lon)), dur in zip(_known_pairs, _durations):
                    km = haversine_km(_origin[0], _origin[1], lat, lon)
                    _tt_km_col[name] = round(km, 1)
                    if dur is not None:
                        mins = round(dur / 60, 1)
                        _tt_dist_col[name] = mins
                        if dur <= _max_sec:
                            _keep_names.add(name)
                    else:
                        _tt_dist_col[name] = None

                s_df = s_df[s_df["医療機関名"].isin(_keep_names)]
                _tt_applied = True

                if _transit_note:
                    st.caption("※ 公共交通は直線距離÷25km/hの近似値です")

    # ── 表示列の決定 ──
    _base = ["医療機関名", "都道府県名", "二次医療圏名", "合計_許可病床数"]
    if "合計稼働率" in s_df.columns:
        _base.append("合計稼働率")
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
    if s_has_spect and "SPECT台数" in s_df.columns:
        _eshow.append("SPECT台数")
    if s_has_robot_eq and "内視鏡手術支援機器台数" in s_df.columns:
        _eshow.append("内視鏡手術支援機器台数")
    if s_has_angio and "血管連続撮影装置台数" in s_df.columns:
        _eshow.append("血管連続撮影装置台数")
    if s_has_imrt and "IMRT台数" in s_df.columns:
        _eshow.append("IMRT台数")
    if s_has_cyberknife and "サイバーナイフ台数" in s_df.columns:
        _eshow.append("サイバーナイフ台数")
    if s_has_gamma and "ガンマナイフ台数" in s_df.columns:
        _eshow.append("ガンマナイフ台数")
    if s_has_mammo and "マンモグラフィ台数" in s_df.columns:
        _eshow.append("マンモグラフィ台数")
    _disp = _base + _sshow + _organ_show + _eshow

    result_s = (
        s_df[_disp]
        .sort_values("合計_許可病床数", ascending=False)
        .reset_index(drop=True)
    )

    # 所要時間列を追加（フィルター適用時）
    if _tt_applied:
        result_s["直線距離(km)"] = result_s["医療機関名"].map(_tt_km_col)
        result_s["所要時間(分)"] = result_s["医療機関名"].map(_tt_dist_col)
        result_s = result_s.sort_values("所要時間(分)").reset_index(drop=True)

    # ── 結果表示 ──
    st.markdown("<div style='margin-top:28px;'></div>", unsafe_allow_html=True)
    st.markdown(
        "<div style='border-left:4px solid #10b981;padding:8px 14px;background:#ecfdf5;"
        "border-radius:0 6px 6px 0;margin-bottom:12px;'>"
        "<span style='font-weight:700;font-size:1rem;color:#065f46;'>③ 検索結果</span>"
        "</div>",
        unsafe_allow_html=True,
    )
    st.markdown(_source_tag(_byosho_source(s_year)), unsafe_allow_html=True)
    st.markdown(f"**{len(result_s):,} 件の病院が見つかりました**")

    _col_cfg = {
        "合計_許可病床数":  st.column_config.NumberColumn("許可病床数（床）", format="%,d 床"),
        "合計稼働率":       st.column_config.ProgressColumn("稼働率", format="%.1f%%", min_value=0, max_value=100),
        "CT_64列以上":      st.column_config.NumberColumn("CT 64列以上",      format="%,d 台"),
        "CT_16〜64列":      st.column_config.NumberColumn("CT 16〜64列",      format="%,d 台"),
        "CT_16列未満":      st.column_config.NumberColumn("CT 16列未満",      format="%,d 台"),
        "MRI_3T以上":       st.column_config.NumberColumn("MRI 3T以上",       format="%,d 台"),
        "MRI_1.5〜3T":      st.column_config.NumberColumn("MRI 1.5〜3T",      format="%,d 台"),
        "MRI_1.5T未満":     st.column_config.NumberColumn("MRI 1.5T未満",     format="%,d 台"),
        "内視鏡手術支援機器台数": st.column_config.NumberColumn("手術支援ロボット", format="%,d 台"),
    }
    for _c in _sshow:
        _col_cfg[_c] = st.column_config.TextColumn()
    for _c in _organ_show:
        _label = _c.replace("手術_", "").replace("全麻_", "全麻:")
        _col_cfg[_c] = st.column_config.TextColumn(_label)
    for _c in _eshow:
        if _c not in _col_cfg:
            _col_cfg[_c] = st.column_config.NumberColumn(format="%,d 台")
    if _tt_applied:
        _col_cfg["直線距離(km)"] = st.column_config.NumberColumn("直線距離", format="%.1f km")
        _col_cfg["所要時間(分)"] = st.column_config.NumberColumn("所要時間", format="%.1f 分")

    # -1（マスク値）を表示用に "*" へ変換（手術列のみ）
    result_s_disp = result_s.copy()
    for _c in _sshow + _organ_show:
        if _c in result_s_disp.columns:
            _v = pd.to_numeric(result_s_disp[_c], errors="coerce").fillna(0)
            result_s_disp[_c] = _v.apply(
                lambda x: "*" if x == -1 else (f"{int(x):,}" if x > 0 else "0")
            )

    st.dataframe(result_s_disp, hide_index=True, use_container_width=True, column_config=_col_cfg)

    # CSVダウンロード（CSV も -1 → "*"）
    _csv_df = result_s.copy()
    for _c in _sshow + _organ_show:
        if _c in _csv_df.columns:
            _v = pd.to_numeric(_csv_df[_c], errors="coerce").fillna(0)
            _csv_df[_c] = _v.apply(lambda x: "*" if x == -1 else int(x))
    st.download_button(
        "📥 CSV ダウンロード",
        _csv_df.to_csv(index=False).encode("utf-8-sig"),
        file_name=f"hospital_search_{s_year}.csv",
        mime="text/csv",
        key="s_csv_dl",
    )


    # 検索モードはここで終了
    _render_footer()
    st.stop()


# ══════════════════════════════════════════════════════════
# 地域医療構想分析モード
# ══════════════════════════════════════════════════════════

if st.session_state.get("_view_mode") == "region_vision":
    import plotly.graph_objects as _go_rv

    # セレクター UI（サイドバー廃止により、メインエリアに移動）
    with st.container(border=True, key="rv_filter_box"):
        _rv_sel_c1, _rv_sel_c2, _rv_sel_c3, _rv_sel_c4 = st.columns([2, 2, 3, 2])
        with _rv_sel_c1:
            _rv_years_list = [int(y) for y in sorted(_df_all["報告年度"].dropna().unique(), reverse=True)]
            if st.session_state.get("_rv_sel_year") not in _rv_years_list:
                st.session_state["_rv_sel_year"] = _rv_years_list[0] if _rv_years_list else 2023
            _rv_year = st.selectbox("📅 分析年度", _rv_years_list, key="_rv_sel_year")
        with _rv_sel_c2:
            _rv_all_prefs = _sort_prefs(_df_all["都道府県名"].unique())
            if st.session_state.get("_rv_sel_pref") not in _rv_all_prefs:
                st.session_state["_rv_sel_pref"] = _rv_all_prefs[0] if _rv_all_prefs else None
            _rv_pref = st.selectbox("🗾 都道府県", _rv_all_prefs, key="_rv_sel_pref")
        with _rv_sel_c3:
            _rv_regions_list = sorted(
                r for r in _df_all[_df_all["都道府県名"] == _rv_pref]["二次医療圏名"].unique()
                if r != "不明"
            )
            if st.session_state.get("_rv_sel_region") not in _rv_regions_list:
                st.session_state["_rv_sel_region"] = _rv_regions_list[0] if _rv_regions_list else None
            _rv_region = st.selectbox("🏘️ 二次医療圏", _rv_regions_list, key="_rv_sel_region")
        with _rv_sel_c4:
            st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
            if st.button("← ホームに戻る", key="_rv_back_btn", use_container_width=True):
                st.session_state["_view_mode"] = "home"
                st.rerun()

    # ── ヘッダー
    st.markdown(f"## {_rv_region} 地域医療構想分析")
    st.caption(
        f"データ出典：{_byosho_source(_rv_year)}　|　{_rv_pref}　{_rv_region}　"
        "※ 本分析は病床機能報告データに基づく参考情報です。"
        "実際の構想策定には一次データ・専門家の関与が必要です。"
    )

    # ── 地域内全病院データ
    rv_df = df[
        (df["報告年度"] == _rv_year) &
        (df["都道府県名"] == _rv_pref) &
        (df["二次医療圏名"] == _rv_region)
    ].copy().reset_index(drop=True)

    if rv_df.empty:
        st.warning("選択した二次医療圏のデータが見つかりません。上の検索から二次医療圏を選択してください。")
        _render_footer()
        st.stop()

    # 手術データマップ {医療機関名: 手術総数 / ロボット手術数}
    _surg_df_rv = st.session_state.get("surgery_df")
    _surg_map_rv   = {}   # 医療機関名 → 手術総数
    _robot_map_rv  = {}   # 医療機関名 → ロボット支援手術数
    _zenmac_map_rv = {}   # 医療機関名 → 全身麻酔手術数
    if _surg_df_rv is not None and not _surg_df_rv.empty:
        _rv_smask = pd.Series(True, index=_surg_df_rv.index)
        if "二次医療圏名" in _surg_df_rv.columns:
            _rv_smask = _rv_smask & (_surg_df_rv["二次医療圏名"] == _rv_region)
        if "報告年度" in _surg_df_rv.columns:
            _rv_smask = _rv_smask & (_surg_df_rv["報告年度"] == _rv_year)
        for _, _sr in _surg_df_rv[_rv_smask].iterrows():
            _hn = str(_sr.get("医療機関名", ""))
            _surg_map_rv[_hn]   = _si(_sr.get("手術総数", 0))
            _robot_map_rv[_hn]  = _si(_sr.get("ロボット支援手術数", 0))
            _zenmac_map_rv[_hn] = _si(_sr.get("全身麻酔手術数", 0))

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
        """機能方向性を多変量フィットスコアで分類しラベルとコメントを返す"""
        beds    = _si(row.get("合計_許可病床数", 0))
        koudo   = _si(row.get("高度急性期_許可病床数", 0))
        kyusei  = _si(row.get("急性期_許可病床数", 0))
        kaifuku = _si(row.get("回復期_許可病床数", 0))
        mansei  = _si(row.get("慢性期_許可病床数", 0))
        docs    = _si(row.get("常勤医師数", 0))
        nurses  = _si(row.get("常勤看護師数", 0))
        emg     = _si(row.get("救急搬送件数", 0))
        hn      = str(row.get("医療機関名", ""))

        if beds == 0:
            return "⚪ データ不足", "許可病床数データがありません。"

        surg_cnt = _surg_map_rv.get(hn, 0)
        robot    = _robot_map_rv.get(hn, 0)
        zenmac   = _zenmac_map_rv.get(hn, 0)

        acute_r    = (koudo + kyusei) / beds
        recovery_r = kaifuku / beds
        chronic_r  = mansei / beds
        doc100     = docs / beds * 100 if beds > 0 else 0
        nurse100   = nurses / beds * 100 if beds > 0 else 0

        # 稼働率（合計稼働率列 or 在棟延べ数から計算）
        occ = None
        _occ_raw = row.get("合計稼働率")
        if pd.notna(_occ_raw) and float(_occ_raw or 0) > 0:
            occ = float(_occ_raw)
        else:
            stay = _si(row.get("合計_在棟延べ数", 0))
            if stay > 0:
                occ = min(150.0, stay / (beds * 365) * 100)

        # ── 各役割への適合度スコア（0〜100点） ──────────────
        _top_n = max(1, min(3, n_total // 4 + 1))

        # ① 急性期拠点候補
        f_kyoten = 0
        if rank <= _top_n:            f_kyoten += 35
        elif rank <= _top_n + 1:      f_kyoten += 15
        f_kyoten += min(25, score * 25 // 55)      # 急性期拠点スコア反映
        if acute_r >= 0.50:           f_kyoten += 10
        if surg_cnt >= 2000:          f_kyoten += 10
        elif surg_cnt >= 1000:        f_kyoten += 5
        if zenmac >= 800:             f_kyoten += 5
        if emg >= 2000:               f_kyoten += 10
        elif emg >= 1000:             f_kyoten += 5
        if robot > 0:                 f_kyoten += 5
        if occ and occ >= 80:         f_kyoten += 5

        # ② 地域急性期
        f_acute = 0
        if acute_r >= 0.65:           f_acute += 45
        elif acute_r >= 0.50:         f_acute += 35
        elif acute_r >= 0.40:         f_acute += 20
        elif acute_r >= 0.30:         f_acute += 8
        if beds >= 300:               f_acute += 20
        elif beds >= 200:             f_acute += 15
        elif beds >= 150:             f_acute += 10
        elif beds >= 100:             f_acute += 5
        if surg_cnt >= 1000:          f_acute += 10
        elif surg_cnt >= 500:         f_acute += 6
        elif surg_cnt >= 200:         f_acute += 2
        if emg >= 1000:               f_acute += 10
        elif emg >= 500:              f_acute += 6
        elif emg >= 200:              f_acute += 2
        if occ and occ >= 75:         f_acute += 5
        if doc100 >= 8:               f_acute += 5
        # 拠点候補と重複しないよう減点
        if rank <= _top_n and score >= 38:
            f_acute = max(0, f_acute - 20)

        # ③ 高齢者救急（急性期＋回復期混合、高齢者軽〜中等症対応）
        f_elderly = 0
        if 0.20 <= acute_r <= 0.55:   f_elderly += 30
        elif 0 < acute_r < 0.20:      f_elderly += 10
        if recovery_r >= 0.25:        f_elderly += 25
        elif recovery_r >= 0.15:      f_elderly += 15
        elif recovery_r >= 0.05:      f_elderly += 5
        if emg > 0:                   f_elderly += 10
        if beds < 300:                f_elderly += 10
        if occ and 65 <= occ <= 93:   f_elderly += 5
        # 急性期比率が高く大規模なら地域急性期向き
        if acute_r >= 0.60 and beds >= 200:
            f_elderly = max(0, f_elderly - 20)

        # ④ 回復期強化
        f_recovery = 0
        if recovery_r >= 0.55:        f_recovery += 55
        elif recovery_r >= 0.45:      f_recovery += 45
        elif recovery_r >= 0.35:      f_recovery += 30
        elif recovery_r >= 0.25:      f_recovery += 15
        elif recovery_r >= 0.15:      f_recovery += 5
        if acute_r <= 0.20:           f_recovery += 15
        if chronic_r <= 0.15:         f_recovery += 5
        if occ and occ >= 85:         f_recovery += 15
        elif occ and occ >= 75:       f_recovery += 7
        if nurse100 >= 10:            f_recovery += 5
        if beds >= 80:                f_recovery += 5

        # ⑤ 慢性期・在宅支援
        f_chronic = 0
        if chronic_r >= 0.55:         f_chronic += 55
        elif chronic_r >= 0.45:       f_chronic += 45
        elif chronic_r >= 0.35:       f_chronic += 30
        elif chronic_r >= 0.25:       f_chronic += 15
        elif chronic_r >= 0.15:       f_chronic += 5
        if acute_r <= 0.15:           f_chronic += 15
        if recovery_r <= 0.20:        f_chronic += 5
        if occ and occ >= 88:         f_chronic += 15
        elif occ and occ >= 78:       f_chronic += 7
        if nurse100 >= 8:             f_chronic += 5

        # ⑥ 専門・外来特化
        f_small = 0
        if beds < 50:                 f_small += 60
        elif beds < 80:               f_small += 40
        elif beds < 100:              f_small += 20
        elif beds < 130:              f_small += 5
        if acute_r < 0.25 and recovery_r < 0.25 and chronic_r < 0.25:
            f_small += 15

        fits = {
            "kyoten":   f_kyoten,
            "acute":    f_acute,
            "elderly":  f_elderly,
            "recovery": f_recovery,
            "chronic":  f_chronic,
            "small":    f_small,
        }
        priority = ["kyoten", "acute", "elderly", "recovery", "chronic", "small"]
        best = max(priority, key=lambda k: (fits[k], -priority.index(k)))

        # ── コメント用ヘルパー ───────────────────────────────
        def _fmt(*parts):
            return "".join(p for p in parts if p)

        occ_s  = f"稼働率{occ:.0f}%" if occ else ""
        emg_s  = f"救急搬送{emg:,}件/年" if emg > 0 else ""
        surg_s = f"手術{surg_cnt:,}件/年（うち全身麻酔{zenmac:,}件）" if surg_cnt > 0 and zenmac > 0 else \
                 f"手術{surg_cnt:,}件/年" if surg_cnt > 0 else ""
        nrs_s  = f"看護師{nurses}人（{nurse100:.0f}人/100床）" if nurses > 0 else ""

        # ── ラベル・コメント出力 ────────────────────────────
        MIN_FIT = 20

        if best == "kyoten" and fits[best] >= MIN_FIT:
            _robot_s = f"ロボット支援手術{robot}件を含む高度手術実績があり、" if robot > 0 else ""
            _doc_s   = f"常勤医師{docs}人（{doc100:.0f}人/100床）" if docs > 0 else ""
            return (
                "🏆 急性期拠点候補",
                _fmt(
                    f"急性期拠点機能スコア{score}点（地域{rank}位）。",
                    f"急性期系病床{acute_r*100:.0f}%・{_doc_s}。",
                    f"{_robot_s}地域の急性期医療を集約的に担う中核病院として有力。",
                    f"　{emg_s}{'、' if emg_s and surg_s else ''}{surg_s}{'、' if surg_s and occ_s else ''}{occ_s}。" if any([emg_s, surg_s, occ_s]) else "",
                )
            )

        if best == "acute" and fits[best] >= MIN_FIT:
            return (
                "🔴 地域急性期",
                _fmt(
                    f"急性期系病床{acute_r*100:.0f}%（{int(beds*acute_r):,}床）・{beds}床規模。",
                    f"地域の急性期需要を幅広く担い、急性期拠点病院との役割分担・連携強化が課題。",
                    f"　{emg_s}{'、' if emg_s and surg_s else ''}{surg_s}{'、' if surg_s and occ_s else ''}{occ_s}。" if any([emg_s, surg_s, occ_s]) else "",
                )
            )

        if best == "elderly" and fits[best] >= MIN_FIT:
            return (
                "🚑 高齢者救急",
                _fmt(
                    f"急性期系{acute_r*100:.0f}%・回復期{recovery_r*100:.0f}%の混合構成（{beds}床）。",
                    f"高齢者の軽〜中等症急性期入院と在宅・施設への後方支援を担うポジション。",
                    f"2040年に向け高齢者救急需要の増大が見込まれ、受け入れ体制整備が重要。",
                    f"　{emg_s}{'、' if emg_s and occ_s else ''}{occ_s}。" if any([emg_s, occ_s]) else "",
                )
            )

        if best == "recovery" and fits[best] >= MIN_FIT:
            _occ_note = f"稼働率{occ:.0f}%と高需要。" if occ and occ >= 85 else ""
            return (
                "🔄 回復期強化",
                _fmt(
                    f"回復期病床{recovery_r*100:.0f}%（{int(beds*recovery_r):,}床）。",
                    f"{_occ_note}",
                    f"2040年に向け高齢者リハビリ需要が大幅増大するなか、",
                    f"回復期・地域包括ケア病棟機能のさらなる拡充が期待される。",
                    f"　{nrs_s}{'。' if nrs_s else ''}",
                )
            )

        if best == "chronic" and fits[best] >= MIN_FIT:
            _occ_note = f"稼働率{occ:.0f}%と高稼働を維持。" if occ and occ >= 88 else ""
            return (
                "💊 慢性期・在宅支援",
                _fmt(
                    f"慢性期病床{chronic_r*100:.0f}%（{int(beds*chronic_r):,}床）。",
                    f"{_occ_note}",
                    f"高齢化に伴う療養需要に対応しつつ、",
                    f"在宅療養支援・看取り機能の充実が2040年に向けた重要課題。",
                    f"　{nrs_s}{'。' if nrs_s else ''}",
                )
            )

        if best == "small" and fits[best] >= MIN_FIT:
            return (
                "🏠 専門・外来特化",
                _fmt(
                    f"許可病床{beds}床の小規模医療機関。",
                    f"外来・専門診療への特化、または在宅療養支援・後方ベッドとしての",
                    f"役割強化が地域内機能分担として有効。",
                    f"　{occ_s}{'。' if occ_s else ''}",
                )
            )

        return (
            "⚪ 機能転換検討中",
            _fmt(
                f"急性期{acute_r*100:.0f}%・回復期{recovery_r*100:.0f}%・慢性期{chronic_r*100:.0f}%（{beds}床）。",
                f"いずれの機能にも明確な特化が見られない。",
                f"地域医療構想調整会議での機能選択・役割分担の議論が重要。",
                f"　{emg_s}{'、' if emg_s and occ_s else ''}{occ_s}{'。' if any([emg_s, occ_s]) else ''}",
            )
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

    # ── AI短評生成（ANTHROPIC_API_KEY が設定されている場合） ──────
    _ant_key = (st.secrets.get("ANTHROPIC_API_KEY", "") if hasattr(st, "secrets") else "") \
               or os.environ.get("ANTHROPIC_API_KEY", "")
    _rv_ai_results: dict = {}
    if _ant_key:
        _rv_total_beds = int(rv_df["合計_許可病床数"].fillna(0).sum())
        def _rv_avg(col):
            v = pd.to_numeric(rv_df[col], errors="coerce")
            return float(v.mean()) if v.notna().any() else 0.0
        _avg_koudo_r  = _rv_avg("高度急性期_許可病床数") / max(1, _rv_avg("合計_許可病床数")) * 100
        _avg_kyusei_r = _rv_avg("急性期_許可病床数")    / max(1, _rv_avg("合計_許可病床数")) * 100
        _avg_kaifu_r  = _rv_avg("回復期_許可病床数")    / max(1, _rv_avg("合計_許可病床数")) * 100
        _avg_mansei_r = _rv_avg("慢性期_許可病床数")    / max(1, _rv_avg("合計_許可病床数")) * 100
        _avg_doc100_rv = (_rv_avg("常勤医師数") / max(1, _rv_avg("合計_許可病床数"))) * 100
        _avg_emg_rv    = float(pd.to_numeric(rv_df.get("救急搬送件数", pd.Series()), errors="coerce").mean() or 0)

        def _build_rv_prompt(rr):
            beds  = _si(rr.get("合計_許可病床数", 0))
            docs  = _si(rr.get("常勤医師数", 0))
            nrs   = _si(rr.get("常勤看護師数", 0))
            emg   = _si(rr.get("救急搬送件数", 0))
            hn    = str(rr.get("医療機関名", ""))
            rnk   = int(rr.get("_rank", 0))
            role  = str(rr.get("_role", ""))
            scr   = int(rr.get("_score", 0))
            koudo = _si(rr.get("高度急性期_許可病床数", 0))
            kyusei= _si(rr.get("急性期_許可病床数", 0))
            kaif  = _si(rr.get("回復期_許可病床数", 0))
            mans  = _si(rr.get("慢性期_許可病床数", 0))
            surg  = _surg_map_rv.get(hn, 0)
            zmac  = _zenmac_map_rv.get(hn, 0)
            robo  = _robot_map_rv.get(hn, 0)
            doc100= docs / beds * 100 if beds else 0
            occ_v = None
            _ov = rr.get("合計稼働率")
            if pd.notna(_ov) and float(_ov or 0) > 0:
                occ_v = float(_ov)
            else:
                stay = _si(rr.get("合計_在棟延べ数", 0))
                if stay > 0:
                    occ_v = min(150.0, stay / (beds * 365) * 100)
            occ_s  = f"{occ_v:.0f}%" if occ_v else "不明"
            robo_s = f"あり（{robo}件）" if robo > 0 else "なし"
            krate  = (koudo + kyusei) / beds * 100 if beds else 0
            return f"""あなたは地域医療構想の専門アナリストです。
以下のデータをもとに、この病院の短評を60〜100文字の日本語で書いてください。

要件：
- 地域平均との差異・病院固有の特徴・数値の組み合わせから読み取れる現況を指摘すること
- 「〜が重要」「〜が期待される」など抽象的な結論だけで終わらないこと
- 短評のみ返すこと（見出し・前置き不要）

【地域：{_rv_region}（{_rv_year}年度・全{_n_hosp_rv}院）】
合計許可病床 {_rv_total_beds:,}床
地域平均病床構成: 高度急性期{_avg_koudo_r:.0f}% 急性期{_avg_kyusei_r:.0f}% 回復期{_avg_kaifu_r:.0f}% 慢性期{_avg_mansei_r:.0f}%
地域平均医師密度: {_avg_doc100_rv:.1f}人/100床
地域平均救急搬送: {_avg_emg_rv:.0f}件/年

【病院：{hn}（急性期拠点スコア{scr}点・地域{rnk}位）】
許可病床: {beds}床
病床構成: 高度急性期{koudo}床({koudo/beds*100 if beds else 0:.0f}%) 急性期{kyusei}床 回復期{kaif}床 慢性期{mans}床
急性期系合計: {krate:.0f}%（地域平均{_avg_koudo_r+_avg_kyusei_r:.0f}%）
医師: {docs}人（{doc100:.1f}人/100床、地域平均比{doc100-_avg_doc100_rv:+.1f}）
看護師: {nrs}人
救急搬送: {emg}件/年（地域平均比{emg-_avg_emg_rv:+.0f}件）
手術総数: {surg}件（うち全身麻酔{zmac}件）
稼働率: {occ_s}
ロボット支援手術: {robo_s}
機能分類: {role}"""

        # プロンプトを組み立ててサーバー共有キャッシュ関数に渡す
        import json as _json
        _prompt_records = [
            {"hn": rr["医療機関名"], "prompt": _build_rv_prompt(rr)}
            for _, rr in rv_df.iterrows()
        ]
        _records_json = _json.dumps(_prompt_records, ensure_ascii=False)
        try:
            with st.spinner(f"AI短評を生成中… {len(_prompt_records)}病院"):
                _rv_ai_results = _gen_rv_ai_comments(_records_json, _ant_key)
        except Exception:
            pass

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
        f"{_n_hosp_rv:,} 病院",
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
        + (f"（{_rv_docs_reported:,}/{_n_hosp_rv:,}病院が報告）" if _rv_docs_reported < _n_hosp_rv else f"（{_rv_docs_reported:,}病院）")
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
            dragmode=False,
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
            "許可病床数":     st.column_config.NumberColumn("許可病床", format="%,d 床"),
            "高度急性期床":   st.column_config.NumberColumn("高度急性期", format="%,d 床"),
            "手術総数":       st.column_config.NumberColumn("手術", format="%,d 件"),
            "常勤医師数":     st.column_config.NumberColumn("医師", format="%,d 人"),
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
.role-def-table { width:100%; border-collapse:collapse; font-size:0.92rem; }
.role-def-table th {
    background:#f0f2f6; padding:7px 10px; text-align:left;
    border-bottom:2px solid #d0d3db; font-size:0.88rem; color:#444;
}
.role-def-table td { padding:7px 10px; border-bottom:1px solid #e8e8e8; vertical-align:top; }
.role-def-table tr:last-child td { border-bottom:none; }
.role-badge {
    display:inline-block; padding:2px 8px; border-radius:10px;
    font-weight:600; font-size:0.88rem; white-space:nowrap;
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
<p style="font-size:0.82rem;color:#999;margin-top:8px;">
※ 判定基準は病床機能報告の報告値のみを用いた参考分類です。実際の機能定義は都道府県の地域医療構想に基づきます。
</p>
        """, unsafe_allow_html=True)

    _ai_badge = (
        ' <span style="font-size:0.65rem;color:#7c3aed;background:#f5f3ff;'
        'padding:1px 5px;border-radius:3px;vertical-align:middle;">AI</span>'
        if _ant_key else ""
    )
    for _, _rr in rv_df.iterrows():
        _hn_r   = _rr["医療機関名"]
        _role_r = _rr["_role"]
        _ai_txt = _rv_ai_results.get(_hn_r)
        _comm_r = _ai_txt if _ai_txt else _rr["_comment"]
        _is_ai  = bool(_ai_txt)
        _scr_r  = int(_rr["_score"])
        _beds_r = _si(_rr.get("合計_許可病床数", 0))
        _surg_r = _surg_map_rv.get(_hn_r, 0)
        _bc_r   = _rv_role_colors.get(_role_r, "#bdc3c7")

        _surg_txt = f" | 手術 {_surg_r:,}件/年" if _surg_r > 0 else ""
        _docs_r   = _si(_rr.get("常勤医師数", 0))
        _docs_txt = f" | 医師 {_docs_r:,}人" if _docs_r > 0 else ""

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
          <div style="font-size:0.8rem; color:#555; line-height:1.6;">
            {_comm_r}{_ai_badge if _is_ai else ""}
          </div>
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
                "現状（床）":      st.column_config.NumberColumn(format="%,d 床"),
                "2040年試算（床）": st.column_config.NumberColumn(format="%,d 床"),
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
            dragmode=False,
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
    _render_footer()
    st.stop()


# ══════════════════════════════════════════════════════════
# DPC疾患別検索モード
# ══════════════════════════════════════════════════════════

if st.session_state.get("_view_mode") == "dpc_search":
    _dsc1, _dsc2 = st.columns([8, 2])
    with _dsc1:
        st.markdown("## DPC疾患別 病院検索")
        st.caption("疾患（DPC6桁）ごとに全国・都道府県・二次医療圏の病院を件数順に比較します")
    with _dsc2:
        if st.button("← ホームに戻る", use_container_width=True, key="_dsc_back"):
            st.session_state["_view_mode"] = "home"
            st.rerun()

    _ds_surg_all = _load_dpc_surgery_detail(_DPC_SURG_MTIME)
    if _ds_surg_all is None:
        st.warning("DPCデータが読み込まれていません")
        _render_footer()
        st.stop()

    _ds_hosp_info = _build_dpc_hosp_info()

    # ── フィルター行1: MDC + 疾患名 + ランキング指標 + 手術有無 ──
    _dsf1, _dsf2, _dsf3, _dsf3b = st.columns([2, 4, 2, 2])
    with _dsf1:
        _ds_mdc_present = set(_ds_surg_all["MDC"].dropna().unique())
        _ds_mdc_opts = ["すべて"] + [
            f"{k}　{v}" for k, v in MDC_LABELS.items() if k in _ds_mdc_present
        ]
        if st.session_state.get("_dsc_mdc") not in _ds_mdc_opts:
            st.session_state["_dsc_mdc"] = _ds_mdc_opts[0]
        _ds_mdc_sel  = st.selectbox("MDC（診断群分類）", _ds_mdc_opts, key="_dsc_mdc")

    with _dsf2:
        _ds_mdc_key = _ds_mdc_sel[:5].strip() if _ds_mdc_sel != "すべて" else None
        _ds_disease_src = (
            _ds_surg_all if _ds_mdc_key is None
            else _ds_surg_all[_ds_surg_all["MDC"] == _ds_mdc_key]
        )
        _ds_disease_mask = _ds_disease_src["件数_総計"].fillna(0) != 0
        if "件数_手術有" in _ds_disease_src.columns:
            _ds_disease_mask = _ds_disease_mask | (_ds_disease_src["件数_手術有"].fillna(0) != 0)
        _ds_disease_src = _ds_disease_src[_ds_disease_mask]
        _ds_diseases = sorted(_ds_disease_src["疾患名"].dropna().unique().tolist())
        # セッションステート検証（MDC変更時に古い疾患名が残らないように）
        if st.session_state.get("_dsc_disease") not in _ds_diseases:
            st.session_state["_dsc_disease"] = _ds_diseases[0] if _ds_diseases else None
        _ds_disease = st.selectbox("疾患名", _ds_diseases, key="_dsc_disease")

    with _dsf3:
        _ds_metric = st.selectbox("ランキング指標", ["患者総数", "平均在院日数", "医療圏シェア"], key="_dsc_metric")

    with _dsf3b:
        _ds_surg_sel = st.radio("手術有無", ["すべて", "手術あり", "手術なし"], horizontal=False, key="_dsc_surg")

    # ── フィルター行2: 都道府県 + 病院区分 ──
    _dsf4, _dsf5 = st.columns([3, 5])
    with _dsf4:
        _ds_geo_scope = st.radio("地域絞り込み", ["全国", "都道府県", "二次医療圏"], horizontal=True, key="_dsc_scope")
        _ds_pref_sel = None
        _ds_region_sel = None
        if _ds_geo_scope in ("都道府県", "二次医療圏"):
            _ds_all_prefs = sorted(_ds_hosp_info["都道府県名"].dropna().unique().tolist())
            _ds_pref_sel  = st.selectbox("都道府県を選択", _ds_all_prefs, key="_dsc_pref")
            if _ds_geo_scope == "二次医療圏":
                _ds_all_regions = sorted(
                    r for r in _ds_hosp_info[_ds_hosp_info["都道府県名"] == _ds_pref_sel]["二次医療圏名"].unique()
                    if r
                )
                if _ds_all_regions:
                    _ds_region_sel = st.selectbox("二次医療圏を選択", _ds_all_regions, key="_dsc_region")
                else:
                    st.caption("この都道府県はDPCと病床機能報告の突合データがありません")

    with _dsf5:
        _ds_all_kubun = ["DPC算定病院", "DPC準備病院", "出来高算定病院"]
        # 旧デフォルト（出来高除外）のセッションを更新
        if st.session_state.get("_dsc_kubun") == ["DPC算定病院", "DPC準備病院"]:
            st.session_state["_dsc_kubun"] = _ds_all_kubun
        _ds_kubun_sel = st.multiselect(
            "病院区分",
            _ds_all_kubun,
            default=_ds_all_kubun,
            key="_dsc_kubun",
        )
        _ds_hide_nan = st.checkbox("非公表（10例未満等）の病院を除く", value=True, key="_dsc_hide_nan")

    st.markdown("---")

    # ── 検索・集計 ──
    _ds_has_surg_detail = "件数_手術有" in _ds_surg_all.columns
    _ds_cnt_col_base = next((c for c in _ds_surg_all.columns if "件数" in c and "総計" in c), None)
    _ds_los_col_base = next((c for c in _ds_surg_all.columns if "在院" in c and "総計" in c), None)

    # 件数_総計 = Excelコード99 = 手術なし件数（真の総計ではない）
    # 件数_手術有 = Excelコード97 = 手術あり件数
    # 真の総計 = 件数_総計(code99) + 件数_手術有(code97)
    if _ds_surg_sel == "手術あり" and _ds_has_surg_detail:
        _ds_cnt_col = "件数_手術有"
        _ds_los_col = "在院日数_手術有" if "在院日数_手術有" in _ds_surg_all.columns else _ds_los_col_base
    elif _ds_surg_sel == "手術なし":
        _ds_cnt_col = _ds_cnt_col_base  # 件数_総計 = code99 = 手術なし
        _ds_los_col = _ds_los_col_base
    else:  # すべて
        _ds_cnt_col = "_ds_cnt_total"
        _ds_los_col = _ds_los_col_base

    if _ds_cnt_col_base and _ds_disease:
        # 疾患でフィルター（最新年度のみ）
        _ds_filtered = _ds_surg_all[_ds_surg_all["疾患名"] == _ds_disease].copy()
        if "年度" in _ds_filtered.columns:
            _ds_filtered = _ds_filtered[_ds_filtered["年度"] == _ds_filtered["年度"].max()]

        # すべて: 真の総計 = 手術なし(code99) + 手術あり(code97)
        if _ds_surg_sel == "すべて":
            _ds_filtered["_ds_cnt_total"] = (
                _ds_filtered["件数_総計"].fillna(0)
                + (_ds_filtered["件数_手術有"].fillna(0) if _ds_has_surg_detail else 0)
            )

        # 手術有無データがない疾患（MDC06以外）の場合は警告
        if _ds_surg_sel != "すべて" and _ds_has_surg_detail:
            _surg_avail = _ds_filtered["件数_手術有"].notna().any() if "件数_手術有" in _ds_filtered.columns else False
            if not _surg_avail:
                st.info("この疾患の手術有無別データは公表されていません。「すべて」に切り替えると患者総数で比較できます。")

        # 告示番号単位で集計
        _ds_agg: dict = {_ds_cnt_col: "sum", "施設名": "first"}
        if _ds_los_col and _ds_los_col in _ds_filtered.columns:
            _ds_agg[_ds_los_col] = "first"
        _ds_result = _ds_filtered.groupby("告示番号", as_index=False).agg(_ds_agg)

        # 病院情報（病院区分・都道府県名）をJOIN
        if not _ds_hosp_info.empty:
            _ds_result = _ds_result.merge(_ds_hosp_info, on="告示番号", how="left")
        else:
            _ds_result["病院区分"] = ""
            _ds_result["都道府県名"] = ""

        # 病院区分フィルター
        if _ds_kubun_sel:
            _ds_result = _ds_result[_ds_result["病院区分"].isin(_ds_kubun_sel)]

        # 医療圏シェア: 同一二次医療圏内でこの病院の件数が占める割合
        # （地域絞り込みの前、病院区分フィルター後の母集団で計算する）
        if "二次医療圏名" in _ds_result.columns:
            _share_base = _ds_result[
                (_ds_result["二次医療圏名"] != "") & (_ds_result[_ds_cnt_col] >= 0)
            ]
            _region_totals = _share_base.groupby("二次医療圏名")[_ds_cnt_col].sum()

            def _calc_share(row, _totals=_region_totals):
                reg = row.get("二次医療圏名", "")
                cnt = row.get(_ds_cnt_col)
                if not reg or pd.isna(cnt) or cnt < 0:
                    return np.nan
                tot = _totals.get(reg, 0)
                return round(cnt / tot * 100, 1) if tot > 0 else np.nan

            _ds_result["医療圏シェア"] = _ds_result.apply(_calc_share, axis=1)

        # 都道府県フィルター
        if _ds_geo_scope in ("都道府県", "二次医療圏") and _ds_pref_sel:
            _ds_result = _ds_result[_ds_result["都道府県名"] == _ds_pref_sel]

        # 二次医療圏フィルター
        if _ds_geo_scope == "二次医療圏" and _ds_region_sel:
            _ds_result = _ds_result[_ds_result["二次医療圏名"] == _ds_region_sel]

        # 非公表除外（0件を除く。-1はマスク値なので残す）
        if _ds_hide_nan:
            _ds_result = _ds_result[_ds_result[_ds_cnt_col].notna() & (_ds_result[_ds_cnt_col] != 0)]

        # 平均在院日数カラムを作成
        if _ds_los_col and _ds_los_col in _ds_result.columns:
            _ds_result = _ds_result.rename(columns={_ds_los_col: "平均在院日数"})

        # ソート
        _sort_col_map = {"患者総数": _ds_cnt_col, "平均在院日数": "平均在院日数", "医療圏シェア": "医療圏シェア"}
        _sort_col = _sort_col_map.get(_ds_metric, _ds_cnt_col)
        _asc      = _ds_metric == "平均在院日数"
        if _sort_col in _ds_result.columns:
            _ds_result = _ds_result.sort_values(_sort_col, ascending=_asc, na_position="last")

        _ds_result = _ds_result.reset_index(drop=True)
        _ds_result.index += 1

        # カラム表示設定
        _total_n = len(_ds_result)
        # -1（マスク値）を除いた実件数合計
        _total_patients = int(_ds_result[_ds_cnt_col].clip(lower=0).sum()) if _ds_cnt_col in _ds_result.columns else 0
        _ds_year = int(_ds_surg_all["年度"].max()) if "年度" in _ds_surg_all.columns else None
        if _ds_year:
            st.markdown(_source_tag(_dpc_source(_ds_year)), unsafe_allow_html=True)
        st.caption(
            f"**{_total_n:,}病院** が対象 / 疾患: {_ds_disease}"
            + (f" / 合計 {_total_patients:,}例（＊除く）" if not _ds_hide_nan else "")
        )

        _ds_show_cols = ["施設名", "病院区分", "都道府県名", "二次医療圏名", _ds_cnt_col]
        _ds_col_cfg: dict = {
            "施設名":      st.column_config.TextColumn("病院名"),
            "病院区分":    st.column_config.TextColumn("区分"),
            "都道府県名":   st.column_config.TextColumn("都道府県"),
            "二次医療圏名": st.column_config.TextColumn("二次医療圏"),
            _ds_cnt_col:  st.column_config.TextColumn("患者数"),
        }
        if "平均在院日数" in _ds_result.columns:
            _ds_show_cols.append("平均在院日数")
            _ds_col_cfg["平均在院日数"] = st.column_config.TextColumn("平均在院日数")
        if "医療圏シェア" in _ds_result.columns:
            _ds_show_cols.append("医療圏シェア")
            _ds_col_cfg["医療圏シェア"] = st.column_config.TextColumn("医療圏シェア")

        # -1（マスク値）を "*" に変換した表示用コピー
        _ds_disp = _ds_result[[c for c in _ds_show_cols if c in _ds_result.columns]].copy()
        for _c in [_ds_cnt_col, "平均在院日数"]:
            if _c in _ds_disp.columns:
                _v = pd.to_numeric(_ds_disp[_c], errors="coerce")
                _ds_disp[_c] = _v.apply(
                    lambda x: "*" if x == -1 else ("" if pd.isna(x) else (f"{int(x):,}例" if _c == _ds_cnt_col else f"{x:.1f}日"))
                )
        if "医療圏シェア" in _ds_disp.columns:
            _ds_disp["医療圏シェア"] = _ds_result["医療圏シェア"].apply(lambda x: f"{x:.1f}%" if pd.notna(x) else "—")
            st.caption("※医療圏シェアは同一二次医療圏内（非公表・*病院を除く）での件数比率の参考値です")

        # 前回選択した病院のバナーをテーブルの上に表示（地図モードと同じパターン）
        _dsc_last = st.session_state.get("_dsc_last_selected")
        if _dsc_last:
            _dsc_c1, _dsc_c2 = st.columns([4, 1])
            with _dsc_c1:
                st.info(f"🏥 **{_dsc_last}** を選択中")
            with _dsc_c2:
                if st.button("詳細を見る →", key="_dsc_nav_btn", type="primary", use_container_width=True):
                    # 病床報告データから最新行を取得してナビゲート
                    _df_base = st.session_state.df
                    if _df_base is not None:
                        _hr_rows = _df_base[_df_base["医療機関名"] == _dsc_last].sort_values("報告年度", ascending=False)
                        if not _hr_rows.empty:
                            _hr = _hr_rows.iloc[0]
                            st.session_state["_nav_jump"] = {
                                "year":     int(_hr["報告年度"]),
                                "pref":     str(_hr["都道府県名"]),
                                "region":   str(_hr["二次医療圏名"]),
                                "hospital": _dsc_last,
                            }
                            st.session_state["_hospital_chosen"] = True
                            st.session_state["_view_mode"] = "detail"
                            st.session_state.pop("_dsc_last_selected", None)
                            st.rerun()

        st.caption("💡 行をクリックして病院を選択 → 「詳細を見る」で病院詳細に移動できます")
        _ds_evt = st.dataframe(
            _ds_disp,
            use_container_width=True,
            column_config=_ds_col_cfg,
            height=520,
            on_select="rerun",
            selection_mode="single-row",
            key="_dsc_table",
        )

        # 行選択 → DPC施設名からbyosho医療機関名を解決してセッションステートに保存
        _ds_sel_rows = _ds_evt.selection.rows if hasattr(_ds_evt, "selection") else []
        if _ds_sel_rows:
            _sel_row      = _ds_disp.iloc[_ds_sel_rows[0]]
            _sel_dpc_name = _sel_row["施設名"]
            _sel_pref     = _sel_row.get("都道府県名", "") if "都道府県名" in _sel_row.index else ""
            _nav_name     = _sel_dpc_name
            _df_base      = st.session_state.df
            if _df_base is not None:
                _pref_hosps = (
                    _df_base[_df_base["都道府県名"] == _sel_pref]["医療機関名"]
                    if _sel_pref else _df_base["医療機関名"]
                )
                def _norm(s): return s.replace('　', '').replace(' ', '')
                _dpc_norm = _norm(_sel_dpc_name)
                # 1. 完全一致
                if _sel_dpc_name in _pref_hosps.values:
                    _nav_name = _sel_dpc_name
                else:
                    # 2. 部分一致（元の表記）
                    _nav_name = next(
                        (_h for _h in _pref_hosps if _h in _sel_dpc_name or _sel_dpc_name in _h),
                        None
                    )
                    if _nav_name is None:
                        # 3. 正規化（全角スペース除去）後の一致
                        _nav_name = next(
                            (_h for _h in _pref_hosps if _norm(_h) in _dpc_norm or _dpc_norm in _norm(_h)),
                            _sel_dpc_name
                        )
            st.session_state["_dsc_last_selected"] = _nav_name
            st.rerun()

    _render_footer()
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


# Hospital info strip
_sw1, _sw2, _sw3 = st.columns([6, 2, 2])
with _sw1:
    st.markdown(
        f"<div style='padding:6px 0;font-size:0.9rem;color:#374151;'>"
        f"<strong>{hospital}</strong>"
        f"<span style='color:#9ca3af;font-size:0.8rem;margin-left:10px;'>{pref} › {region}</span>"
        f"</div>",
        unsafe_allow_html=True,
    )
with _sw2:
    _sw_years = [int(y) for y in sorted(df["報告年度"].unique(), reverse=True)]
    _sw_year_idx = _sw_years.index(year) if year in _sw_years else 0
    _sw_new_year = st.selectbox("年度", _sw_years, index=_sw_year_idx, key="_detail_year_sw", label_visibility="collapsed")
    if _sw_new_year != year:
        st.session_state["_sel_year"] = _sw_new_year
        st.rerun()
with _sw3:
    if st.button("← 検索に戻る", key="_detail_back_btn", use_container_width=True):
        st.session_state["_view_mode"] = "home"
        st.session_state["_hospital_chosen"] = False
        st.rerun()
st.markdown("<hr style='margin:4px 0 16px;border:none;border-top:1px solid #f3f4f6;'>", unsafe_allow_html=True)

# ── ページヘッダー ─────────────────────────────────────────

_h_address = hosp_row.get("住所", "") if isinstance(hosp_row, pd.Series) else ""
_h_address = "" if str(_h_address) in ("nan", "None", "") else str(_h_address)
_h_url = hosp_row.get("url", "") if isinstance(hosp_row, pd.Series) else ""
_h_url = "" if str(_h_url) in ("nan", "None", "") else str(_h_url).strip()
_addr_part = f"<span>📍 {_h_address}</span>" if _h_address else ""
_url_part  = f'<a href="{_h_url}" target="_blank" style="color:#12886D;text-decoration:none;">公式サイト ↗</a>' if _h_url else ""
_meta_parts = " &nbsp;·&nbsp; ".join(p for p in [_addr_part, _url_part] if p)

_hdr_col, _btn_col = st.columns([8, 1])
with _hdr_col:
    st.markdown(
        f"""
<div style="margin-bottom:4px;">
  <div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-bottom:8px;">
    <span style="background:#EAF4F0;color:#0B6653;border-radius:20px;
                 padding:2px 10px;font-size:0.75rem;font-weight:700;">{year}年度</span>
    <span style="color:#d1d5db;font-size:0.8rem;">›</span>
    <span style="background:#f0fdf4;color:#15803d;border-radius:20px;
                 padding:2px 10px;font-size:0.75rem;font-weight:700;">{pref}</span>
    <span style="color:#d1d5db;font-size:0.8rem;">›</span>
    <span style="background:#fefce8;color:#92400e;border-radius:20px;
                 padding:2px 10px;font-size:0.75rem;font-weight:700;">{region}</span>
  </div>
  <h2 style="font-size:1.65rem;font-weight:800;color:#111827;margin:0 0 6px;line-height:1.25;">{hospital}</h2>
  <div style="font-size:0.82rem;color:#6b7280;">{_meta_parts}</div>
</div>""",
        unsafe_allow_html=True,
    )
with _btn_col:
    components.html(
        """
        <style>
        button {
            background: #f9fafb;
            border: 1px solid #e5e7eb;
            border-radius: 8px;
            padding: 6px 14px;
            font-size: 0.82rem;
            color: #374151;
            cursor: pointer;
            float: right;
            margin-top: 18px;
            font-family: sans-serif;
            transition: background 0.15s;
        }
        button:hover { background: #f3f4f6; border-color: #d1d5db; }
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

def kpi_card(col, label, value, sub="", color="#3b82f6"):
    col.markdown(
        f'<div class="metric-card" style="border-top-color:{color};">'
        f'<div class="metric-label">{label}</div>'
        f'<div class="metric-value">{value}</div>'
        f'<div class="metric-sub">{sub or "&nbsp;"}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

_occ_pct = occ * 100
_occ_color = (
    "#10b981" if _occ_pct >= 80 else
    "#3b82f6" if _occ_pct >= 60 else
    "#f59e0b" if _occ_pct >= 40 else "#ef4444"
)
# 病床機能報告では医師・看護師の人員欄が未記入（0）の病院がある（特に古い
# 年度）。病床があるのに0人は実態でなく未報告なので「—」で示す。
_nurse_txt = f"看護師 {nurses:,}人" if nurses > 0 else "看護師 —（未報告）"
if doctors > 0:
    _doc_val, _doc_sub = f"{doctors:,}人", _nurse_txt
else:
    _doc_val, _doc_sub = "—", f"未報告 · {_nurse_txt}"

kpi_card(m1, "許可病床数",  f"{total_kyoka:,}床",         kado_sub,                color="#6366f1")
kpi_card(m2, "総稼働率",    f"{_occ_pct:.1f}%",           "",                      color=_occ_color)
kpi_card(m3, "地域内順位",  f"{region_rank}位",           f"/ {len(region_df)}院中", color="#8b5cf6")
kpi_card(m4, "地域シェア",  f"{region_share_val:.1f}%",   "許可病床数ベース",       color="#0ea5e9")
kpi_card(m5, "常勤医師数",  _doc_val,                     _doc_sub,                color="#14b8a6")

st.markdown(_source_tag(_byosho_source(year)), unsafe_allow_html=True)
st.markdown("<br>", unsafe_allow_html=True)


# ── DPC判定 ───────────────────────────────────────────────

_dpc_ban: int | None = None
_dpc_hosp_row = None
_is_dpc = False
_dpc_matched_name = None
_dpc_avail_years: list[int] = []   # この病院にDPCがある年度（案内用）
_dpc_match_all = _load_dpc_match(_DPC_MATCH_MTIME)
_dpc_hospitals_all = _load_dpc_hospitals()
if _dpc_match_all is not None:
    _m = _dpc_match_all[_dpc_match_all["病床報告施設名"] == hospital]
    if not _m.empty and "未結合" not in str(_m.iloc[0]["マッチ状態"]):
        _dpc_matched_name = str(_m.iloc[0]["DPC施設名"])
        if _dpc_hospitals_all is not None:
            _dpc_h_all = _dpc_hospitals_all[_dpc_hospitals_all["施設名"] == _dpc_matched_name]
            if not _dpc_h_all.empty:
                if "年度" in _dpc_h_all.columns:
                    _dpc_avail_years = sorted(int(y) for y in _dpc_h_all["年度"].dropna().unique())
                # 病床機能報告と年度がズレて誤解を招かないよう、選択中の年度と
                # 同じ年度のDPCのみを使う（同年度のDPCが無ければDPC表示は出さない）。
                _dpc_h_year = _dpc_h_all[_dpc_h_all["年度"] == year] if "年度" in _dpc_h_all.columns else _dpc_h_all
                if not _dpc_h_year.empty:
                    _is_dpc = True
                    _dpc_ban = int(_dpc_h_year.iloc[0]["告示番号"])
                    _dpc_hosp_row = _dpc_h_year.iloc[0]

# 経年トレンド用：選択中の年度に関わらず、この病院のDPC症例数を全年度分集計
_dpc_case_trend_df = pd.DataFrame()
if _dpc_matched_name is not None:
    _dpc_cases_all = _load_dpc_mdc_cases()
    if _dpc_cases_all is not None:
        _dct_sub = _dpc_cases_all[_dpc_cases_all["施設名"] == _dpc_matched_name]
        if not _dct_sub.empty:
            _dct_mdc_cols = [c for c in _dct_sub.columns if c.startswith("MDC")]
            _dct_agg = _dct_sub.groupby("年度", as_index=False)[_dct_mdc_cols].sum(numeric_only=True)
            _dct_agg["DPC症例数"] = _dct_agg[_dct_mdc_cols].sum(axis=1)
            _dpc_case_trend_df = _dct_agg[["年度", "DPC症例数"]].sort_values("年度")


# ── タブ ──────────────────────────────────────────────────

# タブラベルは絵文字を使わずテキストのみ（デザイントークン方針：
# 絵文字は環境依存で描画が変わり、色が意味を運ぶ体系も崩すため）
_tab_labels = [
    "病院概要",
    "地図",
    "地域比較",
    "ランキング",
    "経年トレンド",
    "スタッフ分析",
    "病床・手術分析",
]
if _is_dpc:
    _tab_labels.append("DPC分析")

# 各タブに何が入っているか一目でわかるよう、タブを描画する直前に簡潔な案内を表示する
# （st.tabs自体はラベルしか見えず中身は開くまで分からない上、タブ内部で出力すると
#  そのタブの一番下＝スクロールした先にしか表示されず発見の助けにならないため、
#  タブバーより上に置く）
st.caption(
    "**タブの中身**　"
    "病院概要: 基本情報・DPC上位疾患・医療設備・施設基準届出（区分含む）／ "
    "地図: 所在地・移動時間／ "
    "地域比較: 医療圏内での位置づけ／ "
    "ランキング: 指標別の順位／ "
    "経年トレンド: 年度ごとの推移／ "
    "スタッフ分析: 職種別の配置状況／ "
    "病床・手術分析: 入院基本料別病床数・在宅復帰率・手術実績"
)

_all_tabs = st.tabs(_tab_labels)
tab1, tab7, tab2, tab3, tab4, tab5, tab6 = _all_tabs[:7]
tab_dpc = _all_tabs[7] if _is_dpc else None


# ── TAB 1: 病院概要 ─────────────────────────────────────────

with tab1:
    if not isinstance(hosp_row, pd.Series):
        st.warning("選択した年度のデータが見つかりません")
    else:
        # ── DPC MDC別患者件数 上位3 ──────────────────────────
        # DPCは選択中の病床機能報告と同じ年度のみ表示（_is_dpc が年度連動済み）。
        # 同年度のDPCが無いが他年度にはある場合は、切替を促す案内を出す。
        if not _is_dpc and _dpc_avail_years:
            _dpc_yrs_txt = "・".join(_reiwa_nendo(y) for y in _dpc_avail_years)
            st.info(
                f"この病院のDPCデータは {_dpc_yrs_txt}（{'／'.join(str(y) for y in _dpc_avail_years)}）があります。"
                f"病床機能報告と同じ年度で見るため、上の年度セレクトを切り替えるとDPCが表示されます。"
            )
        if _is_dpc and _dpc_ban is not None:
            _ov_cases_all = _load_dpc_mdc_cases()
            if _ov_cases_all is not None:
                _ov_cases = _ov_cases_all[_ov_cases_all["告示番号"] == _dpc_ban]
                if "年度" in _ov_cases.columns:
                    _ov_cases = _ov_cases[_ov_cases["年度"] == year]
                if not _ov_cases.empty:
                    _ov_mdc_keys = [k for k in MDC_LABELS if k in _ov_cases.columns]
                    if _ov_mdc_keys:
                        _ov_sum = _ov_cases[_ov_mdc_keys].sum()
                        _ov_top3 = _ov_sum[_ov_sum > 0].sort_values(ascending=False).head(3)
                        if not _ov_top3.empty:
                            _ov_noop_row = _ov_cases[_ov_cases["手術有無"] == "無し"]
                            _ov_surg_row = _ov_cases[_ov_cases["手術有無"] == "有り"]
                            st.markdown('<div class="section-header">DPC 患者件数 上位3領域</div>', unsafe_allow_html=True)
                            st.markdown(_source_tag(_dpc_source(year)), unsafe_allow_html=True)
                            _ov_accent = ["#3b82f6", "#8b5cf6", "#06b6d4"]
                            _ov_cols = st.columns(3)
                            for _oi, ((_ov_key, _ov_val), _ov_col, _ov_ac) in enumerate(
                                zip(_ov_top3.items(), _ov_cols, _ov_accent)
                            ):
                                _ov_label = MDC_LABELS.get(_ov_key, _ov_key)
                                _ov_k_noop = int(_ov_noop_row[_ov_key].sum()) if _ov_key in _ov_noop_row.columns else 0
                                _ov_k_surg = int(_ov_surg_row[_ov_key].sum()) if _ov_key in _ov_surg_row.columns else 0
                                _ov_col.markdown(
                                    f'<div class="metric-card" style="border-top-color:{_ov_ac};">'
                                    f'<div class="metric-label">{_oi+1}位　{_ov_label}</div>'
                                    f'<div class="metric-value">{int(_ov_val):,}</div>'
                                    f'<div class="metric-sub">'
                                    f'手術なし {_ov_k_noop:,}件　手術あり {_ov_k_surg:,}件'
                                    f'</div>'
                                    f'</div>',
                                    unsafe_allow_html=True,
                                )

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
                "許可病床数（床）": st.column_config.NumberColumn(format="%,d 床"),
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
                    f'border-right:1px solid #F0EDE6;">'
                    f'<div style="color:#6E6A5E;font-size:0.68rem;margin-bottom:3px;">{lbl}</div>'
                    f'<div style="color:#26251F;font-size:1.05rem;font-weight:600;">{val}台</div>'
                    f'</div>'
                    for lbl, val in breakdown.items()
                )
                return (
                    f'<div style="background:#fff;border:1px solid #E8E4DB;'
                    f'border-left:5px solid {accent};border-radius:12px;'
                    f'box-shadow:0 1px 2px rgba(70,60,35,.05);'
                    f'padding:14px 18px;margin-bottom:10px;">'
                    f'<div style="color:{accent};font-size:0.78rem;font-weight:700;'
                    f'letter-spacing:.4px;margin-bottom:6px;">{title}</div>'
                    f'<div style="display:flex;align-items:baseline;gap:3px;margin-bottom:10px;">'
                    f'<span style="color:#26251F;font-size:2.2rem;font-weight:700;">{total}</span>'
                    f'<span style="color:#6E6A5E;font-size:0.9rem;margin-left:2px;">台</span>'
                    f'</div>'
                    f'<div style="display:flex;border-top:1px solid #F0EDE6;padding-top:8px;">'
                    f'{items_html}'
                    f'</div></div>'
                )

            def _equip_badge(label: str, val: int) -> str:
                return (
                    f'<div style="background:#fff;border:1px solid #E8E4DB;'
                    f'border-radius:10px;padding:10px 14px;text-align:center;">'
                    f'<div style="color:#6E6A5E;font-size:0.72rem;margin-bottom:4px;">{label}</div>'
                    f'<div style="color:#26251F;font-size:1.4rem;font-weight:700;">{val}'
                    f'<span style="font-size:0.75rem;color:#6E6A5E;margin-left:2px;">台</span></div>'
                    f'</div>'
                )

            ct_total = _ev("CT台数") or 0
            has_ct   = any(_ev(c) is not None for c in CT_BREAKDOWN) or _ev("CT台数") is not None
            if has_ct:
                breakdown_ct = {lbl: _ev(col) or 0 for col, lbl in CT_BREAKDOWN.items()}
                st.markdown(
                    _modality_card("CT（コンピューター断層撮影装置）", "#33739E", ct_total, breakdown_ct),
                    unsafe_allow_html=True,
                )

            mri_total = _ev("MRI台数") or 0
            has_mri   = any(_ev(c) is not None for c in MRI_BREAKDOWN) or _ev("MRI台数") is not None
            if has_mri:
                breakdown_mri = {lbl: _ev(col) or 0 for col, lbl in MRI_BREAKDOWN.items()}
                st.markdown(
                    _modality_card("MRI（磁気共鳴画像診断装置）", "#B3574B", mri_total, breakdown_mri),
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

        # ── 施設基準届出セクション ──────────────────────
        _sk_df = _load_shisetsu_kijun()
        if _sk_df is not None and isinstance(hosp_row, pd.Series):
            _sk_name_norm = _normalize_hospital_for_match(hospital)
            _sk_pref_code = None
            for _c, _n in PREF_CODE_MAP.items():
                if _n == pref:
                    _sk_pref_code = _c
                    break

            if _sk_pref_code:
                # 完全一致（優先）
                _sk_matched = _sk_df[
                    (_sk_df["医療機関名_正規化"] == _sk_name_norm)
                    & (_sk_df["都道府県コード"] == _sk_pref_code)
                ]
                # サフィックス一致（法人名+団体名+施設名 に対応）
                if _sk_matched.empty and _sk_name_norm:
                    _sk_matched = _sk_df[
                        (_sk_df["医療機関名_正規化"].str.endswith(_sk_name_norm))
                        & (_sk_df["都道府県コード"] == _sk_pref_code)
                    ]
            else:
                _sk_matched = pd.DataFrame()

            _sk_covered_prefs = set(_sk_df["都道府県コード"].unique())

            st.markdown('<div class="section-header">施設基準届出（診療報酬）</div>', unsafe_allow_html=True)

            if not _sk_matched.empty:
                _sk_detail_cols = [c for c in ["区分", "病棟数", "病床数"] if c in _sk_matched.columns]
                _sk_items_df = (
                    _sk_matched[["受理届出名称", "受理記号"] + _sk_detail_cols]
                    .drop_duplicates()
                    .reset_index(drop=True)
                )

                # 施設基準届出（地方厚生局）は厚生局によって区分（急性期一般入院料等の
                # 段階）を公表していない場合がある。病床機能報告は病床を持つ医療機関が
                # 毎年義務的に報告する全国統一フォーマットのため、一般病棟・療養病棟・
                # 障害者施設等の区分はここから確実に補完できる（精神病棟・有床診療所・
                # 結核病棟は病床機能報告の対象外のため対象外）。
                if "区分" in _sk_items_df.columns:
                    _SK_NYUIN_KEYWORD_MAP = {
                        "一般病棟入院基本料": [
                            "急性期一般入院料", "地域一般入院料", "一般病棟特別入院基本料",
                            "特定機能病院一般病棟", "専門病院", "特定一般病棟入院料",
                        ],
                        "療養病棟入院基本料": ["療養病棟入院料", "療養病棟特別入院基本料"],
                        "障害者施設等入院基本料": ["障害者施設等"],
                    }
                    _ward_df_all = st.session_state.get("ward_df")
                    if _ward_df_all is not None and not _ward_df_all.empty:
                        _hosp_wards = _ward_df_all[
                            (_ward_df_all["医療機関名"] == hospital)
                            & (_ward_df_all["報告年度"] == year)
                        ]
                        _hosp_wards_kubun: dict[str, str] = {}
                        for _sk_name, _kw_list in _SK_NYUIN_KEYWORD_MAP.items():
                            _matched_wards = _hosp_wards[
                                _hosp_wards["入院基本料"].apply(
                                    lambda x: any(kw in str(x) for kw in _kw_list)
                                )
                            ]
                            _vals = [v for v in _matched_wards["入院基本料"].dropna().unique() if v != "-"]
                            if _vals:
                                _hosp_wards_kubun[_sk_name] = "・".join(sorted(_vals))

                        if _hosp_wards_kubun:
                            def _fill_kubun(row):
                                if str(row["区分"]).strip():
                                    return row["区分"]
                                return _hosp_wards_kubun.get(row["受理届出名称"], row["区分"])
                            _sk_items_df["区分"] = _sk_items_df.apply(_fill_kubun, axis=1)
                _sk_ym = _sk_matched["年月"].iloc[0] if "年月" in _sk_matched.columns else ""
                if _sk_ym:
                    st.caption(f"出典：診療報酬 施設基準届出情報（{_sk_ym} 現在）")

                # 施設種別（病院／有床診療所／無床診療所）バッジ。入院基本料の届出
                # パターンから判定した値（build_shisetsu_kijun.py の _classify_facility_types）。
                if "施設種別" in _sk_matched.columns:
                    _sk_fac_type = _sk_matched["施設種別"].iloc[0]
                    _sk_fac_color = {
                        "病院": "#3b82f6", "有床診療所": "#f59e0b", "無床診療所": "#6b7280",
                    }.get(_sk_fac_type, "#6b7280")
                    st.markdown(
                        f'<span style="display:inline-block;background:{_sk_fac_color}22;'
                        f'color:{_sk_fac_color};border:1px solid {_sk_fac_color}55;'
                        f'border-radius:10px;padding:3px 10px;font-size:0.78rem;font-weight:700;'
                        f'margin-bottom:8px;">🏷️ {_sk_fac_type}</span>',
                        unsafe_allow_html=True,
                    )

                _SK_GROUPS_DEF = [
                    ("病床・入院体制", "#3498db", [
                        "入院基本料", "入院時食事療養",
                        "地域包括ケア病棟", "緩和ケア病棟", "回復期リハビリテーション病棟",
                        "療養病棟入院料", "障害者施設等入院基本料", "短期滞在手術等基本料",
                    ]),
                    ("救急・集中治療", "#e74c3c", [
                        "救急医療管理加算", "集中治療室管理料", "ハイケアユニット",
                        "超急性期脳卒中加算", "新生児集中治療", "院内トリアージ",
                        "小児入院医療管理料",
                    ]),
                    ("手術・麻酔", "#8e44ad", [
                        "腹腔鏡", "胸腔鏡", "切除術", "切断術", "植込術", "交換術",
                        "修復術", "センチネルリンパ節", "体外衝撃波", "麻酔管理料",
                        "輸血管理料", "輸血適正使用加算", "術後疼痛管理", "周術期薬剤管理",
                        "人工肛門・人工膀胱", "椎間板内酵素注入", "刺激装置植込術",
                        "膀胱水圧拡張術", "精巣温存手術", "ゲル充填人工乳房",
                        "乳癌センチネルリンパ節", "乳腺悪性腫瘍手術",
                        "ペースメーカー", "大動脈バルーン", "手術の通則の16",
                        "手術の通則の19", "緊急整復固定加算", "下肢創傷処置管理料",
                        "ストーマ合併症加算", "組織拡張器による再建",
                    ]),
                    ("検査・画像診断", "#2980b9", [
                        "検体検査管理加算", "遺伝学的検査", "画像診断管理加算",
                        "ＣＴ撮影", "ＭＲＩ撮影", "病理診断管理加算",
                        "遺伝カウンセリング加算", "ＨＰＶ核酸検出", "ＢＲＣＡ",
                        "冠動脈ＣＴ", "乳房ＭＲＩ", "前立腺針生検法",
                        "ヘッドアップティルト", "長期継続頭蓋内脳波", "神経学的検査",
                        "胎児心エコー", "内服・点滴誘発試験",
                        "放射線治療専任加算", "外来放射線", "高エネルギー放射線",
                        "定位放射線治療", "画像誘導放射線治療", "一回線量増加加算",
                        "体外照射呼吸性移動",
                    ]),
                    ("リハビリテーション", "#27ae60", [
                        "リハビリテーション料", "摂食嚥下機能回復",
                        "がん患者リハビリテーション料", "排尿自立支援加算",
                        "外来排尿自立指導料", "二次性骨折予防継続管理料",
                        "小児運動器疾患指導管理料",
                    ]),
                    ("がん・専門診療", "#f39c12", [
                        "がん", "抗悪性腫瘍", "外来腫瘍化学療法診療料",
                        "悪性腫瘍病理組織標本加算", "外来化学療法加算",
                        "婦人科特定疾患治療管理料",
                    ]),
                    ("精神科", "#9b59b6", [
                        "精神病棟", "精神療養病棟", "精神疾患診療体制加算",
                        "認知症ケア加算", "せん妄ハイリスク",
                    ]),
                    ("在宅・地域連携", "#16a085", [
                        "在宅療養後方支援病院", "在宅患者訪問", "訪問看護",
                        "在宅時医学総合管理料", "在宅持続陽圧呼吸療法",
                        "開放型病院共同指導料", "がん治療連携計画策定料",
                        "ハイリスク妊産婦共同管理料", "ハイリスク妊産婦連携指導料",
                        "肝炎インターフェロン", "ハイリスク妊娠管理加算",
                        "ハイリスク分娩管理加算",
                    ]),
                    ("入院管理・加算", "#7f8c8d", [
                        "医療ＤＸ", "診療録管理体制加算", "医師事務作業補助",
                        "急性期看護補助", "看護職員夜間配置", "療養環境加算",
                        "重症者等療養環境", "栄養サポートチーム加算",
                        "医療安全対策加算", "感染対策向上加算",
                        "患者サポート体制充実加算", "報告書管理体制加算",
                        "褥瘡ハイリスク", "入退院支援加算",
                        "地域医療体制確保加算", "データ提出加算",
                        "看護職員処遇改善評価料", "入院ベースアップ評価料",
                        "後発医薬品使用体制加算", "バイオ後続品使用体制加算",
                        "病棟薬剤業務実施加算", "無菌製剤処理料",
                        "人工腎臓", "透析液水質確保加算", "導入期加算",
                        "下肢末梢動脈疾患指導管理加算",
                        "外来栄養食事指導料", "糖尿病合併症管理料",
                        "糖尿病透析予防指導管理料",
                    ]),
                ]

                MAX_CHIPS = 6

                # グループ分類
                _sk_group_results = []
                _sk_assigned_idx = set()
                for _grp_name, _grp_color, _grp_kws in _SK_GROUPS_DEF:
                    _grp_rows = _sk_items_df[
                        _sk_items_df["受理届出名称"].apply(
                            lambda x: any(kw in str(x) for kw in _grp_kws)
                        )
                    ]
                    if not _grp_rows.empty:
                        _chips = []
                        for _, _r in _grp_rows.iterrows():
                            _code = str(_r["受理記号"]).strip()
                            _chips.append(
                                _code if _code not in ("", "nan") else str(_r["受理届出名称"])[:12]
                            )
                            _sk_assigned_idx.add(_r.name)
                        _sk_group_results.append((_grp_name, _grp_color, _chips, _grp_rows))

                _sk_unassigned = _sk_items_df[~_sk_items_df.index.isin(_sk_assigned_idx)]
                if not _sk_unassigned.empty:
                    _chips = []
                    for _, _r in _sk_unassigned.iterrows():
                        _code = str(_r["受理記号"]).strip()
                        _chips.append(
                            _code if _code not in ("", "nan") else str(_r["受理届出名称"])[:12]
                        )
                    _sk_group_results.append(("その他", "#95a5a6", _chips, _sk_unassigned))

                # 2カラムカードグリッド
                _sk_col_a, _sk_col_b = st.columns(2)
                for _gi, (_grp_name, _grp_color, _chips, _grp_rows) in enumerate(_sk_group_results):
                    _sk_col = _sk_col_a if _gi % 2 == 0 else _sk_col_b
                    with _sk_col:
                        _chips_html = ""
                        for _chip in _chips[:MAX_CHIPS]:
                            _chips_html += (
                                f'<span style="display:inline-block;background:{_grp_color}22;'
                                f'color:{_grp_color};border:1px solid {_grp_color}55;'
                                f'border-radius:10px;padding:2px 8px;font-size:0.72rem;'
                                f'margin:2px 2px;white-space:nowrap;">{_chip}</span>'
                            )
                        _extra = len(_chips) - MAX_CHIPS
                        if _extra > 0:
                            _chips_html += (
                                f'<span style="color:#6E6A5E;font-size:0.72rem;margin-left:4px;">'
                                f'+{_extra}件</span>'
                            )
                        st.markdown(
                            f'<div style="background:#fff;border:1px solid #E8E4DB;'
                            f'border-left:3px solid {_grp_color};'
                            f'border-radius:10px;padding:10px 12px;margin-bottom:8px;">'
                            f'<div style="color:{_grp_color};font-size:0.72rem;font-weight:700;'
                            f'letter-spacing:.3px;margin-bottom:6px;">{_grp_name}</div>'
                            f'<div style="line-height:1.8;">{_chips_html}</div>'
                            f'</div>',
                            unsafe_allow_html=True,
                        )

                # 入院基本料の区分（急性期一般入院料１等）はカード内のチップ表記
                # （受理記号のみ）では見えないため、常時表示のバッジで明示する
                # （折りたたみ式の「全一覧」の中に埋もれて発見されないのを防ぐ）
                _sk_kubun_values = sorted({
                    v for v in _sk_items_df.get("区分", pd.Series(dtype=str)).astype(str)
                    if v.strip() and v.strip() != "nan"
                })
                if _sk_kubun_values:
                    st.markdown(
                        '<div style="background:#EAF4F0;border:1px solid #BFDFD4;'
                        'border-radius:10px;padding:8px 12px;margin:4px 0 10px 0;">'
                        '<span style="color:#0B6653;font-weight:700;font-size:0.72rem;">'
                        '入院基本料の区分：</span> '
                        f'<span style="font-size:0.85rem;color:#26251F;">{"・".join(_sk_kubun_values)}</span>'
                        '</div>',
                        unsafe_allow_html=True,
                    )

                # フル一覧（エキスパンダー）
                with st.expander(f"届出項目 全一覧（{len(_sk_items_df)}件）"):
                    for _grp_name, _grp_color, _chips, _grp_rows in _sk_group_results:
                        st.markdown(f"**{_grp_name}**")
                        for _, _r in _grp_rows.iterrows():
                            _kubun = str(_r.get("区分", "")).strip()
                            _byoto = str(_r.get("病棟数", "")).strip()
                            _byosho = str(_r.get("病床数", "")).strip()
                            _detail_parts = [p for p in [_kubun, _byoto, _byosho] if p and p != "nan"]
                            _detail = f"（{'・'.join(_detail_parts)}）" if _detail_parts else ""
                            st.markdown(f"- {_r['受理届出名称']}{_detail}")

            elif _sk_pref_code and _sk_pref_code in _sk_covered_prefs:
                # 都道府県データはあるが病院名がマッチしなかった（診療所等は対象外）
                st.caption(f"この病院の施設基準データが見つかりませんでした（{pref}のデータは収録済み）。")

            else:
                _sk_covered_names = [PREF_CODE_MAP[c] for c in sorted(_sk_covered_prefs) if c in PREF_CODE_MAP]
                st.caption(
                    f"施設基準データは現在 {'・'.join(_sk_covered_names)} のみ収録しています。"
                    f"他地域は各地方厚生局からダウンロードして追加できます。"
                )


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

    _RANK_OPTIONS = {
        "許可病床数":  {"col": "合計_許可病床数",  "show": ["合計_許可病床数", "合計_稼働病床数", "地域シェア(%)", "合計稼働率"], "labels": ["許可病床数", "稼働病床数", "地域シェア", "稼働率"]},
        "稼働率":      {"col": "合計稼働率",        "show": ["合計稼働率", "合計_許可病床数"],                                   "labels": ["稼働率",   "許可病床数"]},
        "医師数":      {"col": "常勤医師数",         "show": ["常勤医師数", "医師数_per100床"],                                   "labels": ["常勤医師数", "医師数/100床"]},
        "看護師数":    {"col": "常勤看護師数",       "show": ["常勤看護師数", "看護師数_per100床"],                               "labels": ["常勤看護師", "看護師/100床"]},
        "救急搬送":    {"col": "救急搬送件数",       "show": ["救急搬送件数", "合計_許可病床数"],                                 "labels": ["救急搬送件数", "許可病床数"]},
        "CT":          {"col": "CT台数",             "show": ["CT台数", "合計_許可病床数"],                                       "labels": ["CT台数", "許可病床数"]},
        "MRI":         {"col": "MRI台数",            "show": ["MRI台数", "合計_許可病床数"],                                      "labels": ["MRI台数", "許可病床数"]},
    }

    rank_sel = st.radio("ランキング項目", list(_RANK_OPTIONS.keys()), horizontal=True, key="_rank_sel")
    _opt = _RANK_OPTIONS[rank_sel]
    st.plotly_chart(
        ranking_table_fig(region_df, hospital, rank_col=_opt["col"], show_cols=_opt["show"], col_labels=_opt["labels"]),
        use_container_width=True,
    )


# ── TAB 4: 経年トレンド ────────────────────────────────────

with tab4:
    if len(trend_df) < 2:
        _yrs_in_data = sorted(df["報告年度"].dropna().unique().astype(int))
        _yr_str = "・".join(str(y) for y in _yrs_in_data)
        st.info(f"この病院の経年データが1年度分しかありません（現在のデータセット: {_yr_str}年度）。複数年度のデータを取り込むと経年比較が表示されます。")
    else:
        c1, c2 = st.columns(2)
        with c1:
            st.plotly_chart(trend_beds(trend_df, hospital), use_container_width=True)
        with c2:
            st.plotly_chart(trend_occupancy(trend_df, hospital), use_container_width=True)

        _los_trend_df = hospital_los_trend(st.session_state.ward_df, hospital)
        _has_los_trend = len(_los_trend_df.dropna(subset=["平均在院日数"])) >= 2
        _has_dpc_trend = len(_dpc_case_trend_df) >= 2

        if _has_los_trend or _has_dpc_trend:
            c3, c4 = st.columns(2)
            if _has_los_trend:
                with c3:
                    st.plotly_chart(trend_los(_los_trend_df, hospital), use_container_width=True)
                    st.markdown(_source_tag("病床機能報告（在棟延べ数・新規入棟患者数・退棟患者数）"), unsafe_allow_html=True)
            if _has_dpc_trend:
                with c4:
                    st.plotly_chart(trend_dpc_cases(_dpc_case_trend_df, hospital), use_container_width=True)
                    st.markdown(_source_tag("DPC導入の影響評価に係る調査"), unsafe_allow_html=True)

        st.plotly_chart(trend_staff(trend_df, hospital), use_container_width=True)

        st.markdown('<div class="section-header">年度別データ一覧</div>', unsafe_allow_html=True)
        _disp_df = trend_df
        disp_cols = ["報告年度", "合計_許可病床数", "合計_稼働病床数"]
        for t in BED_TYPES:
            if f"{t}_許可病床数" in trend_df.columns:
                disp_cols.append(f"{t}_許可病床数")
        if _has_los_trend:
            _disp_df = _disp_df.merge(_los_trend_df[["報告年度", "平均在院日数"]], on="報告年度", how="left")
            disp_cols.append("平均在院日数")
        if "常勤医師数" in trend_df.columns:
            disp_cols += ["常勤医師数", "常勤看護師数"]
        st.dataframe(_disp_df[disp_cols].reset_index(drop=True), hide_index=True, use_container_width=True)

        if len(trend_df) >= 2:
            first_y = trend_df.iloc[0]
            last_y  = trend_df.iloc[-1]
            _f_beds = pd.to_numeric(first_y.get("合計_許可病床数"), errors="coerce")
            _l_beds = pd.to_numeric(last_y.get("合計_許可病床数"), errors="coerce")
            _f_act  = pd.to_numeric(first_y.get("合計_稼働病床数"), errors="coerce")
            _l_act  = pd.to_numeric(last_y.get("合計_稼働病床数"), errors="coerce")
            if pd.notna(_f_beds) and pd.notna(_l_beds):
                delta_beds = int(_l_beds) - int(_f_beds)
                st.markdown('<div class="section-header">期間内変化サマリー</div>', unsafe_allow_html=True)
                sc1, sc2, sc3 = st.columns(3)
                sc1.metric(
                    f"許可病床数 ({int(first_y['報告年度'])}→{int(last_y['報告年度'])})",
                    f"{int(_l_beds):,}床",
                    f"{delta_beds:+,}床",
                )
                if pd.notna(_f_act) and pd.notna(_l_act):
                    delta_occ = (
                        _l_act / max(_l_beds, 1) - _f_act / max(_f_beds, 1)
                    ) * 100
                    sc2.metric(
                        "稼働率変化",
                        f"{_l_act / max(_l_beds, 1) * 100:.1f}%",
                        f"{delta_occ:+.1f}pt",
                    )
                if "常勤医師数" in trend_df.columns:
                    _f_doc = pd.to_numeric(first_y.get("常勤医師数"), errors="coerce")
                    _l_doc = pd.to_numeric(last_y.get("常勤医師数"), errors="coerce")
                    if pd.notna(_f_doc) and pd.notna(_l_doc):
                        delta_doc = int(_l_doc) - int(_f_doc)
                        sc3.metric("常勤医師数変化", f"{int(_l_doc):,}人", f"{delta_doc:+,}人")


# ── TAB 5: スタッフ分析 ────────────────────────────────────

with tab5:
    has_staff = "常勤医師数" in region_df.columns and "常勤看護師数" in region_df.columns

    if not has_staff:
        st.info("スタッフデータが含まれていません")
    else:
        import plotly.graph_objects as _go_staff
        region_df_staff = add_derived_columns(region_df)

        # ── 職種別 経年推移 ──────────────────────────────────────────
        st.markdown('<div class="section-header">職種別 経年推移</div>', unsafe_allow_html=True)

        # col: (常勤列名, 非常勤列名, ラベル, 常勤色, 非常勤色)
        _staff_trend_defs = [
            ("常勤医師数",          "非常勤医師数",          "医師",             "#e74c3c", "#f1948a"),
            ("常勤看護師数",         "非常勤看護師数",         "看護師",           "#3498db", "#85c1e9"),
            ("常勤理学療法士数",     "非常勤理学療法士数",     "理学療法士（PT）", "#10b981", "#6ee7b7"),
            ("常勤作業療法士数",     "非常勤作業療法士数",     "作業療法士（OT）", "#f59e0b", "#fcd34d"),
            ("常勤言語聴覚士数",     "非常勤言語聴覚士数",     "言語聴覚士（ST）", "#8b5cf6", "#c4b5fd"),
            ("常勤薬剤師数",         "非常勤薬剤師数",         "薬剤師",           "#06b6d4", "#67e8f9"),
            ("常勤診療放射線技師数", "非常勤診療放射線技師数", "診療放射線技師",   "#ec4899", "#f9a8d4"),
            ("常勤臨床検査技師数",   "非常勤臨床検査技師数",   "臨床検査技師",     "#84cc16", "#bef264"),
        ]
        _trend_avail = [
            (ccol, pcol, lbl, cclr, pclr)
            for ccol, pcol, lbl, cclr, pclr in _staff_trend_defs
            if ccol in trend_df.columns and trend_df[ccol].notna().any()
        ]

        if _trend_avail:
            for _row_start in range(0, len(_trend_avail), 2):
                _row_defs = _trend_avail[_row_start:_row_start + 2]
                _st_cols = st.columns(len(_row_defs))
                for (ccol, pcol, lbl, cclr, pclr), _stc in zip(_row_defs, _st_cols):
                    _years = trend_df["報告年度"].dropna().sort_values().unique()
                    _x = [str(int(y)) + "年度" for y in _years]
                    _c_vals = [
                        int(trend_df.loc[trend_df["報告年度"] == y, ccol].fillna(0).iloc[0])
                        if len(trend_df.loc[trend_df["報告年度"] == y]) > 0 else 0
                        for y in _years
                    ]
                    _has_part = pcol in trend_df.columns and trend_df[pcol].notna().any()
                    _p_fvals = [
                        float(trend_df.loc[trend_df["報告年度"] == y, pcol].fillna(0).iloc[0])
                        if _has_part and len(trend_df.loc[trend_df["報告年度"] == y]) > 0 else 0.0
                        for y in _years
                    ]
                    _sfig = _go_staff.Figure(layout=dict(
                        title=dict(text=f"{lbl}数 経年推移"),
                        barmode="stack",
                        showlegend=bool(_has_part),
                        height=320,
                        margin=dict(t=50, b=30, l=50, r=20),
                        yaxis=dict(title="人数", rangemode="tozero"),
                        xaxis=dict(type="category"),
                        dragmode=False,
                    ))
                    _sfig.add_trace(_go_staff.Bar(
                        name="常勤",
                        x=_x,
                        y=_c_vals,
                        marker_color=cclr,
                        text=[f"{v:,}" for v in _c_vals],
                        textposition="inside",
                        width=0.5,
                    ))
                    if _has_part and any(v > 0 for v in _p_fvals):
                        _sfig.add_trace(_go_staff.Bar(
                            name="非常勤（常勤換算）",
                            x=_x,
                            y=_p_fvals,
                            marker_color=pclr,
                            text=[f"{v:.1f}" for v in _p_fvals],
                            textposition="inside",
                            width=0.5,
                        ))
                    _stc.plotly_chart(_sfig, use_container_width=True)

        st.caption("※ 非常勤は常勤換算数（FTE）で表示。施設票の再インポート後に反映されます")
        st.markdown("---")

        # ── 地域内スタッフ比較 ──────────────────────────────────────
        st.markdown('<div class="section-header">地域内スタッフ比較</div>', unsafe_allow_html=True)
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

            # 病院カテゴリ分類（地域比較タブと同じ考え方: 最大比率の種別が35%以上なら採用）
            def _bed_category(row):
                total = pd.to_numeric(row.get("合計_許可病床数", 0), errors="coerce") or 0
                if total == 0:
                    return "混合・その他"
                acute  = (pd.to_numeric(row.get("高度急性期_許可病床数", 0), errors="coerce") or 0)
                acute += (pd.to_numeric(row.get("急性期_許可病床数",     0), errors="coerce") or 0)
                recov  = pd.to_numeric(row.get("回復期_許可病床数",     0), errors="coerce") or 0
                chron  = pd.to_numeric(row.get("慢性期_許可病床数",     0), errors="coerce") or 0
                ratios = {"急性期系": acute / total, "回復期系": recov / total, "慢性期系": chron / total}
                best, best_r = max(ratios.items(), key=lambda x: x[1])
                return best if best_r >= 0.35 else "混合・その他"

            _cat_col_exists = "合計_許可病床数" in region_df_staff.columns
            if _cat_col_exists:
                region_df_staff["_hosp_cat"] = region_df_staff.apply(_bed_category, axis=1)
                _hosp_row = region_df_staff[region_df_staff["医療機関名"] == hospital]
                _hosp_cat = _hosp_row["_hosp_cat"].iloc[0] if len(_hosp_row) > 0 else "その他"
                _same_cat = region_df_staff[region_df_staff["_hosp_cat"] == _hosp_cat]
                region_means = _same_cat[metrics].mean()
                _n_same = len(_same_cat)
                _cat_label = f"同カテゴリ平均（{_hosp_cat}・{_n_same}院）"
            else:
                region_means = region_df_staff[metrics].mean()
                _cat_label = f"地域平均（{len(region_df_staff)}院）"

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
                        f"{hv - rv:+.1f}（{_cat_label}比）",
                    )


# ── TAB 6: 病床・手術分析 ──────────────────────────────────

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
                    column_config={"件数（人）": st.column_config.NumberColumn(format="%,d 人")},
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
                    column_config={"件数（人）": st.column_config.NumberColumn(format="%,d 人")},
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
                (ward_df["都道府県名"] == pref) &
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
                    dragmode=False,
                )
                st.plotly_chart(fig_surg, use_container_width=True)

            st.markdown('<div class="section-header">二次医療圏内 手術数シェア</div>', unsafe_allow_html=True)
            # 年度フィルター（複数年度データが混在するとバーが重複するため）
            if "二次医療圏名" in surgery_df.columns:
                _rsurg_mask = surgery_df["二次医療圏名"] == region
                if "都道府県名" in surgery_df.columns:
                    _rsurg_mask = _rsurg_mask & (surgery_df["都道府県名"] == pref)
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
                    dragmode=False,
                )
                st.plotly_chart(fig_share, use_container_width=True)

                tbl = region_surg[["医療機関名", "手術総数", "全身麻酔手術数", "シェア(%)", "全身麻酔率(%)"]].sort_values("手術総数", ascending=False).reset_index(drop=True)
                tbl.index += 1
                st.dataframe(tbl, use_container_width=True, column_config={
                    "手術総数":     st.column_config.NumberColumn(format="%,d 件"),
                    "全身麻酔手術数": st.column_config.NumberColumn(format="%,d 件"),
                })
            else:
                st.info("この二次医療圏の手術データがありません。")


# ── TAB 7: 地図 ─────────────────────────────────────────────

with tab7:
    st.markdown("### 🗺️ 病院マップ")
    st.caption("病院名・都道府県名からジオコーディング（OpenStreetMap）して地図に表示します。初回のみ時間がかかります。")

    try:
        import folium
        from streamlit_folium import st_folium as _st_folium
        from geocoder import (
            geocode_batch, load_cached_coords, count_uncached, has_official_locations,
            geocode_address, haversine_km, osrm_durations,
            load_all_hospital_coords, load_coords_from_parquet,
        )
        _MAP_OK = True
    except ImportError as _e:
        _MAP_OK = False
        st.error(f"地図表示に必要なライブラリが見つかりません: {_e}")

    if _MAP_OK:
        _map_has_db  = DB_PATH.exists()
        _map_has_loc = _LOCS_PARQUET.exists()

        if not _map_has_db and not _map_has_loc:
            st.warning("地図機能は公式座標データ（locations_cache.parquet）またはDuckDBがある場合のみ利用できます。")
        else:
            # ── 座標ソース表示 ──
            if _map_has_db:
                if has_official_locations(str(DB_PATH)):
                    st.success("✅ 厚労省 医療情報ネットの公式座標データ読み込み済み（DuckDB）")
                else:
                    st.info(
                        "💡 **公式座標データを取り込むと精度が大幅に向上します**\n\n"
                        "ローカルで以下を実行してください:\n"
                        "```\npython build_master.py\n```\n"
                    )
            elif _map_has_loc:
                st.success("✅ 厚労省 医療情報ネットの公式座標データ読み込み済み（parquet）")

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

            st.markdown(f"**対象: {map_title} — {len(map_df):,}病院**")

            # ── DB ありの場合のみ geocoding 統計・ボタンを表示 ──
            if _map_has_db:
                n_uncached = count_uncached(str(DB_PATH), map_df["医療機関名"].tolist(), pref)
                n_cached = len(map_df) - n_uncached
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

            # ── 座標を読み込んで地図を描画 ──
            # parquet: 都道府県コード（"01"等）で絞り込み + 施設名を正規化してマッチ
            _pref_code = _PREF_ORDER.get(pref, pref)
            _norm_geo: dict[str, tuple] = {}
            if _map_has_loc:
                try:
                    _lp = pd.read_parquet(
                        str(_LOCS_PARQUET),
                        columns=["施設名", "lat", "lon", "都道府県名"],
                    )
                    _lp = _lp[_lp["都道府県名"] == _pref_code].dropna(subset=["施設名", "lat", "lon"])
                    _lp["_norm"] = _lp["施設名"].apply(_normalize_name)
                    _norm_geo = dict(zip(
                        _lp["_norm"],
                        zip(_lp["lat"].astype(float), _lp["lon"].astype(float)),
                    ))
                except Exception:
                    pass

            # geocache（DB）から名前直接マッチ
            _geo_cache: dict[str, tuple] = {}
            if _map_has_db:
                _geo_cache = load_cached_coords(str(DB_PATH), pref)

            def _lookup_coords(name: str) -> tuple:
                # 1. geocache（DB・高精度）
                if name in _geo_cache:
                    return _geo_cache[name]
                # 2. parquet（正規化名前マッチ）
                norm = _normalize_name(name)
                if norm in _norm_geo:
                    return _norm_geo[norm]
                return (None, None)

            map_df["lat"] = map_df["医療機関名"].map(lambda n: _lookup_coords(n)[0])
            map_df["lon"] = map_df["医療機関名"].map(lambda n: _lookup_coords(n)[1])
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
                        f'<div style="font-family:Meiryo,sans-serif;min-width:200px;line-height:1.6">'
                        f'<b style="font-size:13px">{_r["医療機関名"]}</b><br>'
                        f'<span style="color:#666;font-size:11px">{_r["都道府県名"]} {_r["二次医療圏名"]}</span>'
                        f'<hr style="margin:6px 0">許可病床数: <b>{_beds:,}床</b><br>稼働率: <b>{_occ}</b>'
                        f'<br><a href="#"'
                        f' onclick="window.open(window.top.location.origin+\'/?hospital=\'+encodeURIComponent(\'{_r["医療機関名"]}\'),\'_blank\');return false;"'
                        f' style="display:block;margin-top:10px;padding:7px 12px;'
                        f'background:#12886D;color:#fff;border-radius:8px;'
                        f'text-align:center;text-decoration:none;font-size:12px;font-weight:700;">'
                        f'詳細を見る →</a>'
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

                _last_clicked = st.session_state.get("_map_last_clicked")

                _map_data = _st_folium(
                    _m, width="100%", height=600,
                    returned_objects=["last_object_clicked_tooltip"],
                )

                # ── クリックされたマーカーを session_state に保存 ──
                _clicked_tip = (_map_data or {}).get("last_object_clicked_tooltip") or ""
                if _clicked_tip:
                    _clicked_name = re.sub(r"（[\d,]+床）$", "", _clicked_tip).strip()
                    if _clicked_name and (_clicked_name in map_valid["医療機関名"].values):
                        st.session_state["_map_last_clicked"] = _clicked_name
                        _last_clicked = _clicked_name

                # ── クリック済み病院のバナーを地図の下に表示（地図で検索と統一） ──
                if _last_clicked and (_last_clicked in map_valid["医療機関名"].values):
                    _cr = map_valid[map_valid["医療機関名"] == _last_clicked].iloc[0]
                    _cr_beds = int(_cr.get("合計_許可病床数", 0) or 0)
                    _cr_occ  = f'{_cr["合計稼働率"]:.0f}%' if "合計稼働率" in _cr and pd.notna(_cr.get("合計稼働率")) else "—"
                    st.markdown(
                        f'<div style="margin-top:12px;padding:16px 20px;'
                        f'background:#EAF4F0;border:2px solid #12886D;border-radius:12px;">'
                        f'<div style="font-size:0.75rem;color:#0B6653;font-weight:700;letter-spacing:0.05em;margin-bottom:4px;">選択中の病院</div>'
                        f'<div style="font-size:1rem;font-weight:800;color:#111827;">{_last_clicked}</div>'
                        f'<div style="font-size:0.8rem;color:#6b7280;margin-top:2px;">'
                        f'{_cr["都道府県名"]} {_cr["二次医療圏名"]}　🛏 {_cr_beds:,}床　稼働率 {_cr_occ}</div>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )
                    if st.button("この病院の詳細を見る →", key="map_goto_detail", type="primary", use_container_width=True):
                        st.session_state["_nav_jump"] = {
                            "hospital": _last_clicked,
                            "pref": str(_cr["都道府県名"]),
                            "region": str(_cr["二次医療圏名"]),
                            "year": int(year),
                        }
                        st.session_state.pop("_map_last_clicked", None)
                        st.rerun()

            # ── 2点間距離・所要時間計算 ────────────────────────────
            if not map_valid.empty:
                st.markdown("---")
                st.markdown("### 📍 任意の地点からの距離・所要時間")
                st.caption("住所やランドマークを入力すると、地図内の各病院までの距離と所要時間を計算します。")

                _pt_col1, _pt_col2 = st.columns([3, 1])
                with _pt_col1:
                    _pt_addr = st.text_input(
                        "出発地（住所・ランドマーク）",
                        placeholder="例: 東京都渋谷区渋谷2丁目24",
                        key="map_pt_addr",
                    )
                with _pt_col2:
                    _pt_mode = st.radio(
                        "移動手段",
                        ["車（OSRM）", "公共交通（近似）"],
                        key="map_pt_mode",
                        horizontal=False,
                    )

                if _pt_addr:
                    if st.button("計算する", key="map_pt_calc", type="primary"):
                        st.session_state["_map_pt_cache"] = None  # 再計算

                    _pt_cached = st.session_state.get("_map_pt_cache")
                    _pt_cached_valid = (
                        _pt_cached is not None
                        and _pt_cached.get("addr") == _pt_addr
                        and _pt_cached.get("mode") == _pt_mode
                        and _pt_cached.get("scope") == map_scope
                    )

                    if not _pt_cached_valid:
                        _pt_origin = _cached_geocode_address(_pt_addr)
                        if _pt_origin is None:
                            st.warning(f"⚠️ 「{_pt_addr}」の座標が取得できませんでした。より具体的な住所を入力してください。")
                        else:
                            # map_valid には既に lat/lon がセット済み → 再読み込み不要
                            _pt_rows = []
                            for _, _pr in map_valid.iterrows():
                                _nm = _pr["医療機関名"]
                                _lat_h, _lon_h = _pr.get("lat"), _pr.get("lon")
                                _coords_h = (_lat_h, _lon_h) if pd.notna(_lat_h) and pd.notna(_lon_h) else None
                                _km = haversine_km(_pt_origin[0], _pt_origin[1], *_coords_h) if _coords_h else None
                                _pt_rows.append({"医療機関名": _nm, "直線距離(km)": _km, "_coords": _coords_h})

                            _known_pt = [r for r in _pt_rows if r["_coords"] is not None]
                            if _known_pt and _pt_mode == "車（OSRM）":
                                with st.spinner("OSRM で所要時間を計算中..."):
                                    _durs = osrm_durations(
                                        _pt_origin[0], _pt_origin[1],
                                        [r["_coords"] for r in _known_pt],
                                    )
                                for r, d in zip(_known_pt, _durs):
                                    r["所要時間(分)"] = round(d / 60, 1) if d is not None else None
                            else:
                                for r in _known_pt:
                                    r["所要時間(分)"] = (
                                        round(r["直線距離(km)"] / 25.0 * 60, 1)
                                        if r["直線距離(km)"] is not None else None
                                    )

                            for r in _pt_rows:
                                del r["_coords"]
                                r.setdefault("所要時間(分)", None)

                            _pt_df = (
                                pd.DataFrame(_pt_rows)
                                .sort_values("所要時間(分)")
                                .reset_index(drop=True)
                            )
                            _pt_df.index += 1
                            st.session_state["_map_pt_cache"] = {
                                "addr": _pt_addr, "mode": _pt_mode, "scope": map_scope,
                                "df": _pt_df,
                            }
                            _pt_cached = st.session_state["_map_pt_cache"]
                            _pt_cached_valid = True

                    if _pt_cached_valid and _pt_cached and "df" in _pt_cached:
                        _pt_result = _pt_cached["df"]
                        st.markdown(f"**{len(_pt_result):,}病院 — 出発地: {_pt_addr}**")
                        if _pt_mode == "公共交通（近似）":
                            st.caption("※ 公共交通は直線距離÷25km/hの近似値です")
                        st.dataframe(
                            _pt_result,
                            use_container_width=True,
                            column_config={
                                "直線距離(km)": st.column_config.NumberColumn("直線距離", format="%.1f km"),
                                "所要時間(分)": st.column_config.NumberColumn("所要時間", format="%.1f 分"),
                            },
                        )


# ── TAB DPC: DPC分析 ──────────────────────────────────────

if tab_dpc is not None and _is_dpc and _dpc_ban is not None:
    with tab_dpc:
        _dp_proc_all  = _load_dpc_procedure_stats()
        _dp_ratio_all = _load_dpc_mdc_ratio()
        _dp_readm_all = _load_dpc_readmission()
        _dp_surg_all  = _load_dpc_surgery_detail(_DPC_SURG_MTIME)

        def _dpc_latest(df, ban):
            # 選択中の病床機能報告年度と同じ年度のDPCのみ返す（年度ズレ防止）。
            if df is None:
                return pd.DataFrame()
            sub = df[df["告示番号"] == ban]
            if "年度" in sub.columns:
                sub = sub[sub["年度"] == year]
            return sub

        _dp_proc  = _dpc_latest(_dp_proc_all, _dpc_ban)
        _dp_ratio = _dpc_latest(_dp_ratio_all, _dpc_ban)
        _dp_readm = _dpc_latest(_dp_readm_all, _dpc_ban)
        _dp_surg  = _dpc_latest(_dp_surg_all, _dpc_ban) if _dp_surg_all is not None else pd.DataFrame()

        # ── 基本情報ヘッダー ──
        if _dpc_hosp_row is not None:
            import re as _re_dpc
            _dpc_type      = str(_dpc_hosp_row.get("病院類型", ""))
            _dpc_beds      = _si(_dpc_hosp_row.get("DPC算定病床数", 0))
            _dpc_totbeds   = _si(_dpc_hosp_row.get("病床総数", 0))
            _dpc_ratio_pct = float(_dpc_hosp_row.get("DPC算定病床割合", 0) or 0)
            _dpc_nyuin     = str(_dpc_hosp_row.get("入院基本料", "") or "")
            _dpc_url       = str(_dpc_hosp_row.get("病院指標URL", "") or "")
            _dpc_url       = "" if _dpc_url in ("nan", "None") else _dpc_url.strip()
            _m_since = _re_dpc.search(r"(平成|令和)\d+年度", _dpc_type)
            _since_str = _m_since.group(0) if _m_since else ""

            _badge_html = f"""<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px;align-items:center;">
              <span style="background:#dbeafe;color:#1e40af;border-radius:20px;padding:3px 12px;font-size:0.78rem;font-weight:700;">🏥 DPC参加病院</span>
              {"" if not _since_str else f'<span style="background:#f0fdf4;color:#166534;border-radius:20px;padding:3px 12px;font-size:0.78rem;font-weight:600;">{_since_str}〜</span>'}
              {"" if not _dpc_nyuin or _dpc_nyuin=="nan" else f'<span style="background:#fef9c3;color:#92400e;border-radius:20px;padding:3px 12px;font-size:0.78rem;font-weight:600;">{_dpc_nyuin}</span>'}
              {"" if not _dpc_url else f'<a href="{_dpc_url}" target="_blank" style="background:#f3e8ff;color:#6b21a8;border-radius:20px;padding:3px 12px;font-size:0.78rem;font-weight:600;text-decoration:none;">📊 病院情報公表</a>'}
            </div>"""
            st.markdown(_badge_html, unsafe_allow_html=True)

            _di1, _di2, _di3, _di4 = st.columns(4)
            _di1.markdown(
                f'<div class="metric-card" style="border-top-color:#3b82f6;">'
                f'<div class="metric-label">DPC算定病床数</div>'
                f'<div class="metric-value">{_dpc_beds:,}床</div>'
                f'<div class="metric-sub">病床総数 {_dpc_totbeds:,}床</div></div>',
                unsafe_allow_html=True,
            )
            _di2.markdown(
                f'<div class="metric-card" style="border-top-color:#8b5cf6;">'
                f'<div class="metric-label">DPC算定病床割合</div>'
                f'<div class="metric-value">{_dpc_ratio_pct*100:.1f}%</div>'
                f'<div class="metric-sub">&nbsp;</div></div>',
                unsafe_allow_html=True,
            )
            if not _dp_proc.empty:
                _total_cases = _si(_dp_proc.iloc[0].get("件数_総数", 0))
                _surg_rate   = float(_dp_proc.iloc[0].get("割合_手術有", 0) or 0)
                _readm_rate  = float(_dp_readm.iloc[0].get("再入院率", 0) or 0) if not _dp_readm.empty else 0
                _di3.markdown(
                    f'<div class="metric-card" style="border-top-color:#10b981;">'
                    f'<div class="metric-label">年間DPC算定件数</div>'
                    f'<div class="metric-value">{_total_cases:,}件</div>'
                    f'<div class="metric-sub">&nbsp;</div></div>',
                    unsafe_allow_html=True,
                )
                _di4.markdown(
                    f'<div class="metric-card" style="border-top-color:#f59e0b;">'
                    f'<div class="metric-label">手術実施率</div>'
                    f'<div class="metric-value">{_surg_rate*100:.1f}%</div>'
                    # f'<div class="metric-sub">再入院率 {_readm_rate*100:.2f}%</div></div>',  # 非表示中
                    f'<div class="metric-sub">&nbsp;</div></div>',
                    unsafe_allow_html=True,
                )
        st.markdown("<br>", unsafe_allow_html=True)

        # ── 診療実績（手術・化学療法・放射線・全身麻酔・救急） ──
        if not _dp_proc.empty:
            st.markdown('<div class="section-header">診療実績（手術・化学療法・放射線療法・全身麻酔）</div>', unsafe_allow_html=True)
            _proc_row = _dp_proc.iloc[0]
            _proc_avg = {}
            if _dp_proc_all is not None:
                for _col in ["割合_手術有","割合_化学療法有","割合_放射線療法有","割合_全身麻酔","割合_救急車搬送有"]:
                    if _col in _dp_proc_all.columns:
                        _proc_avg[_col] = float(_dp_proc_all[_col].median())

            _proc_items = [
                ("手術実施率",      "割合_手術有",       "件数_手術有",       "#3b82f6"),
                ("化学療法実施率",  "割合_化学療法有",   "件数_化学療法有",   "#8b5cf6"),
                ("放射線療法実施率","割合_放射線療法有", "件数_放射線療法有", "#f59e0b"),
                ("全身麻酔実施率",  "割合_全身麻酔",     "件数_全身麻酔",     "#10b981"),
                ("救急搬送入院率",  "割合_救急車搬送有", "件数_救急車搬送有", "#ef4444"),
            ]
            _pc1, _pc2, _pc3, _pc4, _pc5 = st.columns(5)
            for _col_ui, (_label, _r_col, _c_col, _color) in zip(
                [_pc1, _pc2, _pc3, _pc4, _pc5], _proc_items
            ):
                _val  = float(_proc_row.get(_r_col, 0) or 0)
                _cnt  = _si(_proc_row.get(_c_col, 0))
                _avg  = _proc_avg.get(_r_col, 0)
                _diff = (_val - _avg) * 100
                _diff_str = f"中央値比 {'▲' if _diff >= 0 else '▼'}{abs(_diff):.1f}pt" if _avg else ""
                _col_ui.markdown(
                    f'<div class="metric-card" style="border-top-color:{_color};">'
                    f'<div class="metric-label">{_label}</div>'
                    f'<div class="metric-value">{_val*100:.1f}%</div>'
                    f'<div class="metric-sub">{_cnt:,}件<br>{_diff_str}</div></div>',
                    unsafe_allow_html=True,
                )

        # ── MDC別患者件数 ──
        _dp_cases_all = _load_dpc_mdc_cases()
        _dp_cases = _dpc_latest(_dp_cases_all, _dpc_ban) if _dp_cases_all is not None else pd.DataFrame()
        if not _dp_cases.empty:
            st.markdown("<br>", unsafe_allow_html=True)
            st.markdown('<div class="section-header">MDC別患者件数</div>', unsafe_allow_html=True)
            _mdc_keys = [k for k in MDC_LABELS if k in _dp_cases.columns]
            if _mdc_keys:
                _noop_row = _dp_cases[_dp_cases["手術有無"] == "無し"]
                _surg_row = _dp_cases[_dp_cases["手術有無"] == "有り"]
                _noop_vals = [int(_noop_row[k].sum()) for k in _mdc_keys]
                _surg_vals = [int(_surg_row[k].sum()) for k in _mdc_keys]
                _total_vals = [n + s for n, s in zip(_noop_vals, _surg_vals)]
                _mdc_names_list = [MDC_LABELS[k] for k in _mdc_keys]

                import plotly.graph_objects as _go_dpc
                _fig_mdc = _go_dpc.Figure()
                _fig_mdc.add_trace(_go_dpc.Bar(
                    x=_surg_vals, y=_mdc_names_list, orientation="h",
                    name="手術あり", marker_color="#f59e0b",
                ))
                _fig_mdc.add_trace(_go_dpc.Bar(
                    x=_noop_vals, y=_mdc_names_list, orientation="h",
                    name="手術なし", marker_color="#3b82f6",
                    text=[f"{t:,}件" for t in _total_vals], textposition="outside",
                ))
                _fig_mdc.update_layout(
                    barmode="stack",
                    height=520,
                    margin=dict(l=10, r=100, t=30, b=20),
                    xaxis_title="件数",
                    legend=dict(orientation="h", y=1.06),
                    font=dict(family="Noto Sans JP, sans-serif", size=12),
                    dragmode=False,
                )
                st.plotly_chart(_fig_mdc, use_container_width=True)

        # ── 再入院・再転棟率 ──
        # 2026年7月に発覚: detect_file_type()の「再入院」文字列部分一致による
        # 誤判定で、無関係な表（予定救急医療入院のMDC別内訳など）が再入院率・
        # 再転棟率として取り込まれ、人数がそのまま率になっていた
        # （最大2640%等の物理的にありえない値）。ヘッダー列名の完全一致判定に
        # 修正し、2022・2023年度は生データから再構築、2024年度は値域[0,1]の
        # 妥当性フィルタで復旧した上で再表示する。
        if not _dp_readm.empty:
            st.markdown("<br>", unsafe_allow_html=True)
            st.markdown('<div class="section-header">再入院・再転棟</div>', unsafe_allow_html=True)
            st.markdown(_source_tag(_dpc_source(year)), unsafe_allow_html=True)
            _readm_row    = _dp_readm.iloc[0]
            _readm_rate   = float(_readm_row.get("再入院率", 0) or 0)
            _retrans_rate = float(_readm_row.get("再転棟率", 0) or 0)
            _readm_med    = float(_dp_readm_all["再入院率"].median()) if _dp_readm_all is not None and "再入院率" in _dp_readm_all.columns else 0
            _retrans_med  = float(_dp_readm_all["再転棟率"].median()) if _dp_readm_all is not None and "再転棟率" in _dp_readm_all.columns else 0
            _ra1, _ra2 = st.columns(2)
            _ra1.markdown(
                f'<div class="metric-card" style="border-top-color:#ef4444;">'
                f'<div class="metric-label">再入院率</div>'
                f'<div class="metric-value">{_readm_rate*100:.2f}%</div>'
                f'<div class="metric-sub">全施設中央値 {_readm_med*100:.2f}%</div></div>',
                unsafe_allow_html=True,
            )
            _ra2.markdown(
                f'<div class="metric-card" style="border-top-color:#f97316;">'
                f'<div class="metric-label">再転棟率</div>'
                f'<div class="metric-value">{_retrans_rate*100:.3f}%</div>'
                f'<div class="metric-sub">全施設中央値 {_retrans_med*100:.3f}%</div></div>',
                unsafe_allow_html=True,
            )
            st.caption("症例数が少ない病院では母数が小さく、率が大きく振れやすい点にご留意ください。")
            _period_cols = ["再入院_3日以内","再入院_4-7日","再入院_8-14日","再入院_15-28日"]
            _period_labels = ["3日以内","4〜7日","8〜14日","15〜28日"]
            _period_vals = [float(_readm_row.get(c, 0) or 0) * 100 for c in _period_cols if c in _readm_row.index]
            if len(_period_vals) == 4 and any(v > 0 for v in _period_vals):
                import plotly.graph_objects as _go_readm
                _fig_r = _go_readm.Figure(_go_readm.Bar(
                    x=_period_labels, y=_period_vals,
                    marker_color=["#fca5a5","#fb923c","#fbbf24","#a3e635"],
                    text=[f"{v:.1f}%" for v in _period_vals], textposition="outside",
                ))
                _fig_r.update_layout(
                    title="再入院 期間別内訳（再入院例中の割合）",
                    yaxis_title="%", height=280,
                    margin=dict(l=10, r=10, t=40, b=20),
                    font=dict(family="Noto Sans JP, sans-serif", size=12),
                    dragmode=False,
                )
                st.plotly_chart(_fig_r, use_container_width=True)

        # ── 主要疾患・手術 TOP20 ──
        if not _dp_surg.empty:
            st.markdown("<br>", unsafe_allow_html=True)
            st.markdown('<div class="section-header">疾患別手術件数（MDC別）</div>', unsafe_allow_html=True)
            _cnt_col  = next((c for c in _dp_surg.columns if "件数" in c and "総計" in c), None)
            _surg_col = next((c for c in _dp_surg.columns if "件数" in c and "手術" in c), None)
            _los_col  = next((c for c in _dp_surg.columns if "在院" in c and "総計" in c), None)
            if _cnt_col:
                _sdisp = _dp_surg[["疾患名","MDC","dpc6"] + [c for c in [_cnt_col,_surg_col,_los_col] if c]].copy()
                _sdisp = _sdisp.dropna(subset=[_cnt_col])
                _sdisp = _sdisp[_sdisp[_cnt_col] > 0].copy()
                # 件数_総計=code99=手術なし、真の総計=手術なし+手術あり
                if _surg_col and _surg_col in _sdisp.columns:
                    _sdisp["_真の総計"] = (_sdisp[_cnt_col].fillna(0) + _sdisp[_surg_col].fillna(0)).astype(int)
                    _sdisp["手術実施率"] = (
                        _sdisp[_surg_col] / _sdisp["_真の総計"].replace(0, float("nan"))
                    ).map(lambda x: f"{x*100:.1f}%" if pd.notna(x) else "-")
                    _disp_cnt_col = "_真の総計"
                else:
                    _sdisp[_cnt_col] = _sdisp[_cnt_col].astype(int)
                    _disp_cnt_col = _cnt_col
                if _los_col:
                    _sdisp["平均在院日数"] = _sdisp[_los_col].map(lambda x: f"{x:.1f}日" if pd.notna(x) else "-")
                _sdisp = _sdisp.rename(columns={_disp_cnt_col: "件数", "dpc6": "DPC6桁コード"})
                _show = ["DPC6桁コード","疾患名","件数"]
                if "手術実施率" in _sdisp.columns: _show.append("手術実施率")
                if "平均在院日数" in _sdisp.columns: _show.append("平均在院日数")

                # MDC順に並べ、MDCごとにexpanderで折り畳み表示
                for _mdc_key in [k for k in MDC_LABELS if k in _sdisp["MDC"].values]:
                    _grp = _sdisp[_sdisp["MDC"] == _mdc_key].sort_values("件数", ascending=False)
                    _mdc_label = MDC_LABELS[_mdc_key]
                    _total_cnt = _grp["件数"].sum()
                    with st.expander(f"{_mdc_key} {_mdc_label}　{len(_grp)}疾患 / 計{_total_cnt:,}件", expanded=False):
                        st.dataframe(
                            _grp[[c for c in _show if c in _grp.columns]],
                            use_container_width=True, hide_index=True,
                            column_config={"件数": st.column_config.NumberColumn("件数", format="%d件")},
                        )


_render_footer()
