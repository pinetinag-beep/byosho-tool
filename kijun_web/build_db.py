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

try:  # kijun_web.build_db として読まれる場合と、python kijun_web/build_db.py で動かす場合の両方
    from .sources import BUREAU_OF_PREF
except ImportError:
    from sources import BUREAU_OF_PREF

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SRC = ROOT / "shisetsu_kijun_cache.parquet"
BYOSHO = ROOT / "data_cache.parquet"
WARDS = ROOT / "ward_cache.parquet"
FETCHED = ROOT / "shisetsu_raw" / "fetched.json"
GAIRAI = ROOT / "gairai_form1_annual.parquet"
GAIRAI2 = ROOT / "gairai_form2.parquet"
DPC_HOSP = ROOT / "dpc_hospitals.parquet"
DPC_MATCH = ROOT / "dpc_match.parquet"
DPC_PROC = ROOT / "dpc_procedure_stats.parquet"
DPC_MDC = ROOT / "dpc_mdc_cases.parquet"
GAIRAI = ROOT / "gairai_form1_annual.parquet"   # 市区町村→二次医療圏の対応に使う（外来機能報告）
ABBR = HERE / "abbr_order.json"                 # 受理記号の分類と並び順（make_abbr_order.py が作る）
OUT = HERE / "data" / "kijun.sqlite"
YAKKYOKU = ROOT / "yakkyoku_cache.parquet"                  # 薬局の届出（build_yakkyoku.py が作る）
YAKKYOKU_FETCHED = ROOT / "shisetsu_raw_yakkyoku" / "fetched.json"
SHIKA = ROOT / "shika_cache.parquet"                        # 歯科の届出（build_shika.py が作る）
SHIKA_FETCHED = ROOT / "shisetsu_raw_shika" / "fetched.json"
KDIFF = ROOT / "kijun_diff.parquet"                         # 毎月の新規届出・取り下げ（build_kijun_diff.py が作る）

# 検索フォームで届出項目を並べる区分（略称一覧の見出しと同じ順）
SECTION_ORDER = ["基本診療料", "特掲診療料", "入院時食事療養", "その他の届出", "先進医療"]
AREA_UNKNOWN = "（二次医療圏不明）"

# 政令指定都市は区まで含めて市区町村とする（外来機能報告の市区町村名称と同じ単位）
_SEIREI = ("札幌市 仙台市 さいたま市 千葉市 横浜市 川崎市 相模原市 新潟市 静岡市 浜松市 名古屋市 "
           "京都市 大阪市 堺市 神戸市 岡山市 広島市 北九州市 福岡市 熊本市").split()

# 区分（急性期一般入院料１など）が公表されていない届出（関東信越・近畿・北海道が届出項目別の名簿で埋まり、
# 2026年11月時点では全国で数件）は、病床機能報告の病棟ごとの入院基本料から、届出名称ごとに参考値として補う
# （Streamlit版の病院ページと同じ対応）。
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
    n_facilities INTEGER, n_filings INTEGER,
    bureau TEXT, bureau_page TEXT, bureau_url TEXT,  -- 出典（管轄の厚生局・名簿のページの名前・URL）
    used_on TEXT, used_on_label TEXT                  -- 出典の「利用した日」（名簿を取った日）
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
CREATE TABLE kubuns (             -- 入院基本料の区分（急性期一般入院料１など）。検索フォームで届出項目と同じように選べる
    id INTEGER PRIMARY KEY, kijun_id INTEGER NOT NULL, name TEXT NOT NULL, search TEXT NOT NULL,
    n_facilities INTEGER, n_ref INTEGER
);
CREATE TABLE fac_kubun (          -- 医療機関×区分。ref=1 は病床機能報告からの参考値だけで分かったもの
    facility_id TEXT NOT NULL, kubun_id INTEGER NOT NULL, ref INTEGER NOT NULL,
    start_date TEXT               -- 参考値のもとになった届出の算定開始日（出し直されたかの注意書き用）
);
"""

INDEXES = """
CREATE INDEX ix_fac_pref ON facilities(pref_code, ftype);
CREATE INDEX ix_fil_fac ON filings(facility_id);
CREATE INDEX ix_fil_kijun ON filings(kijun_id, facility_id);
CREATE INDEX ix_fil_item ON filings(item_id, facility_id);
CREATE INDEX ix_fac_area ON facilities(pref_code, area, city);
CREATE INDEX ix_areas ON areas(pref_code);
CREATE INDEX ix_kp ON kijun_pref(kijun_id, pref_code);
CREATE INDEX ix_fk ON fac_kubun(kubun_id, facility_id);
CREATE INDEX ix_fk_fac ON fac_kubun(facility_id);
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

    # 外来機能報告には厚生局の医療機関コード（医科、10桁＝都道府県2桁＋「1」＋医療機関番号7桁）と
    # 病床機能報告と同じオープンデータの医療機関コードが両方載っているので、まずそれでつなぐ
    # （名前の突合だと「宇都宮病院」同士など別の病院につながることがあった。2026年11月に31件見つかった）
    by_code = {k: v for k, v in _gairai_codes().items() if v in latest_name.index}
    codes = pd.Series(
        [by_code.get(fid) or match(p, n, t)
         for fid, p, n, t in fac[["facility_id", "pref_name", "name", "ftype"]].itertuples(index=False)],
        index=fac.index, dtype=object,
    )
    return codes, codes.map(latest_name)


def _gairai_codes() -> dict[str, str]:
    """外来機能報告の (厚生局の医療機関番号から作る facility_id) → オープンデータの医療機関コード（病床機能報告の形）。"""
    if not GAIRAI.exists():
        return {}
    a = pd.read_parquet(GAIRAI, columns=["報告年度", "医療機関コード（医科）", "オープンデータ\n医療機関コード"])
    a = a.dropna().sort_values("報告年度")
    ika = a["医療機関コード（医科）"].astype(str)
    a = a[ika.str.len() == 10]
    ika = a["医療機関コード（医科）"].astype(str)
    fid = ika.str[:2] + ika.str[3:]
    od = a["オープンデータ\n医療機関コード"].astype(str).str.zfill(10).str.lstrip("0")
    return dict(zip(fid, od))


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


def kubun_prefix(kijun: str) -> str:
    """病床機能報告の入院基本料名に付いている、届出名称由来の頭の部分（「療養病棟」「障害者施設等」など）。"""
    return kijun.removesuffix("入院基本料")


_ZEN_DIGITS = str.maketrans("0123456789", "０１２３４５６７８９")


def ref_to_kubun(kijun: str, ref: str) -> list[str]:
    """病床機能報告の入院基本料名（参考値）を、厚生局の「区分」欄と同じ書き方にそろえる。
    例: 療養病棟入院料１ → 入院料１、障害者施設等10対１入院基本料 → １０対１入院基本料、
        一般病棟特別入院基本料 → 特別入院基本料。病棟ごとに違う病院は「・」で連結されているので分ける。"""
    pre = kubun_prefix(kijun)
    out = []
    for v in ref.split("・"):
        v = v.strip()
        if pre and v.startswith(pre) and len(v) > len(pre):
            v = v[len(pre):]
        if v:
            out.append(v.translate(_ZEN_DIGITS))
    return out


def _build_kubuns(con) -> int:
    """届出の区分（公式の kubun と、参考値の kubun_ref）から、検索用の区分一覧と 医療機関×区分 を作る。
    公式と参考の両方で分かる施設は公式（ref=0）を優先する。"""
    names = dict(con.execute("SELECT id, name FROM kijun"))
    # 区分欄は入院基本料の届出にだけ意味がある（ごく一部、加算の届出に入院基本料の区分が紛れ込んでいる）
    names = {k: v for k, v in names.items() if v.endswith("入院基本料")}
    fk: dict[tuple[str, int, str], list] = {}   # (facility, kijun, 区分) → [ref, start_date]
    for fid, kid, kb, kref, sd in con.execute(
        "SELECT facility_id, kijun_id, kubun, kubun_ref, start_date FROM filings WHERE kubun != '' OR kubun_ref != ''"
    ):
        if kid not in names:
            continue
        if kb:
            fk[(fid, kid, kb.strip().translate(_ZEN_DIGITS))] = [0, None]
        else:
            for v in ref_to_kubun(names[kid], kref):
                cur = fk.get((fid, kid, v))
                if cur is None:
                    fk[(fid, kid, v)] = [1, sd or ""]
                elif cur[0] == 1 and (sd or "") > cur[1]:
                    cur[1] = sd or ""
    keys = sorted({(kid, v) for _, kid, v in fk}, key=lambda x: (x[0], x[1]))
    kb_id = {k: i + 1 for i, k in enumerate(keys)}
    n_fac: dict[int, int] = {}
    n_ref: dict[int, int] = {}
    rows = []
    for (fid, kid, v), (ref, sd) in fk.items():
        i = kb_id[(kid, v)]
        n_fac[i] = n_fac.get(i, 0) + 1
        n_ref[i] = n_ref.get(i, 0) + ref
        rows.append((fid, i, ref, sd))
    con.executemany(
        "INSERT INTO kubuns VALUES (?,?,?,?,?,?)",
        [(i, kid, v, search_key(f"{names[kid]} {v} {kubun_prefix(names[kid])}{v}"), n_fac[i], n_ref[i])
         for (kid, v), i in kb_id.items()],
    )
    con.executemany("INSERT INTO fac_kubun VALUES (?,?,?,?)", rows)
    return len(keys)


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


# 病院ページに出す病床機能報告（厚生労働省）。医療機関コードは10桁にそろえる（元データは9桁・10桁が混在）
BYOSHO_HOSP_COLS = [
    "報告年度", "医療機関コード", "医療機関名",
    "高度急性期_許可病床数", "急性期_許可病床数", "回復期_許可病床数", "慢性期_許可病床数", "合計_許可病床数",
    "合計_在棟延べ数",
    "常勤医師数", "非常勤医師数", "常勤看護師数", "非常勤看護師数",
    "常勤理学療法士数", "常勤作業療法士数", "常勤言語聴覚士数", "常勤薬剤師数",
    "CT台数", "CT_64列以上", "CT_16〜64列", "CT_16列未満", "MRI台数", "MRI_3T以上", "MRI_1.5〜3T", "MRI_1.5T未満",
    "血管連続撮影装置台数", "SPECT台数", "マンモグラフィ台数", "PET台数", "PETCT台数", "PETMRI台数",
    "ガンマナイフ台数", "サイバーナイフ台数", "IMRT台数", "内視鏡手術支援機器台数", "救急搬送件数",
]
BYOSHO_WARD_COLS = ["報告年度", "医療機関コード", "機能区分", "入院基本料", "許可病床数", "在棟延べ数"]


def _build_byosho(con) -> int:
    """病床機能報告（病院単位・病棟単位）を医療機関コード10桁で引けるようにして入れる。"""
    if not BYOSHO.exists():
        return 0
    cols = [c for c in BYOSHO_HOSP_COLS if c in pq.read_schema(BYOSHO).names]
    h = pd.read_parquet(BYOSHO, columns=cols)
    h["code"] = h["医療機関コード"].astype(str).str.zfill(10)
    h = h.drop(columns=["医療機関コード"]).rename(columns={"報告年度": "year", "医療機関名": "name"})
    h.to_sql("byosho_hosp", con, index=False)
    if WARDS.exists():
        w = pd.read_parquet(WARDS, columns=BYOSHO_WARD_COLS)
        w["code"] = w["医療機関コード"].astype(str).str.zfill(10)
        w = w.drop(columns=["医療機関コード"]).rename(columns={"報告年度": "year"})
        w.to_sql("byosho_ward", con, index=False)
        con.execute("CREATE INDEX ix_bw ON byosho_ward(code, year)")
    con.execute("CREATE INDEX ix_bh ON byosho_hosp(code, year)")
    return len(h)


GAIRAI_COLS = {  # 外来機能報告（様式1・年間）の列 → DBの列
    "設置主体": "owner", "病診区分": "kind",
    "初診患者数（年間）": "n_first", "紹介患者数（年間）": "n_referred", "逆紹介患者数（年間）": "n_back",
    "紹介率（年間）": "referral_rate", "逆紹介率（年間）": "back_rate", "救急車の受入件数（年間）": "ambulance",
}


def _build_gairai(con) -> int:
    """外来機能報告を、厚生局の医療機関コード（医科）から作る facility_id で引けるようにして入れる
    （病院だけでなく診療所も対象。名前の突合は使わない）。"""
    if not GAIRAI.exists():
        return 0
    a = pd.read_parquet(GAIRAI, columns=["報告年度", "医療機関コード（医科）"] + list(GAIRAI_COLS))
    a = a.dropna(subset=["医療機関コード（医科）"])
    ika = a["医療機関コード（医科）"].astype(str)
    a = a[ika.str.len() == 10].copy()
    ika = a["医療機関コード（医科）"].astype(str)
    a["facility_id"] = ika.str[:2] + ika.str[3:]
    a["ika"] = ika
    a = a.rename(columns={"報告年度": "year", **GAIRAI_COLS}).drop(columns=["医療機関コード（医科）"])
    for c in ["n_first", "n_referred", "n_back", "referral_rate", "back_rate", "ambulance"]:
        a[c] = pd.to_numeric(a[c], errors="coerce")  # 「*」（10件以下で非公表）は空にする
    if GAIRAI2.exists():
        # 紹介受診重点外来（令和4年度は「医療資源を重点的に活用する外来」という名前）の患者割合。報告月0が年間
        cols = ["紹介受診重点外来の患者割合", "紹介受診重点外来の患者割合__1",
                "医療資源を重点的に活用する患者割合", "医療資源を重点的に活用する患者割合__1"]
        b = pd.read_parquet(GAIRAI2, columns=["報告年度", "医療機関コード（医科）", "報告月"] + cols)
        b = b[b["報告月"].astype(str) == "0"].copy()
        num = {c: pd.to_numeric(b[c], errors="coerce") for c in cols}
        b["focus_first"] = num[cols[0]].fillna(num[cols[2]])
        b["focus_re"] = num[cols[1]].fillna(num[cols[3]])
        b = b.rename(columns={"報告年度": "year", "医療機関コード（医科）": "ika"})[["year", "ika", "focus_first", "focus_re"]]
        b["ika"] = b["ika"].astype(str)
        a = a.merge(b.drop_duplicates(["year", "ika"]), on=["year", "ika"], how="left")
    a = a.drop(columns=["ika"]).drop_duplicates(["facility_id", "year"])
    a.to_sql("gairai", con, index=False)
    con.execute("CREATE INDEX ix_ga ON gairai(facility_id, year)")
    return len(a)


def _build_hstats(con) -> int:
    """トップの条件表の「病床・設備・実績」で絞り込むための、医療機関ごとの最新の数字。
    病床機能報告・DPC・外来機能報告それぞれの最新年度の値だけを持つ（年度は meta に残し、画面に書く）。"""
    have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "byosho_hosp" not in have:
        return 0
    by = con.execute("SELECT MAX(year) FROM byosho_hosp").fetchone()[0]
    dy = con.execute("SELECT MAX(year) FROM dpc_hosp").fetchone()[0] if "dpc_hosp" in have else None
    gy = con.execute("SELECT MAX(year) FROM gairai WHERE n_first > 0").fetchone()[0] if "gairai" in have else None
    con.execute(f"""
        CREATE TABLE hstats AS
        SELECT f.id AS facility_id,
               b."合計_許可病床数" AS beds,
               b."高度急性期_許可病床数" AS b_hk, b."急性期_許可病床数" AS b_ak,
               b."回復期_許可病床数" AS b_kf, b."慢性期_許可病床数" AS b_mk,
               b."CT台数" AS ct, b."CT_64列以上" AS ct64, b."MRI台数" AS mri, b."MRI_3T以上" AS mri3t,
               COALESCE(b."PET台数", 0) + COALESCE(b."PETCT台数", 0) + COALESCE(b."PETMRI台数", 0) AS pet,
               b."血管連続撮影装置台数" AS angio, b."内視鏡手術支援機器台数" AS robot, b."IMRT台数" AS imrt,
               b."救急搬送件数" AS amb,
               d."件数_総数" AS dpc_total, d."件数_手術有" AS dpc_surg,
               g.referral_rate AS referral
        FROM facilities f
        JOIN byosho_hosp b ON b.code = substr('0000000000' || f.byosho_code, -10) AND b.year = ?
        LEFT JOIN dpc_hosp d ON d.code = b.code AND d.year = ?
        LEFT JOIN gairai g ON g.facility_id = f.id AND g.year = ? AND g.n_first > 0
        WHERE f.byosho_code IS NOT NULL""", (by, dy, gy))
    con.execute("CREATE UNIQUE INDEX ix_hs ON hstats(facility_id)")
    con.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)",
                    [("hstats_byosho_year", str(by or "")), ("hstats_dpc_year", str(dy or "")),
                     ("hstats_gairai_year", str(gy or ""))])
    return con.execute("SELECT COUNT(*) FROM hstats").fetchone()[0]


_JIS_PREFS = (
    "北海道 青森県 岩手県 宮城県 秋田県 山形県 福島県 茨城県 栃木県 群馬県 埼玉県 千葉県 東京都 神奈川県 "
    "新潟県 富山県 石川県 福井県 山梨県 長野県 岐阜県 静岡県 愛知県 三重県 滋賀県 京都府 大阪府 兵庫県 "
    "奈良県 和歌山県 鳥取県 島根県 岡山県 広島県 山口県 徳島県 香川県 愛媛県 高知県 福岡県 佐賀県 長崎県 "
    "熊本県 大分県 宮崎県 鹿児島県 沖縄県"
).split()
DPC_PROC_COLS = ["件数_総数", "件数_手術有", "件数_化学療法有", "件数_放射線療法有", "件数_救急車搬送有", "件数_全身麻酔"]


def _build_dpc(con) -> int:
    """DPC（退院患者調査）を病床機能報告の医療機関コード10桁で引けるようにして入れる。

    告示番号 → (施設名, 都道府県) → dpc_match の病床報告施設名 → 病床機能報告の医療機関コード、の順でつなぐ。
    同じ施設名・都道府県の組が対応表に複数ある、または同じ年度に1つの医療機関コードへ
    複数の告示番号がつながるものは、取り違えを避けるため入れない。
    """
    if not (DPC_HOSP.exists() and DPC_MATCH.exists() and BYOSHO.exists()):
        return 0
    bd = pd.read_parquet(BYOSHO, columns=["都道府県名", "医療機関名", "医療機関コード"]).drop_duplicates()
    key = {(p, n): str(c).zfill(10) for p, n, c in bd.itertuples(index=False)}
    m = pd.read_parquet(DPC_MATCH)
    m = m[m["マッチ状態"].astype(str) != "未結合"].drop_duplicates(["DPC施設名", "都道府県"], keep=False)
    m["code"] = [key.get((p, n)) for p, n in m[["都道府県", "病床報告施設名"]].itertuples(index=False)]
    h = pd.read_parquet(DPC_HOSP, columns=["年度", "告示番号", "施設名", "市町村番号", "病院類型", "DPC算定病床数"])
    h["pref"] = h["市町村番号"].map(lambda x: _JIS_PREFS[int(x) // 1000 - 1] if pd.notna(x) else None)
    h = h.merge(m[["DPC施設名", "都道府県", "code"]], left_on=["施設名", "pref"],
                right_on=["DPC施設名", "都道府県"], how="inner").dropna(subset=["code"])
    h = h[~h.duplicated(["年度", "code"], keep=False)]
    h = h.rename(columns={"年度": "year", "告示番号": "kokuji", "施設名": "dpc_name",
                          "病院類型": "dpc_type", "DPC算定病床数": "dpc_beds"})
    h = h[["code", "year", "kokuji", "dpc_name", "dpc_type", "dpc_beds"]]
    if DPC_PROC.exists():
        p = pd.read_parquet(DPC_PROC, columns=["年度", "告示番号"] + DPC_PROC_COLS)
        p = p.drop_duplicates(["年度", "告示番号"]).rename(columns={"年度": "year", "告示番号": "kokuji"})
        h = h.merge(p, on=["year", "kokuji"], how="left")
    h.to_sql("dpc_hosp", con, index=False)
    con.execute("CREATE INDEX ix_dh ON dpc_hosp(code, year)")
    if DPC_MDC.exists():
        c = pd.read_parquet(DPC_MDC)
        c = c[c["手術有無"].isin(["有り", "無し"])]
        mdcs = [f"MDC{i:02d}" for i in range(1, 19)]
        c = c.melt(id_vars=["年度", "告示番号", "手術有無"], value_vars=mdcs, var_name="mdc", value_name="n")
        c = c.pivot_table(index=["年度", "告示番号", "mdc"], columns="手術有無", values="n",
                          aggfunc="first", dropna=False).reset_index()
        c = c.rename(columns={"年度": "year", "告示番号": "kokuji", "有り": "n_surg", "無し": "n_nosurg"})
        c = c.merge(h[["year", "kokuji", "code"]], on=["year", "kokuji"], how="inner")
        c = c.dropna(subset=["n_surg", "n_nosurg"], how="all")
        c[["code", "year", "mdc", "n_surg", "n_nosurg"]].to_sql("dpc_mdc", con, index=False)
        con.execute("CREATE INDEX ix_dm ON dpc_mdc(code, year)")
    return len(h)


def _build_kijun_rel(con, top: int = 12) -> int:
    """施設基準ページの「一緒に届け出られていることが多い施設基準」。施設基準Aを届け出ている医療機関のうち、
    施設基準Bも届け出ている医療機関の数（両方を届け出ている数）を数え、
    両方の届出数で割った値（n / √(Aの数×Bの数)）の大きい順に上位を残す。どの医療機関も届け出る
    ベースアップ評価料のような項目ばかりが並ばないようにするため（並べ方だけに使い、画面には件数を出す。
    SQLiteに平方根が無い環境もあるので、同じ順になる n² / (Aの数×Bの数) で並べる）。"""
    con.execute("CREATE TEMP TABLE fk AS SELECT DISTINCT facility_id, kijun_id FROM filings")
    con.execute("CREATE INDEX t_fk ON fk(facility_id, kijun_id)")
    con.execute(
        """CREATE TEMP TABLE pairs AS
           SELECT a.kijun_id AS k, b.kijun_id AS r, COUNT(*) AS n FROM fk a
           JOIN fk b ON a.facility_id = b.facility_id AND a.kijun_id <> b.kijun_id GROUP BY 1, 2""")
    con.execute(
        f"""CREATE TABLE kijun_rel AS
            SELECT k AS kijun_id, r AS rel_id, n, rk FROM (
              SELECT p.k, p.r, p.n, ROW_NUMBER() OVER (
                PARTITION BY p.k ORDER BY p.n * p.n * 1.0 / (ka.n_facilities * kb.n_facilities) DESC, p.n DESC) AS rk
              FROM pairs p JOIN kijun ka ON ka.id = p.k JOIN kijun kb ON kb.id = p.r
              WHERE p.n >= 3)
            WHERE rk <= {top}""")
    con.execute("CREATE INDEX ix_kr ON kijun_rel(kijun_id)")
    con.execute("DROP TABLE pairs")
    con.execute("DROP TABLE fk")
    return con.execute("SELECT COUNT(*) FROM kijun_rel").fetchone()[0]


def _jlabel(iso: str) -> str:
    try:
        return f"{int(iso[:4])}年{int(iso[5:7])}月{int(iso[8:10])}日"
    except (ValueError, TypeError):
        return ""


def _build_kdiff(con) -> int:
    """毎月の新規届出・取り下げ・区分の変更（kijun_diff.parquet）を kdiff に入れる（/changes ページ用）。
    facility_id は医科の医療機関と同じ（都道府県コード2桁＋医療機関番号7桁）。名簿から無くなった医療機関は facilities に無い。"""
    con.executescript("""
        CREATE TABLE kdiff (month TEXT, prev_month TEXT, pref_code TEXT, pref_name TEXT, facility_id TEXT, fac_name TEXT,
                            address TEXT, ftype TEXT, kind TEXT, kijun_name TEXT, prev_name TEXT, kijun_id INTEGER,
                            symbol TEXT, juri_no TEXT, start_date TEXT, fac_event TEXT);
    """)
    if not KDIFF.exists():
        return 0
    d = pd.read_parquet(KDIFF).fillna("")
    d["facility_id"] = d["都道府県コード"].str.zfill(2) + d["医療機関番号"].str.zfill(7)
    kid = dict(con.execute("SELECT name, id FROM kijun").fetchall())
    d["kijun_id"] = d["受理届出名称"].map(kid).astype("Int64")
    d["start_date"] = d["算定開始年月日"].map(wareki_to_iso)
    con.executemany(
        "INSERT INTO kdiff VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        d[["新年月", "旧年月", "都道府県コード", "都道府県名", "facility_id", "医療機関名称", "住所", "施設種別", "種類",
           "受理届出名称", "前の届出名称", "kijun_id", "受理記号", "受理番号", "start_date", "医療機関の動き"]]
        .astype(object).where(d.notna(), None).itertuples(index=False, name=None))
    con.executescript("""
        CREATE INDEX ix_kd_month ON kdiff(month, pref_code);
        CREATE INDEX ix_kd_fac ON kdiff(facility_id);
        CREATE INDEX ix_kd_kijun ON kdiff(kijun_id, month);
    """)
    return len(d)


def _build_yakkyoku(con) -> int:
    """薬局の届出（yakkyoku_cache.parquet）を y_* のテーブルに入れる（/yakkyoku/… のページ用）。"""
    return _build_other(con, "y", YAKKYOKU, YAKKYOKU_FETCHED, "4", r"(特別)?調剤基本料", r"地域支援")


def _build_shika(con) -> int:
    """歯科の届出（shika_cache.parquet）を d_* のテーブルに入れる（/shika/… のページ用）。
    一覧の列には、歯科の診療所の体制が分かる「口腔管理体制強化加算」（旧かかりつけ歯科医機能強化型。名簿の届出名は
    「小児口腔機能管理料の注５に規定する口腔管理体制強化加算」）と
    「歯科外来診療医療安全対策加算」を出す。"""
    return _build_other(con, "d", SHIKA, SHIKA_FETCHED, "3", r".*口腔管理体制強化加算",
                        r"歯科外来診療医療安全対策加算")


def _build_other(con, x: str, path: Path, fetched: Path, digit: str, pat1: str, pat2: str) -> int:
    """薬局・歯科の届出を {x}_* のテーブルに入れる。

    薬局・歯科の医療機関番号は医科と同じ7桁の体系で別の施設に重なることがあるので、IDは
    都道府県コード2桁＋種類を表す1桁（歯科「3」・薬局「4」。10桁の医療機関コードと同じ）＋医療機関番号7桁の
    10桁にする（医科は9桁なので重ならない）。一覧の列 kihon・chiiki には、pat1・pat2 に当てはまる届出名を入れる。"""
    if not path.exists():
        return 0
    # 使う列だけを読み、繰り返しの多い列は辞書エンコード（category）のままにしてメモリを抑える（歯科は70万行ある）
    cols = ["都道府県コード", "都道府県名", "医療機関番号", "医療機関名称", "住所", "受理届出名称", "受理記号",
            "受理番号", "算定開始年月日", "時点"]
    dict_cols = ["都道府県コード", "都道府県名", "受理届出名称", "受理記号", "算定開始年月日"]
    d = pq.read_table(path, columns=cols, read_dictionary=dict_cols).to_pandas()
    for c in dict_cols:
        d[c] = d[c].cat.add_categories([""]).fillna("") if "" not in d[c].cat.categories else d[c].fillna("")
        d[c] = d[c].cat.reorder_categories(sorted(d[c].cat.categories))   # 並べ替えを文字列順にする
    for c in ("医療機関番号", "医療機関名称", "住所", "受理番号", "時点"):
        d[c] = d[c].fillna("")
    d["facility_id"] = (d["都道府県コード"].astype(str).str.zfill(2) + digit + d["医療機関番号"].str.zfill(7))
    d["start_date"] = d["算定開始年月日"].map(wareki_to_iso).astype(str)
    con.executescript(f"""
        CREATE TABLE {x}_prefs (code TEXT PRIMARY KEY, name TEXT, as_of TEXT, as_of_label TEXT, n_facilities INTEGER,
                              bureau TEXT, bureau_page TEXT, bureau_url TEXT, used_on TEXT, used_on_label TEXT);
        CREATE TABLE {x}_fac (id TEXT PRIMARY KEY, pref_code TEXT, pref_name TEXT, code TEXT, name TEXT, name_search TEXT,
                            address TEXT, address_search TEXT, city TEXT, area TEXT, n_kijun INTEGER,
                            kihon TEXT, chiiki TEXT);
        CREATE TABLE {x}_kijun (id INTEGER PRIMARY KEY, name TEXT UNIQUE, symbol TEXT, n_facilities INTEGER);
        CREATE TABLE {x}_filings (facility_id TEXT, kijun_id INTEGER, symbol TEXT, juri_no TEXT, start_date TEXT);
        CREATE TABLE {x}_kijun_pref (kijun_id INTEGER, pref_code TEXT, n INTEGER);
        CREATE TABLE {x}_areas (pref_code TEXT, area TEXT, area_order INTEGER, city TEXT, city_order TEXT, n INTEGER);
    """)
    p = (d.groupby(["都道府県コード", "都道府県名"], observed=True)
          .agg(as_of=("時点", "max"), n_facilities=("facility_id", "nunique")).reset_index())
    p = _with_sources(p, fetched)
    p["as_of_label"] = p["as_of"].map(_jlabel)
    con.executemany(f"INSERT INTO {x}_prefs VALUES (?,?,?,?,?,?,?,?,?,?)",
                    p[["都道府県コード", "都道府県名", "as_of", "as_of_label", "n_facilities",
                       "bureau", "bureau_page", "bureau_url", "used_on", "used_on_label"]].astype(object).itertuples(index=False, name=None))
    # 施設ごとの主な届出（一覧の列に出す）
    first = lambda pat: (d[d["受理届出名称"].str.match(pat)].sort_values("受理届出名称")
                         .groupby("facility_id", observed=True)["受理届出名称"].first().astype(str))
    fac = (d.groupby("facility_id", observed=True)
            .agg(pref_code=("都道府県コード", "first"), pref_name=("都道府県名", "first"), code=("医療機関番号", "first"),
                 name=("医療機関名称", "first"), address=("住所", "first"), n_kijun=("受理届出名称", "nunique"))
            .reset_index())
    fac["kihon"] = fac["facility_id"].map(first(pat1))
    fac["chiiki"] = fac["facility_id"].map(first(pat2))
    fac["name_search"] = fac["name"].map(search_key)
    fac["address_search"] = fac["address"].map(search_key)
    fac["city"], fac["area"], areas = _city_area(fac)
    con.executemany(f"INSERT INTO {x}_fac VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    fac[["facility_id", "pref_code", "pref_name", "code", "name", "name_search", "address",
                         "address_search", "city", "area", "n_kijun", "kihon", "chiiki"]]
                    .astype(object).where(fac.notna(), None).itertuples(index=False, name=None))
    con.executemany(f"INSERT INTO {x}_areas VALUES (?,?,?,?,?,?)",
                    areas[["pref_code", "area", "area_order", "city", "city_order", "n"]].astype(object).itertuples(index=False, name=None))
    kj = d.groupby("受理届出名称", observed=True).agg(n=("facility_id", "nunique")).reset_index()
    sym = (d.groupby(["受理届出名称", "受理記号"], observed=True).size().reset_index(name="c").sort_values("c", ascending=False)
            .drop_duplicates("受理届出名称").set_index("受理届出名称")["受理記号"])
    kj = kj.sort_values(["n", "受理届出名称"], ascending=[False, True]).reset_index(drop=True)
    kj["id"] = kj.index + 1
    kj["symbol"] = kj["受理届出名称"].map(sym)
    con.executemany(f"INSERT INTO {x}_kijun VALUES (?,?,?,?)",
                    kj[["id", "受理届出名称", "symbol", "n"]].astype(object).itertuples(index=False, name=None))
    d["kijun_id"] = d["受理届出名称"].astype(str).map(dict(zip(kj["受理届出名称"], kj["id"])))
    con.executemany(f"INSERT INTO {x}_filings VALUES (?,?,?,?,?)",
                    d[["facility_id", "kijun_id", "受理記号", "受理番号", "start_date"]].drop_duplicates()
                    .astype(object).itertuples(index=False, name=None))
    kp = d.groupby(["kijun_id", "都道府県コード"], observed=True)["facility_id"].nunique().reset_index(name="n")
    con.executemany(f"INSERT INTO {x}_kijun_pref VALUES (?,?,?)", kp.astype(object).itertuples(index=False, name=None))
    con.executescript(f"""
        CREATE INDEX ix_{x}f_pref ON {x}_fac(pref_code, city);
        CREATE INDEX ix_{x}f_area ON {x}_fac(pref_code, area);
        CREATE INDEX ix_{x}fl_fac ON {x}_filings(facility_id);
        CREATE INDEX ix_{x}fl_kijun ON {x}_filings(kijun_id, facility_id);
        CREATE INDEX ix_{x}kp ON {x}_kijun_pref(kijun_id, pref_code);
        CREATE INDEX ix_{x}a ON {x}_areas(pref_code);
    """)
    return len(fac)


def _with_sources(prefs: pd.DataFrame, fetched_path: Path = FETCHED) -> pd.DataFrame:
    """都道府県ごとに、出典に書く厚生局・名簿のページ・URL・利用した日（名簿を取った日）を付ける。"""
    fetched = json.loads(fetched_path.read_text(encoding="utf-8")) if fetched_path.exists() else {}
    p = prefs.copy()
    names = p["都道府県名"].astype(str)
    p["bureau"] = names.map(lambda n: BUREAU_OF_PREF.get(n, ("", "", ""))[0])
    p["bureau_page"] = names.map(lambda n: BUREAU_OF_PREF.get(n, ("", "", ""))[1])
    p["bureau_url"] = names.map(lambda n: BUREAU_OF_PREF.get(n, ("", "", ""))[2])
    p["used_on"] = names.map(lambda n: fetched.get(n, ""))
    p["used_on_label"] = p["used_on"].map(
        lambda d: f"{int(d[:4])}年{int(d[5:7])}月{int(d[8:10])}日" if d else "")
    return p


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
        "INSERT INTO prefs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        _with_sources(prefs)[["都道府県コード", "都道府県名", "as_of", "as_of_label", "n_facilities", "n_filings",
                              "bureau", "bureau_page", "bureau_url", "used_on", "used_on_label"]]
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
    n_kubuns = _build_kubuns(con)
    n_byosho_rows = _build_byosho(con)
    n_dpc_rows = _build_dpc(con)
    n_gairai_rows = _build_gairai(con)
    n_hstats = _build_hstats(con)
    n_kijun_rel = _build_kijun_rel(con)
    n_yakkyoku = _build_yakkyoku(con)
    n_shika = _build_shika(con)
    n_kdiff = _build_kdiff(con)
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
        "n_kubuns": str(n_kubuns),
        "n_byosho_rows": str(n_byosho_rows),
        "n_dpc_rows": str(n_dpc_rows),
        "n_gairai_rows": str(n_gairai_rows),
        "n_hstats": str(n_hstats),
        "n_kijun_rel": str(n_kijun_rel),
        "n_yakkyoku": str(n_yakkyoku),
        "n_shika": str(n_shika),
        "n_kdiff": str(n_kdiff),
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
