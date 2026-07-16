#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
byosho_file_R4〜R7 フォルダに紛れている様式2（手術データ・7〜8地域ファイル）を
読み込んで surgery_cache.parquet に取り込む。

【重要】様式2ファイルの「報告年度」は、フォルダ名（＝病棟票・病院票の
報告年度）より1年古い実績期間を指す。病床機能報告は7月1日時点のward
census（例: byosho_file_R7 = 令和7年7月1日時点）と、その時点までに
確定している直近1年間（前年度）の手術実績をセットで提出する仕組みのため。
実例: byosho_file_R7 内の様式2ファイルは「令和6年4月から令和7年3月診療分」
      = 令和6年度（2024年度）の実績 → 報告年度は 2024（2025ではない）。
このスクリプトはファイル内の「【令和X年Y月から令和(X+1)年Z月診療分】」という
埋め込みテキストから報告年度を自動判定するため、年度を手打ちで指定する必要が
なく、この種の年度ラベルの取り違えを防げる。

使い方:
    python build_yoshiki2_from_byosho.py byosho_file_R7 \
        --exclude 001717798 001717800 001717801 001717802 001717803 001717804 001717805 001717806
"""
import argparse
import glob
import re
from pathlib import Path

import pandas as pd
import openpyxl

from data_processor import load_mhlw_yoshiki2

BASE = Path(__file__).parent
SURGERY_PARQUET = BASE / "surgery_cache.parquet"

_PERIOD_RE = re.compile(r"令和(\d+)年(\d+)月から令和(\d+)年(\d+)月診療分")


def _detect_report_year(path: str) -> int:
    """ファイル先頭の埋め込みテキストから報告年度（西暦）を判定する。
    「令和X年4月から令和(X+1)年3月診療分」の X が報告年度（和暦年度）。
    """
    wb = openpyxl.load_workbook(path, read_only=True)
    ws = wb[wb.sheetnames[0]]
    for row in ws.iter_rows(min_row=1, max_row=3, values_only=True):
        for v in row:
            if not v:
                continue
            m = _PERIOD_RE.search(str(v))
            if m:
                reiwa_year = int(m.group(1))
                return reiwa_year + 2018  # 令和1年 = 2019
    raise ValueError(f"報告年度を検出できませんでした: {path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", help="byosho_file_R4 等のフォルダ名")
    ap.add_argument("--exclude", nargs="+", required=True,
                     help="様式1（病棟票・病院票）で使用済みのファイルIDプレフィックス一覧")
    args = ap.parse_args()

    folder = BASE / args.folder
    all_files = sorted(glob.glob(str(folder / "*.xlsx")))
    targets = [f for f in all_files if not any(Path(f).stem.startswith(e) for e in args.exclude)]
    if not targets:
        print("対象ファイルが見つかりません（--excludeの指定を確認してください）")
        return

    print(f"対象ファイル数: {len(targets)}")
    years_found = set()
    parts = []
    for f in targets:
        year = _detect_report_year(f)
        years_found.add(year)
        with open(f, "rb") as fh:
            df = load_mhlw_yoshiki2(fh.read(), year=year)
        print(f"  {Path(f).name}: 報告年度={year} → {len(df)}行")
        parts.append(df)

    if len(years_found) != 1:
        print(f"⚠ 複数の報告年度が混在しています: {years_found}（想定外。中断します）")
        return
    year = years_found.pop()

    combined = pd.concat(parts, ignore_index=True)
    before = len(combined)
    combined = combined.drop_duplicates(subset=["医療機関名", "都道府県名"])
    print(f"\n報告年度 {year}: {before}行 → 重複除去後 {len(combined)}行")

    if SURGERY_PARQUET.exists():
        existing = pd.read_parquet(SURGERY_PARQUET)
        if year in existing["報告年度"].unique():
            print(f"既存の{year}年度データ（{(existing['報告年度']==year).sum()}行）を置き換えます")
            existing = existing[existing["報告年度"] != year]
        merged = pd.concat([existing, combined], ignore_index=True)
    else:
        merged = combined

    merged.to_parquet(SURGERY_PARQUET, index=False)
    print(f"\n=== 完了 === {SURGERY_PARQUET} 合計 {len(merged)}行")
    print(merged["報告年度"].value_counts().sort_index())


if __name__ == "__main__":
    main()
