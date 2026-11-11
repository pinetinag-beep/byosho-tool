"""
診療報酬 施設基準届出情報 Excel を読み込んで
shisetsu_kijun_cache.parquet を生成する。

各地方厚生局からダウンロードした医科（医療機関）の xlsx を指定して実行する。

使い方:
  py build_shisetsu_kijun.py ファイル1.xlsx ファイル2.xlsx ...
  py build_shisetsu_kijun.py --dir shisetsu_ika_r0805/
  py build_shisetsu_kijun.py --kubun-only     # 今のparquetに shisetsu_kubun_raw/ の区分だけを足す

区分の補完（shisetsu_kubun_raw/）:
  関東信越厚生局は、全件の名簿（24列）には区分を載せていないが、入院基本料等の届出だけを
  集めた別の名簿（19列、区分・病棟数・病床数の備考あり）を公表している。このフォルダに置いた
  名簿から、受理番号まで同じ届出に限って区分などの内訳を埋める（全件の作り直し時にも自動で行う）。

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


def _wareki_date(text: str) -> str:
    """'令和X年Y月Z日' → 'XXXX-YY-ZZ'。日が無ければ月までで返す（同じ県の新旧比較用）。"""
    m = re.search(r"令和\s*(\d+)\s*年\s*(\d+)\s*月\s*(?:(\d+)\s*日)?", str(text))
    if not m:
        return ""
    s = f"{2018 + int(m.group(1))}-{int(m.group(2)):02d}"
    return s + (f"-{int(m.group(3)):02d}" if m.group(3) else "")


def _keep_latest_per_pref(parsed: list[tuple[str, pd.DataFrame, str]]) -> list[pd.DataFrame]:
    """
    同じ都道府県のデータを持つファイルが複数あれば、データ時点が最も新しいファイルだけを使う。
    ダウンロードしたファイルを名前を変えずにフォルダへ入れても、古いファイルと二重に
    数えないため。parsed は (ファイル名, df, 時点) のリスト。時点が同じならファイル名順で後のもの。
    1ファイルに複数県が入っている場合（東北6県）も県ごとに判定する。
    """
    best: dict[str, tuple[str, str]] = {}  # 都道府県コード → (時点, ファイル名)
    for name, df, snap in parsed:
        for code in df["都道府県コード"].unique():
            if code not in best or (snap, name) > best[code]:
                best[code] = (snap, name)
    out = []
    for name, df, snap in parsed:
        win = df["都道府県コード"].map(lambda c: best[c][1] == name)
        lost = df.loc[~win, "都道府県名"].unique()
        if len(lost):
            print(f"  不使用（同じ県のより新しいファイルを優先）: {name}（{'・'.join(lost)}、時点{snap or '不明'}）")
        if win.any():
            out.append(df[win])
    return out


_DETAIL_COLS = ["病棟種別", "病床区分", "病棟数", "病床数", "区分"]


def _carry_over_details(out: pd.DataFrame, prev_path: Path) -> pd.DataFrame:
    """
    関東信越・近畿などは全件Excelの備考（区分・病棟数等）が空だが、差分PDF（apply_shisetsu_diff.py）
    からは区分が取れている。全件で作り直すとそれが消えるため、受理番号まで同じ届出に限り、
    新データで空の内訳を前回のparquetから引き継ぐ（受理番号が変われば別の届出なので引き継がない）。
    """
    key = ["都道府県名", "医療機関番号", "受理記号", "受理番号"]
    cols = [c for c in _DETAIL_COLS if c in out.columns]
    prev = pd.read_parquet(prev_path, columns=key + cols).astype(str)
    prev = prev.replace({"nan": "", "None": ""}).drop_duplicates(key)
    m = out[key].astype(str).merge(prev, on=key, how="left")
    total = 0
    for c in cols:
        cur = out[c].astype(str).replace({"nan": "", "None": ""})
        fill = cur.eq("") & m[c].fillna("").ne("")
        total += int(fill.sum())
        out[c] = cur.where(~fill, m[c])
    print(f"前回データから内訳を引き継ぎ: {total:,}項目（受理番号が同じ届出のみ）")
    return out


KUBUN_DIR = Path(__file__).parent / "shisetsu_kubun_raw"


def _parse_kubun_file(path: Path) -> tuple[pd.DataFrame, str]:
    """区分補完用の名簿（入院基本料等の届出だけの19列形式）を読み、
    (届出ごとの 都道府県名・医療機関番号・受理記号・受理番号＋内訳, 時点) を返す。
    列は位置ではなく見出しの名前で探す（全件の24列形式とは並びが違うため）。"""
    out = []
    snap = ""
    for sname in pd.ExcelFile(path).sheet_names:
        raw = pd.read_excel(path, sheet_name=sname, header=None, dtype=str).fillna("")
        hdr = next((i for i in range(min(10, len(raw)))
                    if {"受理届出名称", "受理番号", "備考（見出し）"} <= set(raw.iloc[i].str.strip())), None)
        if hdr is None:
            continue
        snap = snap or _wareki_date(" ".join(raw.iloc[:hdr].astype(str).values.ravel()))
        d = raw.iloc[hdr + 1:].copy()
        d.columns = [str(c).strip() for c in raw.iloc[hdr]]
        d = d[d["医療機関番号"].str.strip().ne("")]
        d["医療機関番号"] = d["医療機関番号"].str.strip().str.zfill(7)
        # 届出の始まりは見出しもデータも空の行。データだけの行（「早期栄養介入管理加算」など
        # 届け出ている加算の名前）もあるので、見出しが空なだけでは始まりとみなさない
        is_head = d["備考（見出し）"].str.strip().eq("") & d["備考（データ）"].str.strip().eq("")
        d["_g"] = is_head.cumsum()
        heads = d[is_head].set_index("_g")
        det: dict[int, dict[str, str]] = {}
        other: dict[int, list[str]] = {}
        for gid, label, value in d.loc[~is_head, ["_g", "備考（見出し）", "備考（データ）"]].itertuples(index=False):
            label = _normalize_label(label).rstrip(":：")
            value = value.strip()
            key = _KNOWN_DETAIL_LABELS.get(label)
            if key and value and key not in det.get(gid, {}):
                det.setdefault(gid, {})[key] = value
            elif value:
                other.setdefault(gid, []).append(f"{label}:{value}" if label else value)
        f = heads[["都道府県名", "医療機関番号", "受理届出名称", "受理記号", "受理番号"]].copy()
        for c in _DETAIL_COLS:
            f[c] = [det.get(g, {}).get(c, "") for g in f.index]
        f["内訳その他"] = ["；".join(other.get(g, [])) for g in f.index]
        out.append(f.reset_index(drop=True))
    return (pd.concat(out, ignore_index=True) if out else pd.DataFrame()), snap


def _apply_kubun_supplement(out: pd.DataFrame, folder: Path = KUBUN_DIR) -> pd.DataFrame:
    """shisetsu_kubun_raw/ の名簿から、受理番号まで同じ届出の内訳（区分・病棟数・加算など）を埋める。
    名簿は入院基本料等（区分あり）と特定入院料（治療室の病床数・加算など）の2種類があり、どちらも同じ形式。
    同じ県のファイルが複数あれば時点の新しい方だけを使う。名簿側に値がある項目は名簿の値にする
    （差分PDFから引き継いだ値より新しい公表のため）。"""
    files = sorted(folder.glob("*.xlsx")) if folder.exists() else []
    if not files:
        return out
    parsed = []
    for f in files:
        df, snap = _parse_kubun_file(f)
        if df.empty:
            print(f"  区分補完: {f.name} … スキップ（入院基本料等の名簿の形式ではない）")
            continue
        # 同じ県でも「入院基本料等」と「特定入院料」の名簿は別物なので、県×種類ごとに新しい方を使う
        # （_keep_latest_per_pref は都道府県コード列で判定する）
        kind = "入院基本料等" if df["受理届出名称"].str.endswith("入院基本料").any() else "特定入院料"
        df["都道府県コード"] = df["都道府県名"] + "／" + kind
        parsed.append((f.name, df.drop(columns="受理届出名称"), snap))
    key = ["都道府県名", "医療機関番号", "受理記号", "受理番号"]
    cols = _DETAIL_COLS + ["内訳その他"]
    for c in cols:
        out[c] = out[c].astype(str).replace({"nan": "", "None": ""})
    for sup in _keep_latest_per_pref(parsed):
        kind = sup["都道府県コード"].iloc[0].split("／")[1]
        sup = sup.drop(columns="都道府県コード").drop_duplicates(key)
        m = out[key].astype(str).merge(sup, on=key, how="left", indicator=True)
        hit = m["_merge"].eq("both")
        filled = 0
        for c in cols:
            v = m[c].fillna("")
            use = hit & v.ne("")
            filled += int((use & out[c].eq("")).sum())
            out[c] = out[c].where(~use, v)
        for pref, g in sup.groupby("都道府県名"):
            n_hit = int((hit & out["都道府県名"].astype(str).eq(pref)).sum())
            n_kubun = int((hit & out["都道府県名"].astype(str).eq(pref) & out["区分"].ne("")).sum())
            print(f"  区分補完: {pref}（{kind}）名簿{len(g):,}件のうち全件データと一致{n_hit:,}件（区分あり{n_kubun:,}件）")
        print(f"  区分補完: 空欄だった内訳を{filled:,}項目埋めた")
    return out


_CATEGORY_COLS = ["都道府県コード", "都道府県名", "受理届出名称", "受理記号",
                  "施設種別", "病棟種別", "病床区分", "区分", "年月"]


def _save(out: pd.DataFrame, out_path: Path) -> None:
    # 繰り返し文字列をcategory化してメモリを削減（91万行・約356MB→220MB）。
    # Streamlit Cloudのメモリ制限対策。
    for _c in _CATEGORY_COLS:
        if _c in out.columns:
            out[_c] = out[_c].astype(str).astype("category")
    out.to_parquet(out_path, index=False)


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

    # 「市町村名」列はどの厚生局のフォーマットでも常に空のため保持しない
    # （住所の先頭に市区町村名が含まれているため、住所列だけで十分）。
    keep = [
        "都道府県コード", "都道府県名", "医療機関番号", "医療機関名称",
        "医療機関名_正規化", "住所", "受理届出名称", "受理記号", "受理番号", "算定開始年月日",
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


def parse_file(path: str) -> tuple[pd.DataFrame, str, str]:
    """
    Excel 1ファイルをパースして (df, 年月文字列, 時点の日付) を返す。
    複数シートがある場合はすべて結合する（東北局形式など）。
    失敗時は (空DataFrame, "", "") を返す。

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
        return pd.DataFrame(), "", ""

    sheet_dfs: list[pd.DataFrame] = []
    year_month = ""
    snap = ""
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
                snap = _wareki_date(raw.iloc[1, 0])

    if not sheet_dfs:
        return pd.DataFrame(), year_month, snap
    return pd.concat(sheet_dfs, ignore_index=True), year_month, snap


def main() -> None:
    args = sys.argv[1:]
    out_path = Path(__file__).parent / "shisetsu_kijun_cache.parquet"

    if "--kubun-only" in args:
        out = pd.read_parquet(out_path)
        n = len(out)
        out = _apply_kubun_supplement(out)
        assert len(out) == n
        _save(out, out_path)
        print(f"完了: {len(out):,}行 → {out_path}")
        return

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

    parsed: list[tuple[str, pd.DataFrame, str]] = []
    for f in files:
        print(f"読み込み: {Path(f).name} ... ", end="", flush=True)
        df, ym, snap = parse_file(f)
        if df.empty:
            print("スキップ（データなし）")
            continue
        print(f"{len(df):,}行 ({snap or ym})")
        parsed.append((Path(f).name, df, snap))
    dfs = _keep_latest_per_pref(parsed)

    if not dfs:
        print("エラー: 有効なデータがありませんでした")
        sys.exit(1)

    out = pd.concat(dfs, ignore_index=True)
    if out_path.exists():
        out = _carry_over_details(out, out_path)
    out = _apply_kubun_supplement(out)
    out["施設種別"] = _classify_facility_types(out)
    _save(out, out_path)

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
