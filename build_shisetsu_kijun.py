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


def parse_file(path: str) -> tuple[pd.DataFrame, str]:
    """
    Excel 1ファイルをパースして (df, 年月文字列) を返す。
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
        raw = pd.read_excel(path, sheet_name=xl.sheet_names[0], header=None, dtype=str)
    except Exception as e:
        print(f"  読み込みエラー: {e}")
        return pd.DataFrame(), ""

    if len(raw) < 5:
        return pd.DataFrame(), ""

    # 年月を取得（行1の最初のセル）
    year_month = _wareki_to_seireki(raw.iloc[1, 0])

    # データ部分（行4以降）
    data = raw.iloc[4:].copy().reset_index(drop=True)

    if data.shape[1] < 15:
        return pd.DataFrame(), year_month

    data = data.iloc[:, :15].copy()
    data.columns = [
        "項番", "都道府県コード", "都道府県名", "区分",
        "医療機関番号", "併設医療機関番号", "医療機関記号番号", "医療機関名称",
        "郵便番号", "住所", "電話番号", "FAX番号", "病床数",
        "受理届出名称", "受理記号",
    ]

    # 医科のみ（歯科・その他を除外）
    data = data[data["区分"] == "医科"].copy()

    # 都道府県コードを2桁文字列にそろえる
    def _fmt_pref(v):
        if pd.isna(v) or str(v).strip() in ("", "nan", "None"):
            return ""
        try:
            return str(int(float(v))).zfill(2)
        except (ValueError, TypeError):
            return ""

    data["都道府県コード"] = data["都道府県コード"].apply(_fmt_pref)

    # 医療機関番号を7桁文字列にそろえる
    def _fmt_code(v):
        if pd.isna(v) or str(v).strip() in ("", "nan", "None"):
            return ""
        try:
            return str(int(float(v))).zfill(7)
        except (ValueError, TypeError):
            return ""

    data["医療機関番号"] = data["医療機関番号"].apply(_fmt_code)

    # 正規化名称
    data["医療機関名_正規化"] = data["医療機関名称"].apply(_normalize)

    # 年月カラム
    data["年月"] = year_month

    # 必須フィールドが空の行を除去
    data = data[
        data["受理届出名称"].notna()
        & data["受理届出名称"].ne("")
        & data["医療機関番号"].ne("")
    ]

    keep = [
        "都道府県コード", "都道府県名", "医療機関番号", "医療機関名称",
        "医療機関名_正規化", "受理届出名称", "受理記号", "年月",
    ]
    return data[keep], year_month


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

    # 届出名称件数 Top20
    print("\n=== 届出名称 件数 Top20 ===")
    top = out["受理届出名称"].value_counts().head(20)
    for name, cnt in top.items():
        print(f"  {cnt:5,}件  {name}")


if __name__ == "__main__":
    main()
