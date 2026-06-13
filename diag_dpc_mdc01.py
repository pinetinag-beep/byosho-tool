"""
MDC01シートの列構造を診断するスクリプト。
使い方: py diag_dpc_mdc01.py <surgery_detail_xlsxのパス>
例:    py diag_dpc_mdc01.py dpc_data/001682782.xlsx
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
print()

row0 = df.iloc[0].tolist()
row1 = df.iloc[1].tolist()
row2_raw = df.iloc[2].tolist()
row2 = pd.Series(df.iloc[2]).replace("", None).ffill().tolist()
row3 = df.iloc[3].tolist()

print("=== 行0-3 の先頭40列 ===")
for ci in range(min(40, len(row0))):
    print(f"  col{ci:3d}: row0={repr(row0[ci])!s:30} row1={repr(row1[ci])!s:25} row2_raw={repr(row2_raw[ci])!s:12} row2_ffill={repr(row2[ci])!s:12} row3={repr(row3[ci])}")

print()

# DPC 010020 (くも膜下出血) の列を探す
print("=== DPC 010020 の列を探す ===")
found_cols = []
for ci, v in enumerate(row0):
    vs = str(v).strip() if not (isinstance(v, float) and pd.isna(v)) else ""
    if "010020" in vs or vs == "010020":
        found_cols.append(ci)
        print(f"  col{ci}: row0={repr(v)}, row1={repr(row1[ci])}, row2_raw={repr(row2_raw[ci])}, row2_ffill={repr(row2[ci])}, row3={repr(row3[ci])}")

if not found_cols:
    print("  010020 が row0 に見つかりません")
    # 数値として格納されている可能性
    for ci, v in enumerate(row0):
        if isinstance(v, (int, float)) and not pd.isna(v):
            vs = str(int(v)) if v == int(v) else str(v)
            if vs == "10020":
                print(f"  (数値として) col{ci}: row0={repr(v)}, row3={repr(row3[ci])}")

print()
print("=== row3 (手術コード) のユニーク値 ===")
r3_vals = {}
for v in row3:
    vs = str(v).strip() if not (isinstance(v, float) and pd.isna(v)) else "NaN"
    r3_vals[vs] = r3_vals.get(vs, 0) + 1
for k, cnt in sorted(r3_vals.items(), key=lambda x: -x[1])[:20]:
    print(f"  {repr(k)!s:20}: {cnt}回")
