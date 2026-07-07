"""
診療報酬 施設基準届出情報 Excel を読み込んで
shisetsu_kijun_cache.parquet を生成する。

各地方厚生局からダウンロードした医科（医療機関）の xlsx を指定して実行する。

使い方:
  py build_shisetsu_kijun.py ファイル1.xlsx ファイル2.xlsx ...
  py build_shisetsu_kijun.py --dir shisetsu_ika_r0805/

出力: shisetsu_kijun_cache.parquet（実行フォルダに保存）
"""
import sys
import re
import glob
import unicodedata
from pathlib import Path

import pandas as pd

# 法人格プレフィックス（長い順に並べること）
_LEGAL_PREFIXES = [
    "独立行政法人国立病院機構",
    "国家公務員共済組合連合会",
    "地方独立行政法人",
    "社会医療法人財団",
    "社会医療法人",
    "国立大学法人",
    "公立大学法人",
    "医療法人社団",
    "医療法人財団",
    "公益財団法人",
    "一般財団法人",
    "公益社団法人",
    "一般社団法人",
    "社会福祉法人",
    "特定医療法人",
    "医療法人",
    "学校法人",
    "宗教法人",
]


def _wareki_to_seireki(text: str) -> str:
    """'令和X年Y月' → 'XXXX-YY'。見つからなければ空文字。"""
    m = re.search(r"令和\s*(\d+)\s*年\s*(\d+)\s*月", str(text))
    if m:
        y = 2018 + int(m.group(1))
        mo = int(m.group(2))
        return f"{y}-{mo:02d}"
    return ""


def _normalize(name: str) -> str:
    """法人格プレフィックスを除去し、NFKC・小文字・スペース除去を行う。"""
    if not isinstance(name, str):
        return ""
    name = unicodedata.normalize("NFKC", name)
    name = name.strip()
    for prefix in _LEGAL_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    name = re.sub(r"[\s　]", "", name)
    return name.lower()


# 「備考（見出し）」列に現れるラベルのうち、専用列として保持するもの。
# 表記ゆれ（全角スペース混入・末尾スペース・「届出に係る」プレフィックス等）を
# 正規化したうえでこのキーに一致させる。
_KNOWN_DETAIL_LABELS = {
    "病棟種別": "病棟種別",
    "病床区分": "病床区分",
    "病棟数":   "病棟数",
    "病床数":   "病床数",
    "区分":     "区分",
}


def _normalize_label(label: str) -> str:
    """備考（見出し）のラベルを正規化する（全角/半角スペース除去、前置き除去）。"""
    s = unicodedata.normalize("NFKC", str(label))
    s = re.sub(r"[\s　]", "", s)
    s = re.sub(r"^届出に係る", "", s)
    return s


def _parse_sheet(raw: "pd.DataFrame") -> "tuple[pd.DataFrame, str]":
    """
    1シートのrawデータをパースして (df, 年月) を返す。

    1つの届出は複数行に展開されている:
      1行目: 受理届出名称・受理記号・受理番号・算定開始年月日 を持ち、
             「備考（見出し）」「備考（データ）」は空。
      2行目以降: 同じ届出の内訳（病棟種別・病床区分・病棟数・病床数・区分等）が
             「備考（見出し）」「備考（データ）」のペアとして1行ずつ入る。
    「備考（見出し）」が空の行を届出の開始とみなし、それ以降の行を
    直前の届出の内訳として紐づける。
    """
    if len(raw) < 5:
        return pd.DataFrame(), ""

    year_month = _wareki_to_seireki(raw.iloc[1, 0])

    data = raw.iloc[4:].copy().reset_index(drop=True)
    if data.shape[1] < 24:
        return pd.DataFrame(), year_month

    data = data.iloc[:, :24].copy()
    data.columns = [
        "項番", "都道府県コード", "都道府県名", "区分",
        "医療機関番号", "併設医療機関番号", "医療機関記号番号", "医療機関名称",
        "郵便番号", "住所", "電話番号", "FAX番号", "病床数",
        "受理届出名称", "受理記号", "受理番号", "算定開始年月日", "個別有効開始年月日",
        "備考見出し", "備考データ", "市町村コード", "市町村名", "種別コード", "種別",
    ]

    data = data[data["区分"] == "医科"].copy()

    def _fmt_pref(v):
        if pd.isna(v) or str(v).strip() in ("", "nan", "None"):
            return ""
        try:
            return str(int(float(v))).zfill(2)
        except (ValueError, TypeError):
            return ""

    def _fmt_code(v):
        if pd.isna(v) or str(v).strip() in ("", "nan", "None"):
            return ""
        try:
            return str(int(float(v))).zfill(7)
        except (ValueError, TypeError):
            return ""

    data["都道府県コード"] = data["都道府県コード"].apply(_fmt_pref)
    data["医療機関番号"] = data["医療機関番号"].apply(_fmt_code)

    data = data[data["医療機関番号"].ne("")].reset_index(drop=True)

    # 「備考見出し」が空 = 新しい届出の開始行。累積和でグループIDを振る。
    is_header = data["備考見出し"].isna() | data["備考見出し"].astype(str).str.strip().eq("")
    data["_group_id"] = is_header.cumsum()

    header_rows = data[is_header].copy()
    header_rows = header_rows[
        header_rows["受理届出名称"].notna() & header_rows["受理届出名称"].ne("")
    ]

    detail_rows = data[~is_header]

    # グループごとに 備考見出し→備考データ の辞書を作る
    details_by_group: dict[int, dict[str, str]] = {}
    for gid, grp in detail_rows.groupby("_group_id"):
        d: dict[str, str] = {}
        others: list[str] = []
        for _, r in grp.iterrows():
            label = _normalize_label(r["備考見出し"])
            value = str(r["備考データ"]).strip() if pd.notna(r["備考データ"]) else ""
            if not value:
                continue
            key = _KNOWN_DETAIL_LABELS.get(label)
            if key and key not in d:
                d[key] = value
            else:
                others.append(f"{label}:{value}")
        if others:
            d["内訳その他"] = "；".join(others)
        details_by_group[gid] = d

    for col in ["病棟種別", "病床区分", "病棟数", "病床数", "区分", "内訳その他"]:
        header_rows[col] = header_rows["_group_id"].map(
            lambda gid: details_by_group.get(gid, {}).get(col, "")
        )

    header_rows["医療機関名_正規化"] = header_rows["医療機関名称"].apply(_normalize)
    header_rows["年月"] = year_month

    keep = [
        "都道府県コード", "都道府県名", "医療機関番号", "医療機関名称",
        "医療機関名_正規化", "受理届出名称", "受理記号", "受理番号", "算定開始年月日",
        "病棟種別", "病床区分", "病棟数", "病床数", "区分", "内訳その他", "年月",
    ]
    return header_rows[keep].reset_index(drop=True), year_month


# 病院系の入院基本料（医療法上、病院（20床以上）でなければ届出できない）。
# 有床診療所は「有床診療所入院基本料」等の専用の届出名称を持つため、
# それとの重複が無いことを確認済み（本番データで矛盾ケース0件）。
_HOSPITAL_NYUIN_TYPES = {
    "一般病棟入院基本料", "療養病棟入院基本料", "精神病棟入院基本料",
    "障害者施設等入院基本料", "結核病棟入院基本料",
    "特定機能病院入院基本料", "専門病院入院基本料",
}
_CLINIC_BED_NYUIN_TYPES = {
    "有床診療所入院基本料", "有床診療所療養病床入院基本料",
}


def _classify_facility_types(df: pd.DataFrame) -> "pd.Series":
    """
    医療機関番号ごとに「病院／有床診療所／無床診療所」を判定する。

    入院基本料は入院を算定するための必須の届出（届出なしに入院料は
    算定できない）なので、これらの届出が一切無い医療機関は無床診療所と
    判定できる。届出をしていない無床診療所は元々このデータに載らない
    （届出自体が無い）ため、ここでの「無床診療所」は
    「何らかの施設基準は届出ているが入院基本料は届出ていない」医療機関を指す。
    """
    key = df["都道府県コード"].astype(str) + "_" + df["医療機関番号"].astype(str)
    has_hospital = set(key[df["受理届出名称"].isin(_HOSPITAL_NYUIN_TYPES)])
    has_clinic_bed = set(key[df["受理届出名称"].isin(_CLINIC_BED_NYUIN_TYPES)])

    def _classify(k: str) -> str:
        if k in has_hospital:
            return "病院"
        if k in has_clinic_bed:
            return "有床診療所"
        return "無床診療所"

    return key.map(_classify)


def parse_file(path: str) -> tuple[pd.DataFrame, str]:
    """
    Excel 1ファイルをパースして (df, 年月文字列) を返す。
    複数シートがある場合はすべて結合する（東北局形式など）。
    失敗時は (空DataFrame, "") を返す。

    期待するシート構造:
      行0: 空
      行1: "[令和X年Y月Z日 現在]..." （年月情報を含む）
      行2: 空
      行3: ヘッダー行（項番、都道府県コード、…）
      行4+: データ
    """
    try:
        xl = pd.ExcelFile(path)
    except Exception as e:
        print(f"  読み込みエラー: {e}")
        return pd.DataFrame(), ""

    sheet_dfs: list[pd.DataFrame] = []
    year_month = ""
    for sname in xl.sheet_names:
        try:
            raw = pd.read_excel(path, sheet_name=sname, header=None, dtype=str)
        except Exception:
            continue
        df_s, ym = _parse_sheet(raw)
        if not df_s.empty:
            sheet_dfs.append(df_s)
            if not year_month:
                year_month = ym

    if not sheet_dfs:
        return pd.DataFrame(), year_month
    return pd.concat(sheet_dfs, ignore_index=True), year_month


def main() -> None:
    args = sys.argv[1:]

    if "--dir" in args:
        idx = args.index("--dir")
        d = Path(args[idx + 1])
        files = sorted(str(p) for p in d.glob("*.xlsx"))
    else:
        files = [a for a in args if not a.startswith("--")]

    if not files:
        print("使い方: py build_shisetsu_kijun.py ファイル1.xlsx [ファイル2.xlsx ...]")
        print("        py build_shisetsu_kijun.py --dir フォルダ名/")
        sys.exit(1)

    dfs: list[pd.DataFrame] = []
    for f in files:
        print(f"読み込み: {Path(f).name} ... ", end="", flush=True)
        df, ym = parse_file(f)
        if df.empty:
            print("スキップ（データなし）")
            continue
        print(f"{len(df):,}行 ({ym})")
        dfs.append(df)

    if not dfs:
        print("エラー: 有効なデータがありませんでした")
        sys.exit(1)

    out = pd.concat(dfs, ignore_index=True)
    out["施設種別"] = _classify_facility_types(out)
    out_path = Path(__file__).parent / "shisetsu_kijun_cache.parquet"
    out.to_parquet(out_path, index=False)

    print(f"\n完了: {len(out):,}行 → {out_path}")

    # 都道府県別サマリー
    print("\n=== 都道府県別ユニーク医療機関数 ===")
    summary = (
        out.groupby(["都道府県コード", "都道府県名"])["医療機関番号"]
        .nunique()
        .reset_index()
        .sort_values("都道府県コード")
    )
    summary.columns = ["コード", "都道府県名", "医療機関数"]
    print(summary.to_string(index=False))

    # 施設種別サマリー
    print("\n=== 施設種別 ===")
    fac_summary = out.drop_duplicates(subset=["都道府県コード", "医療機関番号"])["施設種別"].value_counts()
    print(fac_summary.to_string())

    # 届出名称件数 Top20
    print("\n=== 届出名称 件数 Top20 ===")
    top = out["受理届出名称"].value_counts().head(20)
    for name, cnt in top.items():
        print(f"  {cnt:5,}件  {name}")


if __name__ == "__main__":
    main()
