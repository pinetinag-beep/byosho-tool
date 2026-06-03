"""
厚生労働省 医療情報ネット オープンデータ から病院緯度経度を取得して DuckDB に格納する。

使い方:
  python build_master.py                      # 最新 ZIP を自動ダウンロード
  python build_master.py --file some.zip      # ローカル ZIP を指定
  python build_master.py --url https://...    # ZIP URL を指定

データソース:
  https://data.e-gov.go.jp/data/dataset/iryou_teikyouseido_mhlw
  （厚生労働省 医療情報ネット オープンデータ）
"""
import argparse
import io
import os
import sys
import zipfile
from pathlib import Path

import duckdb
import pandas as pd
import urllib.request

# ── デフォルト URL（最新版に更新する場合はここを変える） ──────────────
DEFAULT_URL = (
    "https://data.e-gov.go.jp/data/dataset/"
    "321fdf20-5f6a-49e5-bcab-35d81d652c65/resource/"
    "f8c2a615-878f-4e88-9c26-8f2e23ec5548/download/e-gov20250601.zip"
)

DB_PATH = Path(__file__).parent / "data" / "byosho.duckdb"

# e-gov CSV の列名候補（バージョンによってブレがある）
# 注: 旧形式は「緯度」「経度」、2025年版は「所在地標準（緯度）」「所在地標準（経度）」
_COL_CANDIDATES = {
    "name":    ["施設名", "医療機関名称", "正式名称", "名称"],
    "code":    ["医療機関コード", "施設コード", "医療機関番号", "ID"],
    "lat":     ["緯度", "所在地標準（緯度）", "所在地標準住所（緯度）", "latitude", "lat"],
    "lon":     ["経度", "所在地標準（経度）", "所在地標準住所（経度）", "longitude", "lon"],
    "pref":    ["都道府県名", "都道府県"],
    "address": ["住所", "所在地", "所在地標準住所"],
    "url":     ["案内用ホームページアドレス", "ホームページアドレス", "ホームページURL", "URL"],
}


def _pick(df: pd.DataFrame, candidates: list[str]) -> str | None:
    # 完全一致を優先
    for c in candidates:
        if c in df.columns:
            return c
    # 部分一致（フォールバック）
    for kw in candidates:
        for col in df.columns:
            if kw in col:
                return col
    return None


def _load_zip(data: bytes) -> list[pd.DataFrame]:
    """ZIP バイト列から全 CSV を読み込んで DataFrame のリストを返す。"""
    frames = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not csv_names:
            raise ValueError("ZIP 内に CSV ファイルが見つかりません")
        print(f"  ZIP 内 CSV: {csv_names}")
        for name in csv_names:
            with zf.open(name) as f:
                try:
                    df = pd.read_csv(f, encoding="utf-8-sig", dtype=str, low_memory=False)
                except UnicodeDecodeError:
                    f.seek(0)
                    df = pd.read_csv(f, encoding="cp932", dtype=str, low_memory=False)
            print(f"    {name}: {len(df):,}行, 列={list(df.columns[:6])}...")
            frames.append(df)
    return frames


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    """1つの CSV を正規化して返す。緯度経度列がなければ空 DF を返す。"""
    col = {k: _pick(df, v) for k, v in _COL_CANDIDATES.items()}

    # 緯度経度がない CSV（診療時間CSVなど）はスキップ
    if not col["lat"] or not col["lon"]:
        return pd.DataFrame()

    rows = pd.DataFrame()
    rows["施設名"]        = df[col["name"]]    if col["name"]    else ""
    rows["医療機関コード"] = df[col["code"]]    if col["code"]    else None
    rows["lat"]           = pd.to_numeric(df[col["lat"]], errors="coerce")
    rows["lon"]           = pd.to_numeric(df[col["lon"]], errors="coerce")
    rows["都道府県名"]     = df[col["pref"]]    if col["pref"]    else ""
    rows["住所"]           = df[col["address"]] if col["address"] else ""
    rows["url"]            = df[col["url"]]     if col["url"]     else ""

    rows = rows.dropna(subset=["lat", "lon"])
    rows = rows[(rows["lat"].between(20, 50)) & (rows["lon"].between(122, 154))]
    return rows.reset_index(drop=True)


def build_locations(data: bytes, db_path: str) -> int:
    """
    ZIP バイトを解析して DuckDB の locations テーブルに格納する。
    Returns: 格納した行数
    """
    print("CSV を解析中...")
    raw_list = _load_zip(data)

    # 各 CSV を個別に正規化してから結合（スキーマが CSV ごとに異なるため）
    frames = []
    for df in raw_list:
        n = _normalize(df)
        if not n.empty:
            print(f"  → {len(n):,} 行 (緯度経度あり)")
            frames.append(n)

    if not frames:
        raise ValueError("緯度経度を含む CSV が見つかりません")

    combined = pd.concat(frames, ignore_index=True)
    print(f"  合計 {len(combined):,} 行")

    con = duckdb.connect(db_path)
    con.execute("""
        CREATE TABLE IF NOT EXISTS locations (
            施設名        VARCHAR,
            医療機関コード VARCHAR,
            lat           DOUBLE,
            lon           DOUBLE,
            都道府県名    VARCHAR,
            住所          VARCHAR,
            url           VARCHAR,
            data_source   VARCHAR,
            data_date     VARCHAR
        )
    """)
    # 既存の e-gov データを削除して上書き
    con.execute("DELETE FROM locations WHERE data_source = 'egov'")

    df = combined
    df["data_source"] = "egov"
    df["data_date"]   = "2025-06-01"

    con.execute("INSERT INTO locations SELECT * FROM df")
    count = con.execute("SELECT COUNT(*) FROM locations WHERE data_source='egov'").fetchone()[0]
    con.close()
    return count


def main():
    parser = argparse.ArgumentParser(description="医療機関マスタから緯度経度を DuckDB に格納")
    parser.add_argument("--url",  default=DEFAULT_URL, help="ZIP の URL")
    parser.add_argument("--file", default=None,        help="ローカル ZIP ファイルパス")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"ERROR: DuckDB が見つかりません: {DB_PATH}")
        print("先に build_db.py を実行してください。")
        sys.exit(1)

    if args.file:
        print(f"ローカルファイルを読み込み: {args.file}")
        data = Path(args.file).read_bytes()
    else:
        print(f"ダウンロード中: {args.url}")
        try:
            req = urllib.request.Request(
                args.url,
                headers={"User-Agent": "byosho-tool/1.0 (medical data research)"},
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = resp.read()
            print(f"  取得完了: {len(data):,} bytes")
        except Exception as e:
            print(f"ERROR: ダウンロード失敗: {e}")
            print()
            print("手動でダウンロードしてください:")
            print(f"  {DEFAULT_URL}")
            print("そして以下で実行してください:")
            print("  python build_master.py --file ダウンロードしたファイル.zip")
            sys.exit(1)

    count = build_locations(data, str(DB_PATH))
    print(f"\n完了: {count:,} 件の位置情報を locations テーブルに格納しました")


if __name__ == "__main__":
    main()
