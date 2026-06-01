#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
2021年 様式2（手術）データを DuckDB に取り込むスクリプト
7つのファイルをまとめて指定できます。

使い方:
    python update_surgery_2021.py <xlsx1> <xlsx2> ... <xlsx7>
例:
    python update_surgery_2021.py 000953885.xlsx 000953886.xlsx 000953887.xlsx 000953888.xlsx 000953889.xlsx 000953890.xlsx 000953892.xlsx
"""
import sys
from pathlib import Path

import duckdb
import pandas as pd

DB_PATH = Path(__file__).parent / "data" / "byosho.duckdb"


def main():
    if len(sys.argv) < 2:
        print("使い方: python update_surgery_2021.py ファイル1.xlsx ファイル2.xlsx ...")
        sys.exit(1)

    xlsx_paths = [Path(p) for p in sys.argv[1:]]
    for p in xlsx_paths:
        if not p.exists():
            print(f"ERROR: ファイルが見つかりません: {p}")
            sys.exit(1)

    sys.path.insert(0, str(Path(__file__).parent))
    from data_processor import _detect_yoshiki2_is_multilevel, load_mhlw_yoshiki2

    parts = []
    for xlsx_path in xlsx_paths:
        print(f"読み込み: {xlsx_path.name}")
        file_bytes = xlsx_path.read_bytes()
        fmt = "2021式(5段組)" if _detect_yoshiki2_is_multilevel(file_bytes) else "2022/2023式(単一)"
        df = load_mhlw_yoshiki2(file_bytes, year=2021)
        print(f"  フォーマット: {fmt} → {len(df):,} 病院")
        parts.append(df)

    if not parts:
        print("ERROR: データが空です")
        sys.exit(1)

    surg_df = pd.concat(parts, ignore_index=True).drop_duplicates(subset=["医療機関名", "都道府県名"])
    print(f"\n合計: {len(surg_df):,} 病院（重複除去後）")

    print(f"\nDuckDB 更新: {DB_PATH}")
    con = duckdb.connect(str(DB_PATH))
    try:
        existing = con.execute("SELECT * FROM surgery WHERE 報告年度 != 2021").fetchdf()
        print(f"既存 surgery (2021以外): {len(existing):,} 行を保持")
    except Exception:
        existing = pd.DataFrame()

    new_surgery = pd.concat([existing, surg_df], ignore_index=True) if not existing.empty else surg_df
    con.execute("DROP TABLE IF EXISTS surgery")
    con.register("_surg", new_surgery)
    con.execute("CREATE TABLE surgery AS SELECT * FROM _surg")
    con.close()

    print(f"\n=== 完了 ===")
    print(f"  2021年: {len(surg_df):,} 病院")
    if not existing.empty:
        for yr in sorted(existing["報告年度"].unique()):
            n = (existing["報告年度"] == yr).sum()
            print(f"  {yr}年: {n:,} 病院 (既存)")
    print("\nDockerを再起動してください: docker compose restart")


if __name__ == "__main__":
    main()
