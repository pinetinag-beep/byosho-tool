"""薬局の施設基準の届出（各地方厚生局「届出受理医療機関名簿（薬局）」）を yakkyoku_cache.parquet にまとめる。

    python build_yakkyoku.py          # shisetsu_raw_yakkyoku/ の xlsx をすべて読む

名簿の集め方は fetch_kouseikyoku.py（--apply で shisetsu_raw_yakkyoku/ に入る）。
- 基本は全件の名簿（医科と同じ24列で、「区分」の列が「薬局」）。読み方は build_shisetsu_kijun.parse_file と同じ。
- 中国厚生局（鳥取・島根・岡山・広島・山口）は薬局の全件の名簿を出しておらず、届出項目別の名簿（19列）の
  「薬局」に全部の届出が載っているので、全件の名簿が無い県だけそれを使う（2026年11月確認）。
- 同じ県の名簿が複数あれば、時点の新しいものを使う。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

import build_shisetsu_kijun as bk

ROOT = Path(__file__).resolve().parent
RAW = ROOT / "shisetsu_raw_yakkyoku"
OUT = ROOT / "yakkyoku_cache.parquet"
COLS = ["都道府県コード", "都道府県名", "医療機関番号", "医療機関名称", "住所",
        "受理届出名称", "受理記号", "受理番号", "算定開始年月日", "内訳その他", "時点"]


def parse_items(path: Path) -> tuple[pd.DataFrame, str]:
    """届出項目別の名簿（19列）の薬局版を読む。列は見出しの名前で探す。薬局の名簿でなければ空を返す。"""
    out, snap, kind = [], "", ""
    for sname in pd.ExcelFile(path).sheet_names:
        raw = pd.read_excel(path, sheet_name=sname, header=None, dtype=str).fillna("")
        hdr = next((i for i in range(min(10, len(raw)))
                    if {"受理届出名称", "受理番号", "備考（見出し）", "医療機関名称"} <= set(raw.iloc[i].str.strip())), None)
        if hdr is None:
            continue
        title = " ".join(raw.iloc[:hdr].astype(str).values.ravel())
        snap = snap or bk._wareki_date(title)
        kind = kind or next((k for k in ("医科", "歯科", "薬局") if k in title), "")
        d = raw.iloc[hdr + 1:].copy()
        d.columns = [str(c).strip() for c in raw.iloc[hdr]]
        d = d[d["医療機関番号"].str.strip().ne("")]
        # 届出の始まりは見出しもデータも空の行（build_shisetsu_kijun._parse_kubun_file と同じ）
        is_head = d["備考（見出し）"].str.strip().eq("") & d["備考（データ）"].str.strip().eq("")
        d = d[is_head & d["受理届出名称"].str.strip().ne("")]
        f = pd.DataFrame({
            "都道府県コード": d["都道府県コード"].str.strip().str.zfill(2),
            "都道府県名": d["都道府県名"].str.strip(),
            "医療機関番号": d["医療機関番号"].str.strip().str.zfill(7),
            "医療機関名称": d["医療機関名称"].str.strip(),
            "住所": d.get("医療機関所在地（住所）", pd.Series("", index=d.index)).str.strip(),
            "受理届出名称": d["受理届出名称"].str.strip(),
            "受理記号": d["受理記号"].str.strip(),
            "受理番号": d["受理番号"].str.strip(),
            "算定開始年月日": d["算定開始年月日"].str.strip(),
            "内訳その他": "",
        })
        out.append(f)
    if kind != "薬局" or not out:
        return pd.DataFrame(columns=COLS), snap
    df = pd.concat(out, ignore_index=True)
    df["時点"] = snap
    return df, snap


def read(path: Path) -> tuple[pd.DataFrame, str, str]:
    """1ファイルを読み (df, 時点, 種類) を返す。種類は「全件」「項目別」。読めなければ空。"""
    try:
        df, _ym, snap = bk.parse_file(str(path), kind="薬局")
    except Exception:  # noqa: BLE001
        df, snap = pd.DataFrame(), ""
    if not df.empty:
        df = df.copy()
        df["時点"] = snap
        for c in COLS:
            if c not in df.columns:
                df[c] = ""
        return df[COLS].fillna(""), snap, "全件"
    try:
        df, snap = parse_items(path)
    except Exception:  # noqa: BLE001
        df, snap = pd.DataFrame(columns=COLS), ""
    return (df[COLS], snap, "項目別") if not df.empty else (df, "", "")


def build(src: Path = RAW, out: Path = OUT) -> pd.DataFrame:
    parsed: dict[str, list[tuple[str, pd.DataFrame, str]]] = {"全件": [], "項目別": []}
    for f in sorted(src.glob("*.xlsx")):
        df, snap, typ = read(f)
        print(f"読み込み: {f.name} … " + (f"{typ} {len(df):,}行 ({snap})" if typ else "スキップ（薬局の名簿ではない）"))
        if typ:
            parsed[typ].append((f.name, df, snap))
    full = bk._keep_latest_per_pref(parsed["全件"])
    covered = {p for d in full for p in d["都道府県名"].unique()}
    items = [d[~d["都道府県名"].isin(covered)] for d in bk._keep_latest_per_pref(parsed["項目別"])]
    items = [d for d in items if not d.empty]
    if items:
        print("全件の名簿が無く届出項目別の名簿を使った県:", "・".join(sorted({p for d in items for p in d["都道府県名"].unique()})))
    dfs = full + items
    if not dfs:
        raise SystemExit("エラー: 薬局の名簿がありません")
    df = pd.concat(dfs, ignore_index=True).drop_duplicates()
    df.to_parquet(out, index=False, compression="zstd")
    n_fac = df.groupby(["都道府県コード", "医療機関番号"]).ngroups
    print(f"完了: {len(df):,}行・薬局{n_fac:,}件・{df['都道府県名'].nunique()}都道府県 → {out.name}")
    return df


def main() -> int:
    build()
    return 0


if __name__ == "__main__":
    sys.exit(main())
