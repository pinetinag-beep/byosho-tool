#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
令和4〜7年度（2022〜2025）の病床機能報告オープンデータを、各ファイルの
「現在の機能」列で検証した正しい年度ラベルで一括ビルドし、
data_cache.parquet / ward_cache.parquet を作り直す。

背景: 旧 build_db.py はファイルID 001299xxx を「2023」とラベル付けして
いたが、実データの現在機能列は「2024（令和6）」＝令和6年度だった
（既存の"2023"データと令和6年度ファイルが100%一致することで確認）。
本スクリプトは各生ファイルの現在機能列で年度を確定した上で再構築する。

年度対応（現在機能列で検証済み）:
  令和4年度 2022 → byosho_file_R4 (001151xxx)  病棟票8地域＋病院票957
  令和5年度 2023 → byosho_file_R5 (001571xxx)  病棟票7地域＋病院票863
  令和6年度 2024 → byosho_file_R6 (001299xxx)  病棟票7地域＋病院票890
  令和7年度 2025 → byosho_file_R7 (001717xxx)  病棟票7地域＋病院票798
"""
import glob
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

from build_byosho_r7 import build

BASE = Path(__file__).parent

# (year, folder, 病棟票IDリスト, 病院票ID)
JOBS = [
    (2022, "byosho_file_R4",
     ["001151960", "001151962", "001151965", "001151966",
      "001151968", "001151969", "001151970", "001151971"], "001151957"),
    (2023, "byosho_file_R5",
     ["001571866", "001571868", "001571869", "001571870",
      "001571872", "001571873", "001571874"], "001571863"),
    (2024, "byosho_file_R6",
     ["001299892", "001299893", "001299894", "001299895",
      "001299901", "001299914", "001299921"], "001299890"),
    (2025, "byosho_file_R7",
     ["001717800", "001717801", "001717802", "001717803",
      "001717804", "001717805", "001717806"], "001717798"),
]


def _find(folder, id_):
    hits = sorted(glob.glob(str(BASE / folder / f"{id_}*.xlsx")))
    if not hits:
        raise FileNotFoundError(f"{folder}/{id_}*.xlsx が見つかりません")
    return hits[0]


def main():
    hosp_all, ward_all = [], []
    for year, folder, ward_ids, hosp_id in JOBS:
        if not (BASE / folder).exists():
            print(f"⚠ {folder} が無いのでスキップ（年度 {year}）")
            continue
        ward_paths = [_find(folder, i) for i in ward_ids]
        hosp_path = _find(folder, hosp_id)
        print(f"\n=== {year}年度 ({folder}) 病棟票{len(ward_paths)} + 病院票 ===")
        h, w = build(ward_paths, hosp_path, year)
        print(f"  {year}: 病院 {len(h):,} / 病棟 {len(w):,} / 都道府県 {h['都道府県名'].nunique()}")
        hosp_all.append(h)
        ward_all.append(w)

    # 列の和集合で揃えて結合
    def _union_concat(dfs):
        cols = []
        for d in dfs:
            for c in d.columns:
                if c not in cols:
                    cols.append(c)
        fixed = []
        for d in dfs:
            for c in cols:
                if c not in d.columns:
                    d[c] = np.nan
            fixed.append(d[cols])
        return pd.concat(fixed, ignore_index=True)

    hosp_df = _union_concat(hosp_all)
    ward_df = _union_concat(ward_all)

    # 旧schemaにあった空列（住所・url）を互換のため付与（アプリは .get() 参照なので実害はないが揃える）
    for c in ["住所", "url"]:
        if c not in hosp_df.columns:
            hosp_df[c] = np.nan

    hosp_df.to_parquet(BASE / "data_cache.parquet", index=False)
    ward_df.to_parquet(BASE / "ward_cache.parquet", index=False)

    print("\n=== 完了 ===")
    print("data_cache 年度別病院数:", hosp_df["報告年度"].value_counts().sort_index().to_dict())
    print("ward_cache 年度別病棟数:", ward_df["報告年度"].value_counts().sort_index().to_dict())
    print("data_cache 列数:", len(hosp_df.columns), "/ ward_cache 列数:", len(ward_df.columns))


if __name__ == "__main__":
    main()
