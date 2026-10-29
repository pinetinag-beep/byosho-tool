"""施設基準届出データ（shisetsu_kijun_cache.parquet）を検索サイト用のSQLiteに変換する。

月1回の更新の流れ:
  1. ユーザーが各地方厚生局の最新ファイルを shisetsu_raw/ に置いてpush
  2. リポジトリ直下で build_shisetsu_kijun.py --dir shisetsu_raw/ → shisetsu_kijun_cache.parquet を更新
  3. このスクリプトで kijun_web/data/kijun.sqlite を作り直す（サーバー上で実行）

    python kijun_web/build_db.py

作り直したDBは一時ファイルに書いてから置き換えるので、稼働中のサイトが
書き込み途中のDBを読むことはない。直前のDBは kijun.prev.sqlite として残す
（翌月以降「今月の新規届出・取り下げ」を出すための比較元にする予定）。
"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import re
import sqlite3
import sys
import time
import unicodedata
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SRC = ROOT / "shisetsu_kijun_cache.parquet"
BYOSHO = ROOT / "data_cache.parquet"
WARDS = ROOT / "ward_cache.parquet"
GAIRAI = ROOT / "gairai_form1_annual.parquet"   # 市区町村→二次医療圏の対応に使う（外来機能報告）
ABBR = HERE / "abbr_order.json"                 # 受理記号の分類と並び順（make_abbr_order.py が作る）
OUT = HERE / "data" / "kijun.sqlite"

# 検索フォームで届出項目を並べる区分（略称一覧の見出しと同じ順）
SECTION_ORDER = ["基本診療料", "特掲診療料", "入院時食事療養", "その他の届出", "先進医療"]
AREA_UNKNOWN = "（二次医療圏不明）"

# 政令指定都市は区まで含めて市区町村とする（外来機能報告の市区町村名称と同じ単位）
_SEIREI = ("札幌市 仙台市 さいたま市 千葉市 横浜市 川崎市 相模原市 新潟市 静岡市 浜松市 名古屋市 "
           "京都市 大阪市 堺市 神戸市 岡山市 広島市 北九州市 福岡市 熊本市").split()

# 北海道・関東信越・近畿の厚生局は入院基本料の区分（急性期一般入院料１など）を公表していない。
# 病床機能報告の病棟ごとの入院基本料から、届出名称ごとに参考値として補う（Streamlit版の病院ページと同じ対応）。
# 精神病棟・結核病棟・有床診療所は病床機能報告に無いため補えない。
KUBUN_REF_KEYWORDS = {
    "一般病棟入院基本料": ["急性期一般入院料", "地域一般入院料", "一般病棟特別入院基本料"],
    "療養病棟入院基本料": ["療養病棟入院料", "療養病棟特別入院基本料"],
    "障害者施設等入院基本料": ["障害者施設等"],
    "特定機能病院入院基本料": ["特定機能病院"],
    "専門病院入院基本料": ["専門病院"],
}

_ERAS = {"明治": 1867, "大正": 1911, "昭和": 1925, "平成": 1988, "令和": 2018}
_WAREKI = re.compile(r"(明治|大正|昭和|平成|令和)\s*(元|\d+)\s*年\s*(\d+)\s*月\s*(\d+)\s*日")

# build_gakkai_nintei.py の _normalize と同じ考え方（法人格プレフィックス除去＋正規化）。
# あちらはPDF解析用ライブラリを読み込むため、サーバーで動かすこのスクリプトには写して持つ。
_LEGAL_PREFIXES = [
    "独立行政法人国立病院機構", "国家公務員共済組合連合会", "地方独立行政法人",
    "社会医療法人財団", "社会医療法人", "国立大学法人", "公立大学法人",
    "医療法人社団", "医療法人財団", "公益財団法人", "一般財団法人",
    "公益社団法人", "一般社団法人", "社会福祉法人", "特定医療法人", "医療法人",
    "学校法人", "宗教法人",
]

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE prefs (
    code TEXT PRIMARY KEY, name TEXT NOT NULL, as_of TEXT, as_of_label TEXT,
    n_facilities INTEGER, n_filings INTEGER
);
CREATE TABLE facilities (
    id TEXT PRIMARY KEY,          -- 都道府県コード(2桁)+医療機関番号(7桁)
    pref_code TEXT NOT NULL, pref_name TEXT NOT NULL, code TEXT NOT NULL,
    name TEXT NOT NULL, name_search TEXT NOT NULL,
    address TEXT, address_search TEXT,
    ftype TEXT, n_kijun INTEGER,
    byosho_name TEXT,             -- 病床機能報告側の医療機関名（Streamlit版へのリンク用、突合できた場合のみ）
    byosho_code TEXT,             -- 病床機能報告の医療機関コード（同上）
    city TEXT,                    -- 住所から取った市区町村（政令市は区まで）
    area TEXT                     -- 二次医療圏（外来機能報告の市区町村との対応から）
);
CREATE TABLE areas (              -- 検索フォームの 都道府県→二次医療圏→市区町村
    pref_code TEXT NOT NULL, area TEXT NOT NULL, area_order INTEGER, city TEXT NOT NULL,
    city_order TEXT, n INTEGER
);
CREATE TABLE items (              -- 検索フォームの届出項目（届出名称×受理記号。同じ名称で記号が違う＝外来/入院など）
    id INTEGER PRIMARY KEY, kijun_id INTEGER NOT NULL, symbol TEXT NOT NULL,
    section TEXT NOT NULL, sort_key REAL NOT NULL, n_facilities INTEGER
);
CREATE TABLE kijun (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, name_search TEXT NOT NULL,
    symbol TEXT, n_facilities INTEGER
);
CREATE TABLE filings (
    facility_id TEXT NOT NULL, kijun_id INTEGER NOT NULL,
    symbol TEXT, juri_no TEXT, start_date TEXT, start_raw TEXT,
    byoto TEXT, byosho_kubun TEXT, n_byoto TEXT, n_byosho TEXT, kubun TEXT, other TEXT,
    kubun_ref TEXT,               -- kubun が空のときだけ入る、病床機能報告からの参考値
    item_id INTEGER
);
CREATE TABLE kijun_pref (kijun_id INTEGER NOT NULL, pref_code TEXT NOT NULL, n INTEGER NOT NULL);
"""

INDEXES = """
CREATE INDEX ix_fac_pref ON facilities(pref_code, ftype);
CREATE INDEX ix_fil_fac ON filings(facility_id);
CREATE INDEX ix_fil_kijun ON filings(kijun_id, facility_id);
CREATE INDEX ix_fil_item ON filings(item_id, facility_id);
CREATE INDEX ix_fac_area ON facilities(pref_code, area, city);
CREATE INDEX ix_areas ON areas(pref_code);
CREATE INDEX ix_kp ON kijun_pref(kijun_id, pref_code);
"""


def search_key(s) -> str:
    """検索用の正規化（全角半角・ローマ数字Ⅰ/Ｉ等の表記ゆれを吸収し、空白を除いて小文字化）。
    app.py の検索語にも同じ関数を使うこと。"""
    if not isinstance(s, str):
        return ""
    return re.sub(r"[\s　]", "", unicodedata.normalize("NFKC", s)).lower()


def wareki_to_iso(s) -> str:
    """「令和 8年 6月 1日」→「2026-06-01」。読めなければ空文字。"""
    if not isinstance(s, str):
        return ""
    m = _WAREKI.search(unicodedata.normalize("NFKC", s))
    if not m:
        return ""
    era, y, mo, d = m.groups()
    year = _ERAS[era] + (1 if y == "元" else int(y))
    return f"{year:04d}-{int(mo):02d}-{int(d):02d}"


def as_of_key(s: str) -> tuple[str, str]:
    """データの時点（年月列）を (並べ替え用キー, 表示用ラベル) にする。

    全件スナップショット由来は「2026-05」、差分PDFで更新した県は
    「令和8年6月29日」（差分の公表日）という別の書式で入っている。
    """
    s = str(s or "").strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})", s)
    if m:
        return f"{m[1]}-{int(m[2]):02d}", f"{m[1]}年{int(m[2])}月"
    iso = wareki_to_iso(s)
    if iso:
        y, mo, d = iso.split("-")
        return iso, f"{y}年{int(mo)}月{int(d)}日"
    return "", s


def _normalize_hospital(name) -> str:
    if not isinstance(name, str):
        return ""
    name = unicodedata.normalize("NFKC", name).strip()
    for prefix in _LEGAL_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    return re.sub(r"[\s　]", "", name).lower()


def _byosho_link(fac: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """病院・有床診療所を病床機能報告に突合し、(医療機関コード, 最新年度の医療機関名) を返す
    （Streamlit版の病院ページへのリンクと、区分の参考値に使う）。

    完全一致 → 同一都道府県内のサフィックス一致（4文字以上同士）の順。
    学会認定施設の突合（build_gakkai_nintei.py）と同じ方式。
    """
    if not BYOSHO.exists():
        none = pd.Series([None] * len(fac), index=fac.index)
        return none, none
    bd = pd.read_parquet(BYOSHO, columns=["都道府県名", "医療機関名", "医療機関コード", "報告年度"])
    bd["医療機関コード"] = bd["医療機関コード"].astype(str)
    bd = bd.sort_values("報告年度")
    latest_name = bd.groupby("医療機関コード")["医療機関名"].last()
    exact, by_pref = {}, {}
    for pref, name, code in bd[["都道府県名", "医療機関名", "医療機関コード"]].drop_duplicates().itertuples(index=False):
        norm = _normalize_hospital(name)
        exact[(pref, norm)] = code
        by_pref.setdefault(pref, []).append((norm, code))

    def match(pref, name, ftype):
        if ftype not in ("病院", "有床診療所"):
            return None
        norm = _normalize_hospital(name)
        code = exact.get((pref, norm))
        if code is None and len(norm) >= 4:
            for cand, c in by_pref.get(pref, []):
                if len(cand) >= 4 and (cand.endswith(norm) or norm.endswith(cand)):
                    code = c
                    break
        return code

    codes = pd.Series(
        [match(p, n, t) for p, n, t in fac[["pref_name", "name", "ftype"]].itertuples(index=False)],
        index=fac.index, dtype=object,
    )
    return codes, codes.map(latest_name)


def _kubun_ref() -> tuple[dict[tuple[str, str], str], int | None]:
    """病床機能報告（最新年度）の病棟ごとの入院基本料から、(医療機関コード, 受理届出名称) → 区分 の参考値を作る。
    病棟によって区分が違う病院は「・」で連結する。2つ目の戻り値は報告年度。"""
    if not WARDS.exists():
        return {}, None
    w = pd.read_parquet(WARDS, columns=["医療機関コード", "入院基本料", "報告年度"])
    year = int(w["報告年度"].max())
    w = w[w["報告年度"] == year]
    ref: dict[tuple[str, str], str] = {}
    for code, g in w.groupby(w["医療機関コード"].astype(str)):
        vals = {str(v) for v in g["入院基本料"].dropna().unique()}
        for kijun, kws in KUBUN_REF_KEYWORDS.items():
            hit = sorted(v for v in vals if any(k in v for k in kws))
            if hit:
                ref[(code, kijun)] = "・".join(hit)
    # 病床機能報告は各年度7月1日時点の病棟の状況を報告するもの
    return ref, year


def _place_key(s) -> str:
    """住所・市区町村名の照合用（全角半角・空白・「ヶ/ケ」の表記ゆれを吸収）。"""
    return re.sub(r"\s", "", unicodedata.normalize("NFKC", str(s))).replace("ヶ", "ケ")


def _city_from_address(addr: str) -> str | None:
    """外来機能報告に無い（＝報告対象の施設が無い）町村などは、住所の先頭から市区町村名を取り出す。"""
    for c in _SEIREI:
        if addr.startswith(c):
            m = re.match(re.escape(c) + r".+?区", addr)
            return m.group(0) if m else c
    m = re.match(r"(.+?郡)(.+?[町村])", addr)
    if m and not re.search(r"[市区]", m.group(2)):   # 「大和郡山市…」を郡と取り違えない
        return m.group(0)
    for c in ("四日市市", "廿日市市", "野々市市"):     # 名前の途中に「市」がある市
        if addr.startswith(c):
            return c
    m = re.match(r".+?[市区町村]", addr)
    return m.group(0) if m else None


def _city_area(fac: pd.DataFrame) -> tuple[pd.Series, pd.Series, pd.DataFrame]:
    """医療機関の住所から市区町村と二次医療圏を決める。

    二次医療圏は外来機能報告（最新年度）の「市区町村名称→二次医療圏名」の対応を使う（2026年10月で
    医療機関の98.8%が一致）。そこに無い町村は住所から市区町村名を取り出し、同じ郡の町村がすべて
    同じ二次医療圏なら郡から決める。決まらないものは「二次医療圏不明」にする。
    3つ目の戻り値は検索フォーム用の 都道府県×二次医療圏×市区町村 の一覧（並び順つき）。"""
    if GAIRAI.exists():
        g = pd.read_parquet(GAIRAI, columns=["都道府県コード", "二次医療圏コード", "二次医療圏名",
                                             "市区町村コード", "市区町村名称", "報告年度"])
        g = g[g["報告年度"] == g["報告年度"].max()].astype(str)
        g["pref"] = g["都道府県コード"].str.zfill(2)
        mode = lambda s: s.value_counts().index[0]
        tbl = (g.groupby(["pref", "市区町村名称"])
                .agg(area=("二次医療圏名", mode), area_code=("二次医療圏コード", mode), city_code=("市区町村コード", "min"))
                .reset_index())
    else:
        tbl = pd.DataFrame(columns=["pref", "市区町村名称", "area", "area_code", "city_code"])
    info = {(r.pref, r.市区町村名称): (r.area, r.area_code, r.city_code) for r in tbl.itertuples(index=False)}
    names: dict[str, list[tuple[str, str]]] = {}
    for p, c in zip(tbl["pref"], tbl["市区町村名称"]):
        names.setdefault(p, []).append((_place_key(c), c))
    for p in names:
        names[p].sort(key=lambda x: len(x[0]), reverse=True)
    area_code_of = {(p, a): ac for (p, _), (a, ac, _) in info.items()}

    def find(pref: str, addr: str) -> str | None:
        cands = names.get(pref, [])
        for key, name in cands:
            if addr.startswith(key) or ("郡" in key and addr.startswith(key.split("郡", 1)[1])):
                return name
        if "郡" in addr[:10]:   # 住所には郡があり、市区町村名称には郡が無い
            rest = addr.split("郡", 1)[1]
            for key, name in cands:
                if "郡" not in key and rest.startswith(key):
                    return name
        return None

    # 1巡目: 外来機能報告の市区町村名称と照合。外来機能報告の名称には郡が付かないので、
    # 照合できた施設の住所から「郡→二次医療圏」の対応も集めておく
    cities, areas, gun_areas = [], [], {}
    for pref, pname, address in fac[["pref_code", "pref_name", "address"]].itertuples(index=False):
        addr = _place_key(address or "")
        if addr.startswith(pname):        # 住所に都道府県名から書いてある施設がある
            addr = addr[len(pname):]
        city = find(pref, addr)
        if city:
            area = info[(pref, city)][0]
            if "郡" in addr[:10]:
                gun_areas.setdefault((pref, addr.split("郡", 1)[0] + "郡"), set()).add(area)
        else:
            city = _city_from_address(addr) or "（市区町村不明）"
            area = None
        cities.append(city)
        areas.append(area)
    # 2巡目: 照合できなかった町村は、同じ郡の町村がすべて同じ二次医療圏ならそれを使う
    for i, (pref, city) in enumerate(zip(fac["pref_code"], cities)):
        if areas[i] is None:
            same = gun_areas.get((pref, city.split("郡", 1)[0] + "郡"), set()) if "郡" in city else set()
            areas[i] = next(iter(same)) if len(same) == 1 else AREA_UNKNOWN
            if "郡" in city:   # 外来機能報告の書き方（郡を付けない）にそろえる
                cities[i] = city.split("郡", 1)[1]
    city_s = pd.Series(cities, index=fac.index)
    area_s = pd.Series(areas, index=fac.index)

    a = pd.DataFrame({"pref_code": fac["pref_code"], "area": area_s, "city": city_s})
    a = a.groupby(["pref_code", "area", "city"]).size().reset_index(name="n")
    a["area_order"] = [int(area_code_of.get((p, ar), 999999)) if str(area_code_of.get((p, ar), "")).isdigit()
                       else 999999 for p, ar in zip(a["pref_code"], a["area"])]
    a["city_order"] = [str(info.get((p, c), (None, None, "99999"))[2]) + c for p, c in zip(a["pref_code"], a["city"])]
    return city_s, area_s, a


def _items(head: pd.DataFrame) -> pd.DataFrame:
    """検索フォームの届出項目（届出名称×受理記号）を、略称一覧の分類・並び順で返す。

    略称一覧（abbr_order.json）に無い記号は、各医療機関の届出の並び順から位置を推定する。
    地方厚生局の名簿は1医療機関の届出を略称一覧と同じ順（基本診療料→特掲診療料…）で載せているので、
    直前に載っている「一覧にある記号」の位置＋0.5（無ければ直後の記号の位置−0.5）の中央値を使う。"""
    abbr = json.loads(ABBR.read_text(encoding="utf-8")) if ABBR.exists() else []
    known: dict[str, tuple[int, str]] = {}
    for i, a in enumerate(abbr):
        known.setdefault(_place_key(a["symbol"]), (i, a["section"]))
    sym = head["受理記号"]
    sym_cats = list(sym.cat.categories)
    cat_order = np.array([known.get(_place_key(s), (np.nan, None))[0] for s in sym_cats] + [np.nan], dtype=float)
    codes = sym.cat.codes.to_numpy()
    order = pd.Series(cat_order[codes])            # code -1（記号なし）は末尾の NaN を指す
    fid = head["facility_id"].cat.codes.to_numpy()
    prev = order.groupby(fid).ffill().groupby(fid).shift(1)
    nxt = order.groupby(fid).bfill().groupby(fid).shift(-1)
    est = order.fillna(prev + 0.5).fillna(nxt - 0.5)

    df = pd.DataFrame({"k": head["受理届出名称"].cat.codes.to_numpy(), "s": codes, "fid": fid, "est": est})
    it = df.groupby(["k", "s"]).agg(sort_key=("est", "median"), n_facilities=("fid", "nunique")).reset_index()
    k_cats = list(head["受理届出名称"].cat.categories)
    it["kijun"] = [k_cats[k] for k in it["k"]]
    it["symbol"] = [sym_cats[s] if s >= 0 else "" for s in it["s"]]

    anchors = sorted((o, sec) for o, sec in known.values())
    anchor_pos = [o for o, _ in anchors]

    def section_of(symbol: str, key: float) -> str:
        if _place_key(symbol) in known:
            return known[_place_key(symbol)][1]
        if pd.isna(key) or not anchors:
            return "その他の届出"
        i = bisect.bisect_right(anchor_pos, key) - 1
        return anchors[max(i, 0)][1]

    it["section"] = [section_of(s, k) for s, k in zip(it["symbol"], it["sort_key"])]
    it["sort_key"] = it["sort_key"].fillna(1e9)
    it["sec_order"] = it["section"].map({s: i for i, s in enumerate(SECTION_ORDER)}).fillna(len(SECTION_ORDER))
    it = it.sort_values(["sec_order", "sort_key", "symbol"]).reset_index(drop=True)
    it["id"] = it.index + 1
    # 品質確認用: 一覧にある記号同士で、名簿の並びが一覧の順と合っている割合
    known_rows = order.notna()
    it.attrs["order_agreement"] = float(((order >= prev) | prev.isna())[known_rows].mean()) if known_rows.any() else 0.0
    return it[["id", "kijun", "symbol", "section", "sort_key", "n_facilities"]]


def build(src: Path = SRC, out: Path = OUT) -> dict:
    """メモリを抑えるため、集計用の列だけを辞書エンコード（category）のまま読み、
    届出行はparquetから10万行ずつ読んでDBに流し込む（全列を一度に文字列で
    展開する素直な実装ではピーク1.3GBになり、1GBのサーバーで動かなかった）。"""
    t0 = time.time()
    head_cols = ["都道府県コード", "都道府県名", "医療機関番号", "医療機関名称", "住所",
                 "受理届出名称", "受理記号", "施設種別", "年月"]
    head = pq.read_table(src, columns=head_cols, read_dictionary=head_cols).to_pandas()
    head["facility_id"] = (
        head["都道府県コード"].astype(str).str.zfill(2) + head["医療機関番号"].astype(str)
    ).astype("category")

    # ── 都道府県 ──
    ym = pd.DataFrame({"年月": head["年月"].cat.categories})
    ym["key"], ym["label"] = zip(*ym["年月"].map(as_of_key))
    head["_as_of"] = head["年月"].map(dict(zip(ym["年月"], ym["key"]))).astype(str)
    label_of = dict(zip(ym["key"], ym["label"]))
    prefs = (
        head.groupby(["都道府県コード", "都道府県名"], observed=True)
        .agg(as_of=("_as_of", "max"), n_facilities=("facility_id", "nunique"), n_filings=("facility_id", "size"))
        .reset_index()
    )
    prefs["as_of_label"] = prefs["as_of"].map(label_of)

    # ── 医療機関 ──
    fac = (
        head.groupby("facility_id", observed=True)
        .agg(pref_code=("都道府県コード", "first"), pref_name=("都道府県名", "first"),
             code=("医療機関番号", "first"), name=("医療機関名称", "first"),
             address=("住所", "first"), ftype=("施設種別", "first"),
             n_kijun=("受理届出名称", "nunique"))
        .reset_index()
    )
    for c in fac.columns:
        if isinstance(fac[c].dtype, pd.CategoricalDtype):
            fac[c] = fac[c].astype(str)
    fac["name_search"] = fac["name"].map(search_key)
    fac["address_search"] = fac["address"].map(search_key)
    fac["byosho_code"], fac["byosho_name"] = _byosho_link(fac)
    code_of = fac.set_index("facility_id")["byosho_code"].dropna().to_dict()
    kubun_ref, kubun_ref_year = _kubun_ref()
    fac["city"], fac["area"], areas = _city_area(fac)

    # ── 施設基準 ──
    kj = (
        head.groupby("受理届出名称", observed=True)
        .agg(n_facilities=("facility_id", "nunique"))
        .reset_index()
    )
    sym = (
        head.groupby(["受理届出名称", "受理記号"], observed=True).size()
        .reset_index(name="n").sort_values("n", ascending=False)
        .drop_duplicates("受理届出名称").set_index("受理届出名称")["受理記号"]
    )
    kj["受理届出名称"] = kj["受理届出名称"].astype(str)
    kj["symbol"] = kj["受理届出名称"].map(sym.rename(index=str)).astype(str)
    kj = kj.sort_values(["n_facilities", "受理届出名称"], ascending=[False, True]).reset_index(drop=True)
    kj["id"] = kj.index + 1
    kj["name_search"] = kj["受理届出名称"].map(search_key)
    kid = dict(zip(kj["受理届出名称"], kj["id"]))

    head["kijun_id"] = head["受理届出名称"].astype(str).map(kid)
    kp = (
        head.groupby(["kijun_id", "都道府県コード"], observed=True)["facility_id"].nunique()
        .reset_index(name="n")
    )
    kp["都道府県コード"] = kp["都道府県コード"].astype(str)
    items = _items(head)
    items["kijun_id"] = items["kijun"].map(kid)
    item_of = {(k, s): i for i, k, s in items[["id", "kijun", "symbol"]].itertuples(index=False)}
    del head

    # ── 書き出し ──
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    if tmp.exists():
        tmp.unlink()
    con = sqlite3.connect(tmp)
    con.executescript(SCHEMA)
    con.execute("CREATE TEMP TABLE filings_raw AS SELECT * FROM filings WHERE 0")
    con.executemany(
        "INSERT INTO prefs VALUES (?,?,?,?,?,?)",
        prefs[["都道府県コード", "都道府県名", "as_of", "as_of_label", "n_facilities", "n_filings"]]
        .astype(object).itertuples(index=False, name=None),
    )
    con.executemany(
        "INSERT INTO facilities VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        fac[["facility_id", "pref_code", "pref_name", "code", "name", "name_search",
             "address", "address_search", "ftype", "n_kijun", "byosho_name", "byosho_code", "city", "area"]]
        .astype(object).where(fac.notna(), None).itertuples(index=False, name=None),
    )
    con.executemany(
        "INSERT INTO areas VALUES (?,?,?,?,?,?)",
        areas[["pref_code", "area", "area_order", "city", "city_order", "n"]].astype(object).itertuples(index=False, name=None),
    )
    con.executemany(
        "INSERT INTO items VALUES (?,?,?,?,?,?)",
        items[["id", "kijun_id", "symbol", "section", "sort_key", "n_facilities"]].astype(object).itertuples(index=False, name=None),
    )
    con.executemany(
        "INSERT INTO kijun VALUES (?,?,?,?,?)",
        kj[["id", "受理届出名称", "name_search", "symbol", "n_facilities"]]
        .astype(object).itertuples(index=False, name=None),
    )
    cols = ["都道府県コード", "医療機関番号", "受理届出名称", "受理記号", "受理番号", "算定開始年月日",
            "病棟種別", "病床区分", "病棟数", "病床数", "区分", "内訳その他"]
    for batch in pq.ParquetFile(src).iter_batches(batch_size=100_000, columns=cols):
        b = batch.to_pandas()
        for c in b.columns:
            b[c] = b[c].astype(object).where(b[c].notna(), "").astype(str)
        b["facility_id"] = b["都道府県コード"].str.zfill(2) + b["医療機関番号"]
        b["kijun_id"] = b["受理届出名称"].map(kid)
        b["start_date"] = b["算定開始年月日"].map(wareki_to_iso)
        b["kubun_ref"] = [
            kubun_ref.get((code_of.get(fid), kj), "") if not kb and kj in KUBUN_REF_KEYWORDS else ""
            for fid, kj, kb in b[["facility_id", "受理届出名称", "区分"]].itertuples(index=False)
        ]
        b["item_id"] = [item_of.get(key) for key in zip(b["受理届出名称"], b["受理記号"])]
        con.executemany(
            "INSERT INTO filings_raw VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            b[["facility_id", "kijun_id", "受理記号", "受理番号", "start_date", "算定開始年月日",
               "病棟種別", "病床区分", "病棟数", "病床数", "区分", "内訳その他", "kubun_ref", "item_id"]]
            .astype(object).itertuples(index=False, name=None),
        )
    # 元データには完全に同一の行が数百件あるので、ここで重複を除く
    con.execute("INSERT INTO filings SELECT DISTINCT * FROM filings_raw")
    con.execute("DROP TABLE filings_raw")
    n_filings = con.execute("SELECT COUNT(*) FROM filings").fetchone()[0]
    con.executemany(
        "INSERT INTO kijun_pref VALUES (?,?,?)",
        kp[["kijun_id", "都道府県コード", "n"]].astype(object).itertuples(index=False, name=None),
    )
    stats = {
        "built_at": time.strftime("%Y-%m-%d %H:%M"),
        "n_facilities": str(len(fac)),
        "n_filings": str(n_filings),
        "n_kijun": str(len(kj)),
        "n_byosho_linked": str(int(fac["byosho_name"].notna().sum())),
        "n_kubun_ref": str(con.execute("SELECT COUNT(*) FROM filings WHERE kubun_ref != ''").fetchone()[0]),
        "n_items": str(len(items)),
        "items_in_abbr": f"{items['symbol'].map(_place_key).isin({_place_key(a['symbol']) for a in json.loads(ABBR.read_text(encoding='utf-8'))}).mean():.3f}" if ABBR.exists() else "0",
        "item_order_agreement": f"{items.attrs.get('order_agreement', 0):.3f}",
        "area_unknown_facilities": str(int((fac["area"] == AREA_UNKNOWN).sum())),
        "kubun_ref_date": f"{kubun_ref_year}-07-01" if kubun_ref_year else "",
        "kubun_ref_label": f"{kubun_ref_year}年7月1日時点" if kubun_ref_year else "",
        "as_of_min": prefs["as_of"].min(),
        "as_of_max": prefs["as_of"].max(),
        "as_of_max_label": label_of.get(prefs["as_of"].max(), ""),
    }
    con.executemany("INSERT INTO meta VALUES (?,?)", stats.items())
    con.executescript(INDEXES)
    con.commit()
    con.execute("VACUUM")
    con.close()

    if out.exists():
        os.replace(out, out.with_name("kijun.prev.sqlite"))
    os.replace(tmp, out)
    stats["seconds"] = f"{time.time() - t0:.1f}"
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--src", type=Path, default=SRC)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()
    stats = build(args.src, args.out)
    for k, v in stats.items():
        print(f"{k}: {v}")
    print(f"→ {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
