#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
様式2 (yoshiki2) Excelファイルの構造診断スクリプト
使い方:
    python diagnose_yoshiki2.py <xlsx_path> [--year 2021]
"""
import sys, argparse
from pathlib import Path

import pandas as pd
import numpy as np

def diagnose(path: Path, year: int):
    data = path.read_bytes()

    print(f"=== {path.name} ({len(data)//1024:,} KB) ===\n")

    # --- ① シート一覧 ---
    xl = pd.ExcelFile(data)
    print(f"[シート一覧]")
    for s in xl.sheet_names:
        print(f"  {s}")
    print()

    # --- ② 先頭12行をそのまま表示 ---
    print("[先頭12行 (header=None)]")
    raw = pd.read_excel(data, header=None, nrows=12)
    for i, row in raw.iterrows():
        vals = [str(v).strip() for v in row if str(v) not in ("nan", "")]
        print(f"  row{i:2d}: {vals[:8]}")
    print()

    # --- ③ ヘッダー自動検出 ---
    sys.path.insert(0, str(Path(__file__).parent))
    from data_processor import _detect_yoshiki2_header, load_mhlw_yoshiki2

    hdr, skip = _detect_yoshiki2_header(data)
    print(f"[自動検出] header=row{hdr}, skiprows={skip}")

    df = pd.read_excel(data, header=hdr, skiprows=skip)
    df.columns = [str(c).strip() for c in df.columns]
    print(f"  列数: {len(df.columns)}")
    print(f"  行数(raw): {len(df):,}")
    surg_cols = [c for c in df.columns if "手術" in c]
    print(f"  手術系列: {surg_cols[:8]}")
    print()

    # --- ④ 全headerオフセットで試行 ---
    print("[各headerオフセット別 結果]")
    for h in range(3, 8):
        try:
            tmp = pd.read_excel(data, header=h, skiprows=[h+1])
            tmp.columns = [str(c).strip() for c in tmp.columns]
            has_code = any("医療機関" in c for c in tmp.columns)
            has_surg = any("手術" in c for c in tmp.columns)
            print(f"  header={h}: 列={len(tmp.columns)}, 行={len(tmp):,}, 医療機関={has_code}, 手術列={has_surg}")
        except Exception as e:
            print(f"  header={h}: ERROR {e}")
    print()

    # --- ⑤ load_mhlw_yoshiki2 での結果 ---
    try:
        result = load_mhlw_yoshiki2(data, year=year)
        print(f"[load_mhlw_yoshiki2] 病院数: {len(result):,}")
        print(result[["医療機関名", "都道府県名", "手術総数"]].head(10).to_string())
    except Exception as e:
        print(f"[load_mhlw_yoshiki2] ERROR: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("path", help="様式2 Excelファイルのパス")
    parser.add_argument("--year", type=int, default=2021)
    args = parser.parse_args()
    diagnose(Path(args.path), args.year)
