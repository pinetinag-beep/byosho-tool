#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
令和6年度以降の「病床機能報告 オープンデータ」新形式を取り込むビルドスクリプト。

2021〜2023年（旧形式）用の build_db.py とは別に用意している。新形式は
病棟票（機能区分・許可病床・入院基本料・患者フロー）と病院票（医師/看護師数・
CT/MRI等の設備・救急車受入件数）がファイル分割されているため、両者を
医療機関コードで結合して data_cache / ward_cache と同一スキーマに整形する。

新形式でも病棟票のヘッダー構造（5行目ヘッダー・6行目必須任意区分）は
2023年までと互換で、既存の load_mhlw_byosho_extended がそのまま使える。
病院票（設備・人員）だけは列レイアウトが別なので、ここで列名キーワード
一致でマッピングする。

使い方:
  python build_byosho_r7.py --dir byosho_file_R7 --year 2025 \
      --ward-glob '00171780[0-6].xlsx' --hosp 001717798.xlsx \
      --append data_cache.parquet ward_cache.parquet
"""
import argparse
import glob
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

from data_processor import load_mhlw_byosho_extended, load_mhlw_byosho, PREF_CODE_MAP

# ── 病院票（設備・人員）の列マッピング ─────────────────────────
# data_cache 列 → 病院票の列名（5行目ヘッダー、完全一致）
_STAFF_MAP = {
    "常勤医師数":           "施設全体_医師_常勤",
    "非常勤医師数":         "施設全体_医師_非常勤",
    "常勤看護師数":         "施設全体_看護師_常勤",
    "非常勤看護師数":       "施設全体_看護師_非常勤",
    "常勤理学療法士数":     "施設全体_理学療法士_常勤",
    "非常勤理学療法士数":   "施設全体_理学療法士_非常勤",
    "常勤作業療法士数":     "施設全体_作業療法士_常勤",
    "非常勤作業療法士数":   "施設全体_作業療法士_非常勤",
    "常勤言語聴覚士数":     "施設全体_言語聴覚士_常勤",
    "非常勤言語聴覚士数":   "施設全体_言語聴覚士_非常勤",
    "常勤薬剤師数":         "施設全体_薬剤師_常勤",
    "非常勤薬剤師数":       "施設全体_薬剤師_非常勤",
    "常勤診療放射線技師数": "施設全体_診療放射線技師_常勤",
    "非常勤診療放射線技師数": "施設全体_診療放射線技師_非常勤",
    "常勤臨床検査技師数":   "施設全体_臨床検査技師_常勤",
    "非常勤臨床検査技師数": "施設全体_臨床検査技師_非常勤",
}
_EQUIP_MAP = {
    "CT_64列以上":            "CT_マルチスライス_64列以上",
    "CT_16〜64列":            "CT_マルチスライス_16列以上64列未満",
    "CT_16列未満":            "CT_マルチスライス_16列未満",
    "CT_その他":              "CT_その他",
    "MRI_3T以上":             "MRI_3T以上",
    "MRI_1.5〜3T":            "MRI_1.5Ｔ以上3Ｔ未満",
    "MRI_1.5T未満":           "MRI_1.5Ｔ未満",
    "血管連続撮影装置台数":    "血管連続撮影装置",
    "SPECT台数":              "SPECT",
    "マンモグラフィ台数":      "マンモグラフィ",
    "PET台数":                "PET",
    "PETCT台数":              "PETCT",
    "PETMRI台数":             "PETMRI",
    "ガンマナイフ台数":        "ガンマナイフ",
    "サイバーナイフ台数":      "サイバーナイフ",
    "IMRT台数":               "強度変調放射線治療器（IMRT）",
    "内視鏡手術支援機器台数":  "内視鏡手術用支援機器",
}
_EMERGENCY_SRC = "救急車の受入件数"  # 年間受入件数（月別列は別に存在するので完全一致で取る）


def _clean_cols(cols) -> list[str]:
    return [str(c).replace("\n", "").strip() for c in cols]


def _find_exact_or_contains(cols: list[str], target: str) -> str | None:
    """完全一致を優先、なければ前後空白/全半角ゆらぎを吸収した包含一致で探す。"""
    if target in cols:
        return target
    t = target.replace(" ", "").replace("　", "")
    for c in cols:
        if c.replace(" ", "").replace("　", "") == t:
            return c
    return None


def load_hospital_facility(hosp_path: str, year: int) -> pd.DataFrame:
    """病院票（設備・人員）を data_cache スキーマの列に整形して返す。"""
    hp = pd.read_excel(hosp_path, header=4, skiprows=[5])
    hp.columns = _clean_cols(hp.columns)

    code_col = _find_exact_or_contains(hp.columns, "オープンデータ医療機関コード（R7）") \
        or next((c for c in hp.columns if "医療機関コード" in c and "医科" not in c and "歯科" not in c), None)
    if code_col is None:
        raise ValueError("病院票に医療機関コード列が見つかりません")

    out = pd.DataFrame()
    out["医療機関コード"] = hp[code_col].astype(str).str.strip()

    def _num(src_name):
        col = _find_exact_or_contains(list(hp.columns), src_name)
        if col is None:
            return np.zeros(len(hp))
        return pd.to_numeric(hp[col], errors="coerce").fillna(0).values

    for dst, src in {**_STAFF_MAP, **_EQUIP_MAP}.items():
        out[dst] = _num(src)

    out["救急搬送件数"] = _num(_EMERGENCY_SRC)

    # 合計台数（内訳の和）
    out["CT台数"]  = out["CT_64列以上"] + out["CT_16〜64列"] + out["CT_16列未満"] + out["CT_その他"]
    out["MRI台数"] = out["MRI_3T以上"] + out["MRI_1.5〜3T"] + out["MRI_1.5T未満"]

    # 医療機関単位に集約（病院票は基本1医療機関1行だが念のため合計）
    out = out.groupby("医療機関コード", as_index=False).sum(numeric_only=True)
    return out


def build(ward_paths: list[str], hosp_path: str, year: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    # ── 病棟票 → 病床機能集計 + ward_df ──
    hosp_beds_list, ward_list = [], []
    for f in ward_paths:
        data = Path(f).read_bytes()
        h, w = load_mhlw_byosho_extended(data, year=year)
        hosp_beds_list.append(h)
        ward_list.append(w)
        print(f"  病棟票 {Path(f).name}: 病院 {len(h):,} / 病棟 {len(w):,}")
    hosp_beds = pd.concat(hosp_beds_list, ignore_index=True)
    ward_df   = pd.concat(ward_list, ignore_index=True)
    hosp_beds["医療機関コード"] = hosp_beds["医療機関コード"].astype(str).str.strip()

    # ── 病院票 → 設備・人員 ──
    fac = load_hospital_facility(hosp_path, year)
    print(f"  病院票 {Path(hosp_path).name}: {len(fac):,}医療機関")

    # ── 結合 ──
    hosp_df = hosp_beds.merge(fac, on="医療機関コード", how="left")
    # 設備・人員が欠損（病院票に無い）の医療機関は 0 埋め
    for c in list(_STAFF_MAP) + list(_EQUIP_MAP) + ["救急搬送件数", "CT台数", "MRI台数"]:
        if c in hosp_df.columns:
            hosp_df[c] = hosp_df[c].fillna(0)
    return hosp_df, ward_df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="byosho_file_R7")
    ap.add_argument("--year", type=int, default=2025)
    ap.add_argument("--ward-glob", default="00171780[0-6].xlsx")
    ap.add_argument("--hosp", default="001717798.xlsx")
    ap.add_argument("--append", nargs=2, metavar=("HOSP_PARQUET", "WARD_PARQUET"),
                    help="既存parquetに追記する場合に指定")
    args = ap.parse_args()

    base = Path(args.dir)
    ward_paths = sorted(str(p) for p in base.glob(args.ward_glob))
    hosp_path = str(base / args.hosp)
    print(f"病棟票: {len(ward_paths)}ファイル / 病院票: {Path(hosp_path).name} / 年度: {args.year}")

    hosp_df, ward_df = build(ward_paths, hosp_path, args.year)
    print(f"\n{args.year}年度: 病院 {len(hosp_df):,} / 病棟 {len(ward_df):,} / "
          f"都道府県 {hosp_df['都道府県名'].nunique() if '都道府県名' in hosp_df.columns else '?'}")

    if args.append:
        hp_path, wp_path = args.append
        old_h = pd.read_parquet(hp_path)
        old_w = pd.read_parquet(wp_path)
        # 同年度が既にあれば置き換え
        old_h = old_h[old_h["報告年度"] != args.year]
        old_w = old_w[old_w["報告年度"] != args.year]
        # 列を揃える（和集合）
        for c in old_h.columns:
            if c not in hosp_df.columns:
                hosp_df[c] = np.nan
        for c in hosp_df.columns:
            if c not in old_h.columns:
                old_h[c] = np.nan
        for c in old_w.columns:
            if c not in ward_df.columns:
                ward_df[c] = np.nan
        for c in ward_df.columns:
            if c not in old_w.columns:
                old_w[c] = np.nan
        new_h = pd.concat([old_h, hosp_df[old_h.columns]], ignore_index=True)
        new_w = pd.concat([old_w, ward_df[old_w.columns]], ignore_index=True)
        new_h.to_parquet(hp_path, index=False)
        new_w.to_parquet(wp_path, index=False)
        print(f"\n追記完了: {hp_path} = {len(new_h):,}行 (年度: {sorted(new_h['報告年度'].unique())})")
        print(f"          {wp_path} = {len(new_w):,}行")
    else:
        hosp_df.to_parquet("data_cache_R7.parquet", index=False)
        ward_df.to_parquet("ward_cache_R7.parquet", index=False)
        print("\n単年度出力: data_cache_R7.parquet / ward_cache_R7.parquet")


if __name__ == "__main__":
    main()
