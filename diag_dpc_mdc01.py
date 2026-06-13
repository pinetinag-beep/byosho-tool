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
row1 = df.iloc[1].tolist()
row2 = pd.Series(df.iloc[2]).replace("", None).ffill().tolist()
row3 = df.iloc[3].tolist()

# DPC 010020 が始まる列を探す
print("\n=== DPC 010020 のヘッダー行 (col13〜col30) ===")
for ci in range(13, min(31, len(row0))):
    print(f"  col{ci}: row0={repr(row0[ci])}, row1={repr(row1[ci])}, row2={repr(row2[ci])}, row3={repr(row3[ci])}")

data = df.iloc[4:].copy().reset_index(drop=True)

print("\n=== DPC 010020 周辺: col15(99総計)〜col25 の non-NaN 件数 ===")
for ci in range(15, min(26, len(row0))):
    cnt = data.iloc[:, ci].notna().sum()
    print(f"  col{ci} [row3={repr(row3[ci])}]: non-NaN={cnt}")

# 件数_総計(col15)が非NaNの行（手術症例がある病院）
non_nan = data[data.iloc[:, 15].notna()]
print(f"\ncol15(99) が non-NaN の行数: {len(non_nan)}")

print("\n=== 件数_総計あり病院 上位20件: col15〜col24 ===")
end_col = min(25, df.shape[1])
header = "  " + "\t".join([f"c{i}({repr(row3[i])[:6]})" for i in range(15, end_col)])
print(header)
for _, row in non_nan.head(20).iterrows():
    vals = "\t".join([str(row.iloc[i]) for i in range(15, end_col)])
    print(f"  {vals}")

print("\n=== col16〜col24 の値分布（上位5） ===")
for ci in range(16, min(25, len(row0))):
    vc = data.iloc[:, ci].value_counts(dropna=False).head(5)
    print(f"  col{ci} [row3={repr(row3[ci])}]: {dict(vc)}")
