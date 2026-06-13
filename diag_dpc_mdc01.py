"""
MDC01シートの列構造を診断するスクリプト。
使い方: py diag_dpc_mdc01.py <surgery_detail_xlsxのパス>
"""
import sys, re
import pandas as pd

if len(sys.argv) < 2:
    print("使い方: py diag_dpc_mdc01.py <excelファイルパス>")
    sys.exit(1)

path = sys.argv[1]
xl = pd.ExcelFile(path)
mdc01_sheets = [s for s in xl.sheet_names if s.strip() == "MDC01"]
if not mdc01_sheets:
    print(f"MDC01シートが見つかりません。シート一覧: {xl.sheet_names}")
    sys.exit(1)

sheet = mdc01_sheets[0]
df = pd.read_excel(path, sheet_name=sheet, header=None)
print(f"シート: '{sheet}', shape: {df.shape}")

row0 = df.iloc[0].tolist()
row2 = pd.Series(df.iloc[2]).replace("", None).ffill().tolist()
row3 = df.iloc[3].tolist()

# col15=99(総計), col16=97(手術有) のデータ行の実際の値を確認
print("\n=== DPC 010020 (くも膜下出血) col15(99) と col16(97) の実際のデータ ===")
data = df.iloc[4:].copy().reset_index(drop=True)
# 件数_総計(col15)が非NaNの行だけ
non_nan = data[data.iloc[:, 15].notna()]
print(f"col15(99) が non-NaN の行数: {len(non_nan)}")
print(f"col16(97) が non-NaN の行数: {data.iloc[:, 16].notna().sum()}")
print()
print("件数_総計(col15) が入っている病院の col16(97) の値：")
print(non_nan.iloc[:20, [0, 2, 15, 16]].to_string())

print()
print("=== col16(97) の値の分布 ===")
print(data.iloc[:, 16].value_counts(dropna=False).head(10))
