"""
DPCデータをDuckDBに取り込むスクリプト。

使い方:
    py build_dpc.py --dir C:\path\to\dpc_data --match dpc_matching_full.csv

引数:
    --dir    DPCファイルが入ったフォルダ（デフォルト: dpc_data）
    --match  DPC↔病床報告 突合CSVのパス（デフォルト: dpc_matching_full.csv）
    --db     DuckDBファイルのパス（デフォルト: data/byosho.duckdb）
    --year   データ年度（デフォルト: 2024 = 令和6年度）
"""

import argparse
import glob
import os
import re
import sys

import duckdb
import numpy as np
import pandas as pd

MDC_NAMES = {
    "01": "神経系疾患",
    "02": "眼科系疾患",
    "03": "耳鼻咽喉科系疾患",
    "04": "呼吸器系疾患",
    "05": "循環器系疾患",
    "06": "消化器系疾患・肝臓・胆道・膵臓疾患",
    "07": "筋骨格系疾患",
    "08": "皮膚・皮下組織の疾患",
    "09": "乳房の疾患",
    "10": "内分泌・栄養・代謝に関する疾患",
    "11": "腎・尿路系疾患及び男性生殖器系疾患",
    "12": "女性生殖器系疾患及び産褥期疾患・異常妊娠分娩",
    "13": "血液・造血器・免疫臓器の疾患",
    "14": "新生児疾患、先天性奇形",
    "15": "小児疾患",
    "16": "外傷・熱傷・中毒",
    "17": "精神疾患",
    "18": "その他",
}


def detect_file_type(path: str) -> str:
    try:
        xl = pd.ExcelFile(path)
        sheets = xl.sheet_names
    except Exception:
        return "unknown"

    if "施設概要表" in sheets:
        return "gaiyou"
    if "施設別MDC別比率" in sheets:
        return "mdc_ratio"
    if "高度医療" in sheets:
        return "procedure_stats"
    if "件数" in sheets or "件数 " in sheets:
        return "mdc_cases"
    if any(re.match(r"^MDC\d{2}$", s) for s in sheets):
        return "surgery_detail"
    if any("DPC6桁" in s for s in sheets):
        return "type_aggregate"  # 施設類型別 → スキップ
    # 再入院：シート名が年度名で、列に「再入院」を含む
    for s in sheets:
        try:
            df_peek = pd.read_excel(path, sheet_name=s, header=None, nrows=3)
            content = df_peek.to_string()
            if "再入院" in content or "再転棟" in content:
                return "readmission"
        except Exception:
            pass
    return "unknown"


# ── 施設概要表 ──────────────────────────────────────────────────────────────
def load_gaiyou(path: str, year: int) -> pd.DataFrame:
    df = pd.read_excel(path, sheet_name="施設概要表", header=0)
    # 列名を正規化（改行・※付き番号を除去）
    df.columns = [re.sub(r"[\n※].*", "", str(c)).strip() for c in df.columns]
    rename = {
        "告示番号": "告示番号",
        "通番": "通番",
        "市町村番号": "市町村番号",
        "都道\r\n府県": "都道府県",
        "都道\n府県": "都道府県",
        "都道府県": "都道府県",
        "施設名": "施設名",
        "病院類型": "病院類型",
        "DPC算定病床数": "DPC算定病床数",
        "DPC算定病床の入院基本料": "入院基本料",
        "DPC算定病床割合": "DPC算定病床割合",
        "回復期リハビリテーション病棟入院料等病床数": "回復期病床数",
        "地域包括ケア病棟入院料病床数": "地域包括病床数",
        "地域包括医療病棟入院料病床数": "地域包括医療病床数",
        "精神病床数": "精神病床数",
        "療養病床数": "療養病床数",
        "結核病床数": "結核病床数",
        "病床総数": "病床総数",
        "病院指標URL": "病院指標URL",
        "医療の質指標URL": "医療の質指標URL",
    }
    # 提出月数列を検出
    for c in df.columns:
        if "提出月数" in str(c):
            rename[c] = "提出月数"
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
    df = df.dropna(subset=["告示番号", "施設名"])
    df["告示番号"] = df["告示番号"].astype(int)
    df["年度"] = year
    keep = [
        "年度", "告示番号", "通番", "市町村番号", "都道府県", "施設名", "病院類型",
        "DPC算定病床数", "入院基本料", "DPC算定病床割合", "回復期病床数",
        "地域包括病床数", "地域包括医療病床数", "精神病床数", "療養病床数",
        "結核病床数", "病床総数", "提出月数", "病院指標URL", "医療の質指標URL",
    ]
    return df[[c for c in keep if c in df.columns]]


# ── 手術・化学療法・放射線・全身麻酔 ─────────────────────────────────────────
def load_procedure_stats(path: str, year: int) -> pd.DataFrame:
    df = pd.read_excel(path, sheet_name="高度医療", header=None)
    # 行0-2がヘッダー、行3以降がデータ
    # 列構成: 告示番号, 通番, 施設名, 件数(総数,手術有,化学療法有,放射線療法有,救急車搬送有,いずれか有,全身麻酔), 割合(同順)
    data = df.iloc[3:].copy()
    data.columns = range(len(data.columns))
    data = data.rename(columns={
        0: "告示番号", 1: "通番", 2: "施設名",
        3: "件数_総数", 4: "件数_手術有", 5: "件数_化学療法有",
        6: "件数_放射線療法有", 7: "件数_救急車搬送有", 8: "件数_いずれか有",
        9: "件数_全身麻酔",
        10: "割合_総数", 11: "割合_手術有", 12: "割合_化学療法有",
        13: "割合_放射線療法有", 14: "割合_救急車搬送有", 15: "割合_いずれか有",
        16: "割合_全身麻酔",
    })
    data = data.dropna(subset=["告示番号", "施設名"])
    data["告示番号"] = pd.to_numeric(data["告示番号"], errors="coerce")
    data = data.dropna(subset=["告示番号"])
    data["告示番号"] = data["告示番号"].astype(int)
    data["年度"] = year
    num_cols = [c for c in data.columns if c not in ("告示番号", "通番", "施設名", "年度")]
    for c in num_cols:
        data[c] = pd.to_numeric(data[c], errors="coerce")
    return data


# ── MDC別件数（手術有無別） ───────────────────────────────────────────────────
def load_mdc_cases(path: str, year: int) -> pd.DataFrame:
    sheet = "件数" if "件数" in pd.ExcelFile(path).sheet_names else pd.ExcelFile(path).sheet_names[0]
    df = pd.read_excel(path, sheet_name=sheet, header=None)
    # 行0: 年度ヘッダー（無視）, 行1: 列ヘッダー（告示番号,通番,施設名,手術,MDC01..）, 行2: DPC患者数ラベル
    # 行3以降: データ（各病院2行: 手術「無し」「有り」）
    header_row = df.iloc[1].tolist()
    mdc_cols = {}
    for i, val in enumerate(header_row):
        if isinstance(val, str) and re.match(r"^\d{2}$", val.strip()):
            mdc_cols[i] = f"MDC{val.strip()}"

    records = []
    data_rows = df.iloc[3:].reset_index(drop=True)
    i = 0
    while i < len(data_rows):
        row = data_rows.iloc[i]
        ban = row.iloc[0]
        if pd.isna(ban) or str(ban).strip() == "":
            i += 1
            continue
        try:
            ban = int(float(ban))
        except (ValueError, TypeError):
            i += 1
            continue
        name = row.iloc[2]

        def _parse_row(r):
            flag = str(r.iloc[3]).strip()
            rec = {"年度": year, "告示番号": ban, "施設名": name, "手術有無": flag}
            for col_idx, mdc_name in mdc_cols.items():
                rec[mdc_name] = pd.to_numeric(r.iloc[col_idx], errors="coerce")
            return rec

        # 1行目（通常は手術「無し」）
        records.append(_parse_row(row))

        # 次行が同病院の「有り」行（告示番号=NaN）なら一緒に取り込む
        if i + 1 < len(data_rows):
            next_row = data_rows.iloc[i + 1]
            if pd.isna(next_row.iloc[0]) or str(next_row.iloc[0]).strip() == "":
                records.append(_parse_row(next_row))
                i += 2
                continue
        i += 1

    return pd.DataFrame(records)


# ── 施設別MDC比率 ──────────────────────────────────────────────────────────
def load_mdc_ratio(path: str, year: int) -> pd.DataFrame:
    df = pd.read_excel(path, sheet_name="施設別MDC別比率", header=None)
    # 行0: ヘッダー行1（告示番号,通番,施設名,比率,NaN..）
    # 行1: MDC列名（NaN,NaN,NaN,MDC01,MDC02..）
    mdc_cols = {}
    header = df.iloc[1].tolist()
    for i, val in enumerate(header):
        if isinstance(val, str) and val.strip().startswith("MDC"):
            mdc_cols[i] = val.strip()

    data = df.iloc[2:].copy().reset_index(drop=True)
    records = []
    for _, row in data.iterrows():
        ban = row.iloc[0]
        if pd.isna(ban):
            continue
        try:
            ban = int(float(ban))
        except (ValueError, TypeError):
            continue
        rec = {"年度": year, "告示番号": ban, "施設名": row.iloc[2]}
        for col_idx, mdc_name in mdc_cols.items():
            rec[mdc_name] = pd.to_numeric(row.iloc[col_idx], errors="coerce")
        records.append(rec)
    return pd.DataFrame(records)


# ── 再入院・再転棟 ─────────────────────────────────────────────────────────
def load_readmission(path: str, year: int) -> pd.DataFrame:
    xl = pd.ExcelFile(path)
    # シート名が年度名のものを使用
    sheet = xl.sheet_names[0]
    df = pd.read_excel(path, sheet_name=sheet, header=None)
    # 行0: 大ヘッダー, 行1: 小ヘッダー, 行2以降: データ
    # 最低限: 告示番号, 施設名, 再入院率, 再転棟率
    data = df.iloc[2:].copy().reset_index(drop=True)
    records = []
    for _, row in data.iterrows():
        ban = row.iloc[0]
        if pd.isna(ban):
            continue
        try:
            ban = int(float(ban))
        except (ValueError, TypeError):
            continue
        rec = {
            "年度": year,
            "告示番号": ban,
            "施設名": row.iloc[2],
            "再入院率": pd.to_numeric(row.iloc[3], errors="coerce"),
            "再転棟率": pd.to_numeric(row.iloc[4], errors="coerce"),
        }
        # 3日以内, 4-7日, 8-14日, 15-28日の内訳も格納
        col_names = ["再入院_3日以内", "再入院_4-7日", "再入院_8-14日", "再入院_15-28日"]
        for j, cn in enumerate(col_names):
            idx = 5 + j
            if idx < len(row):
                rec[cn] = pd.to_numeric(row.iloc[idx], errors="coerce")
        records.append(rec)
    return pd.DataFrame(records)


# ── 疾患別手術別集計（施設別 MDCxx） ──────────────────────────────────────────
def load_surgery_detail(path: str, year: int) -> pd.DataFrame:
    xl = pd.ExcelFile(path)
    # MDCxxシートを取得（シート名の末尾スペースを除去して照合）
    mdc_sheets = [s for s in xl.sheet_names if re.match(r"^MDC\d{2}$", s.strip())]
    all_records = []

    for sheet in mdc_sheets:
        mdc_code = sheet.strip()  # e.g. "MDC06"（末尾スペースを除去）
        df = pd.read_excel(path, sheet_name=sheet, header=None)

        # ヘッダー行を解析
        # 行0: DPC6桁コード（繰り返し）
        # 行1: 疾患名（繰り返し）
        # 行2: 件数 or 在院日数（繰り返し）
        # 行3: 手術コード（99=総計, 97=手術有, etc.）
        # 行4以降: データ（1施設1行）

        row0 = df.iloc[0].tolist()   # DPCコード
        row1 = df.iloc[1].tolist()   # 疾患名
        row2 = df.iloc[2].tolist()   # 件数/在院日数
        row3 = df.iloc[3].tolist()   # 手術コード

        # 告示番号(col0), 通番(col1), 施設名(col2) → 3列固定
        # col 3以降が DPC別データ
        # 総計(手術コード=99)の件数列だけ抽出（全件数）
        # DPC6桁×（件数_99, 件数_97, 在院日数_99）を収集

        # DPCコードを前方fill
        current_dpc = None
        current_disease = None
        col_map = []  # (col_idx, dpc6, disease, metric, surgery_code)

        for ci in range(3, len(row0)):
            v0 = row0[ci]
            v1 = row1[ci]
            v2 = str(row2[ci]).strip() if not pd.isna(row2[ci]) else ""
            v3 = str(row3[ci]).strip() if not pd.isna(row3[ci]) else ""

            if isinstance(v0, str) and re.match(r"^\d{5}[\dx]$", v0.strip(), re.IGNORECASE):
                current_dpc = v0.strip()
            if isinstance(v1, str) and v1.strip() and not v1.strip().startswith("NaN"):
                current_disease = v1.strip()

            if current_dpc is None:
                continue

            # 手術コード99(総計)と97(手術有)の件数・在院日数のみ保存
            if v3 in ("99", "97") and v2 in ("件数", "在院日数"):
                col_map.append((ci, current_dpc, current_disease or "", v2, v3))

        if not col_map:
            continue

        data_rows = df.iloc[4:].copy().reset_index(drop=True)
        for _, row in data_rows.iterrows():
            ban = row.iloc[0]
            if pd.isna(ban):
                continue
            try:
                ban = int(float(ban))
            except (ValueError, TypeError):
                continue
            name = row.iloc[2]

            # DPCコード別に集約（pivot的に）
            dpc_vals = {}
            for ci, dpc6, disease, metric, surg_code in col_map:
                key = (dpc6, disease)
                if key not in dpc_vals:
                    dpc_vals[key] = {"dpc6": dpc6, "疾患名": disease}
                col_key = f"{'件数' if metric == '件数' else '在院日数'}_{'総計' if surg_code == '99' else '手術有'}"
                dpc_vals[key][col_key] = pd.to_numeric(row.iloc[ci], errors="coerce")

            for (dpc6, disease), vals in dpc_vals.items():
                rec = {
                    "年度": year, "告示番号": ban, "施設名": name,
                    "MDC": mdc_code, **vals,
                }
                all_records.append(rec)

    if not all_records:
        return pd.DataFrame()
    return pd.DataFrame(all_records)


# ── マッチングテーブル ────────────────────────────────────────────────────────
def load_match(match_csv: str) -> pd.DataFrame:
    df = pd.read_csv(match_csv, encoding="utf-8-sig")
    # dpc_matching_full.csv: DPC施設名, 都道府県, 病院類型, DPC算定病床数, 病床総数, マッチ状態, スコア, 病床報告施設名, 候補2, 候補3
    df = df[["DPC施設名", "病床報告施設名", "都道府県", "マッチ状態", "スコア"]].copy()
    df = df.rename(columns={"DPC施設名": "DPC施設名", "病床報告施設名": "病床報告施設名"})
    return df


# ── DB書き込み ────────────────────────────────────────────────────────────────
def upsert_table(con: duckdb.DuckDBPyConnection, table: str, df: pd.DataFrame, pk_cols: list):
    if df.empty:
        print(f"  [skip] {table}: データなし")
        return
    # テーブルが存在しなければ CREATE、存在すれば DELETE+INSERT（年度指定）
    con.execute(f"CREATE TABLE IF NOT EXISTS {table} AS SELECT * FROM df WHERE 1=0")
    if "年度" in pk_cols and "年度" in df.columns:
        year_val = df["年度"].iloc[0]
        con.execute(f"DELETE FROM {table} WHERE 年度 = {year_val}")
    else:
        con.execute(f"DELETE FROM {table}")
    con.execute(f"INSERT INTO {table} SELECT * FROM df")
    print(f"  [ok] {table}: {len(df):,} 行")


# ── メイン ────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default="dpc_data", help="DPCファイルフォルダ")
    parser.add_argument("--match", default="dpc_matching_full.csv", help="突合CSVパス")
    parser.add_argument("--db", default="data/byosho.duckdb", help="DuckDBパス")
    parser.add_argument("--year", type=int, default=2024, help="データ年度（令和6年度=2024）")
    args = parser.parse_args()

    if not os.path.isdir(args.dir):
        print(f"エラー: フォルダが見つかりません: {args.dir}")
        sys.exit(1)
    if not os.path.exists(args.db):
        print(f"エラー: DuckDBが見つかりません: {args.db}")
        sys.exit(1)

    xlsx_files = glob.glob(os.path.join(args.dir, "*.xlsx"))
    print(f"{len(xlsx_files)} 件のExcelファイルを検出")

    # ファイルを種別に分類
    categorized = {
        "gaiyou": [],
        "procedure_stats": [],
        "mdc_cases": [],
        "mdc_ratio": [],
        "readmission": [],
        "surgery_detail": [],
        "type_aggregate": [],
        "unknown": [],
    }
    for f in xlsx_files:
        ft = detect_file_type(f)
        categorized[ft].append(f)
        print(f"  {os.path.basename(f)} → {ft}")

    print()
    con = duckdb.connect(args.db)

    # 1. マッチングテーブル
    if os.path.exists(args.match):
        df_match = load_match(args.match)
        upsert_table(con, "dpc_match", df_match, ["DPC施設名"])
    else:
        print(f"  [warn] 突合CSVが見つかりません: {args.match}")

    # 2. 施設概要表
    dfs = []
    for f in categorized["gaiyou"]:
        try:
            dfs.append(load_gaiyou(f, args.year))
        except Exception as e:
            print(f"  [error] {os.path.basename(f)}: {e}")
    if dfs:
        upsert_table(con, "dpc_hospitals", pd.concat(dfs, ignore_index=True), ["年度", "告示番号"])

    # 3. 手術・化学療法等
    dfs = []
    for f in categorized["procedure_stats"]:
        try:
            dfs.append(load_procedure_stats(f, args.year))
        except Exception as e:
            print(f"  [error] {os.path.basename(f)}: {e}")
    if dfs:
        upsert_table(con, "dpc_procedure_stats", pd.concat(dfs, ignore_index=True), ["年度", "告示番号"])

    # 4. MDC別件数
    dfs = []
    for f in categorized["mdc_cases"]:
        try:
            dfs.append(load_mdc_cases(f, args.year))
        except Exception as e:
            print(f"  [error] {os.path.basename(f)}: {e}")
    if dfs:
        upsert_table(con, "dpc_mdc_cases", pd.concat(dfs, ignore_index=True), ["年度", "告示番号", "手術有無"])

    # 5. MDC比率
    dfs = []
    for f in categorized["mdc_ratio"]:
        try:
            dfs.append(load_mdc_ratio(f, args.year))
        except Exception as e:
            print(f"  [error] {os.path.basename(f)}: {e}")
    if dfs:
        upsert_table(con, "dpc_mdc_ratio", pd.concat(dfs, ignore_index=True), ["年度", "告示番号"])

    # 6. 再入院再転棟
    dfs = []
    for f in categorized["readmission"]:
        try:
            dfs.append(load_readmission(f, args.year))
        except Exception as e:
            print(f"  [error] {os.path.basename(f)}: {e}")
    if dfs:
        upsert_table(con, "dpc_readmission", pd.concat(dfs, ignore_index=True), ["年度", "告示番号"])

    # 7. 疾患別手術別集計（施設別）
    dfs = []
    for f in categorized["surgery_detail"]:
        try:
            df_sd = load_surgery_detail(f, args.year)
            if not df_sd.empty:
                dfs.append(df_sd)
        except Exception as e:
            print(f"  [error] {os.path.basename(f)}: {e}")
    if dfs:
        upsert_table(con, "dpc_surgery_detail", pd.concat(dfs, ignore_index=True), ["年度", "告示番号", "MDC", "dpc6"])

    if categorized["type_aggregate"]:
        print(f"  [skip] 施設類型別集計 {len(categorized['type_aggregate'])} 件（施設別でないためスキップ）")
    if categorized["unknown"]:
        print(f"  [warn] 未分類ファイル {len(categorized['unknown'])} 件: {[os.path.basename(f) for f in categorized['unknown']]}")

    con.close()
    print("\n完了。")
    print("次のステップ: parquetをエクスポートしてください")
    print("  py -c \"import duckdb, pandas as pd; con=duckdb.connect('data/byosho.duckdb'); [con.execute(f'SELECT * FROM {t}').fetchdf().to_parquet(f'{t}.parquet', index=False) for t in ['dpc_hospitals','dpc_procedure_stats','dpc_mdc_cases','dpc_mdc_ratio','dpc_readmission','dpc_surgery_detail','dpc_match']]; con.close(); print('done')\"")


if __name__ == "__main__":
    main()
