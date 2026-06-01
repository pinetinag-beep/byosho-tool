#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
2021年 様式2（手術）データを DuckDB に取り込むスクリプト
既存の hospitals / wards テーブルはそのまま保持し、
surgery テーブルの 2021 年分だけを更新する。

使い方:
    python update_surgery_2021.py <xlsx_path>
例:
    python update_surgery_2021.py "C:/Users/.../000953885.xlsx"
"""
import sys
import argparse
from pathlib import Path

import duckdb
import pandas as pd

DB_PATH = Path(__file__).parent / "data" / "byosho.duckdb"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("xlsx", help="2021年様式2 Excel ファイルのパス")
    parser.add_argument("--db", default=str(DB_PATH))
    args = parser.parse_args()

    xlsx_path = Path(args.xlsx)
    if not xlsx_path.exists():
        print(f"ERROR: ファイルが見つかりません: {xlsx_path}")
        sys.exit(1)

    print(f"読み込み: {xlsx_path}")
    from data_processor import _detect_yoshiki2_is_multilevel, load_mhlw_yoshiki2

    file_bytes = xlsx_path.read_bytes()
    fmt = "2021式(5段組)" if _detect_yoshiki2_is_multilevel(file_bytes) else "2022/2023式(単一)"
    print(f"フォーマット検出: {fmt}")

    surg_df = load_mhlw_yoshiki2(file_bytes, year=2021)
    print(f"手術データ取得: {len(surg_df):,} 病院")

    if surg_df.empty:
        print("ERROR: データが空です。ファイルを確認してください。")
        sys.exit(1)

    # DuckDB 更新: surgery テーブルの 2021 年分を入れ替える
    db_path = Path(args.db)
    print(f"\nDuckDB 更新: {db_path}")
    con = duckdb.connect(str(db_path))

    # 既存の surgery テーブルの 2021 以外を保持
    try:
        existing = con.execute("SELECT * FROM surgery WHERE 報告年度 != 2021").fetchdf()
        print(f"既存 surgery (2021以外): {len(existing):,} 行")
    except Exception:
        existing = pd.DataFrame()
        print("surgery テーブルが存在しないため新規作成します")

    # 新しい surgery = 既存(2021以外) + 新2021
    new_surgery = pd.concat([existing, surg_df], ignore_index=True) if not existing.empty else surg_df

    con.execute("DROP TABLE IF EXISTS surgery")
    con.register("_surg", new_surgery)
    con.execute("CREATE TABLE surgery AS SELECT * FROM _surg")
    con.close()

    print(f"\n完了: surgery テーブル = {len(new_surgery):,} 行")
    print(f"  2021年: {len(surg_df):,} 病院")
    if not existing.empty:
        for yr in sorted(existing["報告年度"].unique()):
            n = (existing["報告年度"] == yr).sum()
            print(f"  {yr}年: {n:,} 病院 (既存)")


if __name__ == "__main__":
    main()
