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
import os
import re
import sqlite3
import sys
import time
import unicodedata
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SRC = ROOT / "shisetsu_kijun_cache.parquet"
BYOSHO = ROOT / "data_cache.parquet"
OUT = HERE / "data" / "kijun.sqlite"

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
    byosho_name TEXT              -- 病床機能報告側の医療機関名（Streamlit版へのリンク用、突合できた場合のみ）
);
CREATE TABLE kijun (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, name_search TEXT NOT NULL,
    symbol TEXT, n_facilities INTEGER
);
CREATE TABLE filings (
    facility_id TEXT NOT NULL, kijun_id INTEGER NOT NULL,
    symbol TEXT, juri_no TEXT, start_date TEXT, start_raw TEXT,
    byoto TEXT, byosho_kubun TEXT, n_byoto TEXT, n_byosho TEXT, kubun TEXT, other TEXT
);
CREATE TABLE kijun_pref (kijun_id INTEGER NOT NULL, pref_code TEXT NOT NULL, n INTEGER NOT NULL);
"""

INDEXES = """
CREATE INDEX ix_fac_pref ON facilities(pref_code, ftype);
CREATE INDEX ix_fil_fac ON filings(facility_id);
CREATE INDEX ix_fil_kijun ON filings(kijun_id, facility_id);
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


def _byosho_names(fac: pd.DataFrame) -> pd.Series:
    """病院・有床診療所を病床機能報告の医療機関名に突合する（Streamlit版の病院ページへのリンク用）。

    完全一致 → 同一都道府県内のサフィックス一致（4文字以上同士）の順。
    学会認定施設の突合（build_gakkai_nintei.py）と同じ方式。
    """
    if not BYOSHO.exists():
        return pd.Series([None] * len(fac), index=fac.index)
    bd = pd.read_parquet(BYOSHO, columns=["都道府県名", "医療機関名", "医療機関コード", "報告年度"])
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
        return latest_name.get(code) if code is not None else None

    return pd.Series(
        [match(p, n, t) for p, n, t in fac[["pref_name", "name", "ftype"]].itertuples(index=False)],
        index=fac.index,
    )


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
    fac["byosho_name"] = _byosho_names(fac)

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
        "INSERT INTO facilities VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        fac[["facility_id", "pref_code", "pref_name", "code", "name", "name_search",
             "address", "address_search", "ftype", "n_kijun", "byosho_name"]]
        .astype(object).where(fac.notna(), None).itertuples(index=False, name=None),
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
        con.executemany(
            "INSERT INTO filings_raw VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            b[["facility_id", "kijun_id", "受理記号", "受理番号", "start_date", "算定開始年月日",
               "病棟種別", "病床区分", "病棟数", "病床数", "区分", "内訳その他"]]
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
