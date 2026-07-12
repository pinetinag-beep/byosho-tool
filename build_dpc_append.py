#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DPC公開データの追加年度を、既存の dpc_*.parquet に追記する。

本番サーバーにはDuckDBが無くparquet直接運用のため、build_dpc.py の
loader関数を直接使って年度別DataFrameを作り、既存parquetに追記する。
年度は各ファイルの現在年シート（R0x全体 等）で検証済みの正しい値を渡す。

  令和4年度 2022 → DPC_file_R4
  令和5年度 2023 → DPC_file_R5
  （令和6年度 2024 は既存parquetに収録済み）
"""
import glob
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

from build_dpc import (
    detect_file_type,
    load_gaiyou, load_procedure_stats, load_mdc_cases,
    load_mdc_ratio, load_readmission, load_surgery_detail,
)

BASE = Path(__file__).parent

# (category, parquet, loader)
TABLES = [
    ("gaiyou",          "dpc_hospitals.parquet",       load_gaiyou),
    ("procedure_stats", "dpc_procedure_stats.parquet", load_procedure_stats),
    ("mdc_cases",       "dpc_mdc_cases.parquet",       load_mdc_cases),
    ("mdc_ratio",       "dpc_mdc_ratio.parquet",       load_mdc_ratio),
    ("readmission",     "dpc_readmission.parquet",     load_readmission),
    ("surgery_detail",  "dpc_surgery_detail.parquet",  load_surgery_detail),
]

JOBS = [(2022, "DPC_file_R4"), (2023, "DPC_file_R5")]


def _optimize_dtypes(df: pd.DataFrame) -> pd.DataFrame:
    """繰り返し文字列をcategory・数値をdowncastしてメモリを削減する。
    surgery_detail は630万行あり、素のobject/float64だと1GB超でStreamlit
    Cloudのメモリ制限を単体で超えるため必須（category化で約1/5になる）。"""
    for c in ["施設名", "疾患名", "MDC", "dpc6", "受理届出名称", "受理記号"]:
        if c in df.columns and str(df[c].dtype) == "object":
            df[c] = df[c].astype("category")
    for c in ["年度", "告示番号"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").astype("int32")
    for c in df.columns:
        if str(df[c].dtype) == "float64":
            df[c] = df[c].astype("float32")
    return df


def _union_append(parquet_path: Path, new_df: pd.DataFrame, year: int):
    """既存parquetから同年度を除き、列の和集合で new_df を追記して書き戻す。"""
    if parquet_path.exists():
        old = pd.read_parquet(parquet_path)
        if "年度" in old.columns:
            old = old[old["年度"] != year]
    else:
        old = pd.DataFrame()
    for c in old.columns:
        if c not in new_df.columns:
            new_df[c] = np.nan
    for c in new_df.columns:
        if c not in old.columns:
            old[c] = np.nan
    cols = list(old.columns) if len(old.columns) else list(new_df.columns)
    combined = pd.concat(
        ([old[cols]] if len(old) else []) + [new_df[cols]],
        ignore_index=True,
    )
    combined = _optimize_dtypes(combined)
    combined.to_parquet(parquet_path, index=False)
    return combined


def main():
    for year, folder in JOBS:
        d = BASE / folder
        if not d.exists():
            print(f"⚠ {folder} が無いのでスキップ（{year}）")
            continue
        files = glob.glob(str(d / "*.xlsx"))
        # 種別に分類
        cat: dict[str, list[str]] = {}
        for f in files:
            cat.setdefault(detect_file_type(f), []).append(f)
        print(f"\n=== {year}年度 ({folder}) {len(files)}ファイル ===")

        for category, parquet, loader in TABLES:
            paths = cat.get(category, [])
            if not paths:
                print(f"  {category}: ファイルなし（スキップ）")
                continue
            dfs = []
            for f in paths:
                try:
                    df = loader(f, year)
                    if df is not None and not df.empty:
                        dfs.append(df)
                except Exception as e:
                    print(f"    [error] {Path(f).name}: {e}")
            if not dfs:
                print(f"  {category}: 有効データなし")
                continue
            new_df = pd.concat(dfs, ignore_index=True)
            combined = _union_append(BASE / parquet, new_df, year)
            yrs = sorted(int(y) for y in combined["年度"].dropna().unique()) if "年度" in combined else []
            print(f"  {category:16s} → {parquet}: +{len(new_df):,}行  (全年度: {yrs})")

    print("\n=== 完了：最終年度構成 ===")
    for _, parquet, _ in TABLES:
        p = BASE / parquet
        if p.exists():
            d = pd.read_parquet(p, columns=["年度"])
            print(f"  {parquet}: {d['年度'].value_counts().sort_index().to_dict()}")


if __name__ == "__main__":
    main()
