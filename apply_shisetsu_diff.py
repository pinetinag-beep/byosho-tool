"""
施設基準届出「新規・変更」「失効」の差分PDF群を、既存の全件スナップショット
shisetsu_kijun_cache.parquet に反映して最新化する。

使い方:
  python apply_shisetsu_diff.py shisetsu_new_20260712

フォルダ内の「<都道府県> 新規.pdf」「<都道府県> 失効.pdf」を都道府県ごとに
ペアで読み込み、以下の順で適用する:
  1. 失効: (都道府県名, 医療機関番号, 受理記号) が一致する既存行を削除
  2. 新規・変更: 同じキーの既存行があれば削除してから新しい内容で追加
     （＝変更届は上書き、新規届は追加）
適用後、施設種別（病院／有床診療所／無床診療所）をデータセット全体で
再判定し、category dtype を再付与して保存する。実行前に元ファイルを
.bak として退避する。
"""
import sys
import glob
from pathlib import Path

import pandas as pd

from build_shisetsu_kijun import _classify_facility_types
from parse_shisetsu_shinki_pdf import (
    parse_pdf as parse_shinki, _guess_pref_from_filename,
)
from parse_shisetsu_shikkou_pdf import parse_pdf as parse_shikkou

BASE = Path(__file__).parent
PARQUET_PATH = BASE / "shisetsu_kijun_cache.parquet"

CATEGORY_COLS = [
    "都道府県コード", "都道府県名", "受理届出名称", "受理記号",
    "病棟種別", "病床区分", "区分", "年月", "施設種別",
]


def _discover_pairs(folder: Path) -> dict:
    """フォルダ内のPDFを都道府県ごとに {pref: {"新規": path, "失効": path}} にまとめる。"""
    pairs: dict[str, dict[str, str]] = {}
    for f in sorted(glob.glob(str(folder / "*.pdf"))):
        stem = Path(f).stem
        if stem.endswith("新規"):
            pref = stem[:-2].strip()
            pairs.setdefault(pref, {})["新規"] = f
        elif stem.endswith("失効"):
            pref = stem[:-2].strip()
            pairs.setdefault(pref, {})["失効"] = f
        else:
            print(f"  [skip] 認識できないファイル名: {f}")
    return pairs


def main():
    if len(sys.argv) < 2:
        print("使い方: python apply_shisetsu_diff.py <フォルダ>")
        sys.exit(1)
    folder = Path(sys.argv[1])
    if not folder.exists():
        print(f"フォルダが見つかりません: {folder}")
        sys.exit(1)

    pairs = _discover_pairs(folder)
    print(f"対象都道府県: {len(pairs)}件")

    all_new = []
    all_shikkou = []
    for pref, files in pairs.items():
        # フォルダ内ファイル名は短縮県名（例:"岡山"）だが、既存データの
        # 都道府県名列は正式名称（例:"岡山県"）なので、そのまま短縮名を
        # ヒントとして渡すとキー不一致でマージが全く効かなくなる。
        # ファイル名から正式名称を解決し直す。
        full_pref = _guess_pref_from_filename(files.get("新規") or files.get("失効", ""))
        if "新規" in files:
            df = parse_shinki(files["新規"], full_pref)
            print(f"  {pref} 新規: {len(df)}行")
            if not df.empty:
                all_new.append(df)
        if "失効" in files:
            df = parse_shikkou(files["失効"], full_pref)
            print(f"  {pref} 失効: {len(df)}行")
            if not df.empty:
                all_shikkou.append(df)

    new_df = pd.concat(all_new, ignore_index=True) if all_new else pd.DataFrame()
    shikkou_df = pd.concat(all_shikkou, ignore_index=True) if all_shikkou else pd.DataFrame()
    print(f"\n新規・変更 合計: {len(new_df)}行 / 失効 合計: {len(shikkou_df)}行")

    if not PARQUET_PATH.exists():
        print(f"既存データが見つかりません: {PARQUET_PATH}")
        sys.exit(1)

    backup_path = PARQUET_PATH.with_suffix(".parquet.bak")
    old = pd.read_parquet(PARQUET_PATH)
    old.to_parquet(backup_path, index=False)
    print(f"バックアップ: {backup_path}（{len(old):,}行）")

    for col in CATEGORY_COLS:
        if col in old.columns and str(old[col].dtype) == "category":
            old[col] = old[col].astype(str)

    before_n = len(old)

    # ── 既存の医療機関名称・住所を、新規行の同一医療機関（都道府県+医療機関番号）
    #    から引き継ぐ（PDFの折り返し結合でスペースが欠落する等の表記ゆれを防ぐ）
    if not new_df.empty:
        name_lookup = (
            old[["都道府県名", "医療機関番号", "医療機関名称", "医療機関名_正規化", "住所"]]
            .drop_duplicates(subset=["都道府県名", "医療機関番号"])
            .rename(columns={
                "医療機関名称": "_既存名称", "医療機関名_正規化": "_既存正規化", "住所": "_既存住所",
            })
        )
        new_df = new_df.merge(name_lookup, on=["都道府県名", "医療機関番号"], how="left")
        has_existing = new_df["_既存名称"].notna()
        new_df.loc[has_existing, "医療機関名称"] = new_df.loc[has_existing, "_既存名称"]
        new_df.loc[has_existing, "医療機関名_正規化"] = new_df.loc[has_existing, "_既存正規化"]
        has_existing_addr = has_existing & new_df["_既存住所"].fillna("").ne("")
        new_df.loc[has_existing_addr, "住所"] = new_df.loc[has_existing_addr, "_既存住所"]
        new_df = new_df.drop(columns=["_既存名称", "_既存正規化", "_既存住所"])

    def _key_set(df, cols=("都道府県名", "医療機関番号", "受理記号")):
        if df.empty:
            return set()
        return set(zip(*[df[c] for c in cols]))

    shikkou_keys = _key_set(shikkou_df)
    new_keys = _key_set(new_df)
    remove_keys = shikkou_keys | new_keys

    if remove_keys:
        old_key = list(zip(old["都道府県名"], old["医療機関番号"], old["受理記号"]))
        mask_remove = pd.Series(old_key, index=old.index).isin(remove_keys)
        removed_n = int(mask_remove.sum())
        kept = old[~mask_remove].copy()
    else:
        removed_n = 0
        kept = old

    combined = pd.concat([kept, new_df], ignore_index=True) if not new_df.empty else kept

    combined["施設種別"] = _classify_facility_types(combined)

    for col in CATEGORY_COLS:
        if col in combined.columns:
            combined[col] = combined[col].astype("category")

    combined.to_parquet(PARQUET_PATH, index=False)

    print(f"\n=== 完了 ===")
    print(f"適用前: {before_n:,}行")
    print(f"既存行のうち削除（失効＋変更の旧内容）: {removed_n:,}行")
    print(f"新規追加（新規＋変更の新内容）: {len(new_df):,}行")
    print(f"適用後: {len(combined):,}行")


if __name__ == "__main__":
    main()
