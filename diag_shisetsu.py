"""
施設票Excelの列名を確認するスクリプト。
実行: py diag_shisetsu.py
結果: shisetsu_cols.txt に出力される
"""
import io, urllib.request, pandas as pd

URL = "https://www.mhlw.go.jp/content/10800000/001299890.xlsx"
OUT = "shisetsu_cols.txt"

print("Downloading shisetsu...")
req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
data = urllib.request.urlopen(req, timeout=60).read()
print(f"  -> {len(data)//1024} KB")

df = pd.read_excel(io.BytesIO(data), header=4, skiprows=[5])
df.columns = [str(c).strip() for c in df.columns]

lines = []
lines.append(f"Total columns: {len(df.columns)}\n")
lines.append("\n--- All columns (index: name) ---\n")
for i, c in enumerate(df.columns):
    lines.append(f"{i:4d}: {c}\n")

with open(OUT, "w", encoding="utf-8") as f:
    f.writelines(lines)

print(f"Done. Results saved to {OUT}")
print(f"  Total: {len(df.columns)} columns")
# quick preview of columns around 医師
joko_cols = [(i, c) for i, c in enumerate(df.columns) if "常" in c or "医師" in c or "看護" in c]
print("  Staff-related columns (first 20):")
for i, c in joko_cols[:20]:
    print(f"    [{i}] {c}")
