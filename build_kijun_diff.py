"""施設基準届出（医科）の「前回の名簿」と「今回の名簿」を比べ、新規届出・取り下げ（失効・辞退など）・区分の変更を
kijun_diff.parquet に足す（MedilenZ の /changes ページ用）。

    python build_kijun_diff.py 旧.parquet            # 今の shisetsu_kijun_cache.parquet と比べる
    python build_kijun_diff.py 旧.parquet 新.parquet

毎月の更新（fetch_kouseikyoku.py → build_shisetsu_kijun.py）の前に退避した shisetsu_kijun_cache.parquet を「旧」に渡す。

決まりごと:
- 都道府県ごとに、名簿の時点（年月）が変わった県だけを比べる（同じ時点の名簿どうしは比べない。厚生局の出し直しなどで
  わずかに違うことがあるが、実際の届出の動きとは限らないため）。
- 届出は「都道府県・医療機関番号・受理届出名称」で同じものとみなす。今回だけにあれば新規、前回だけにあれば取り下げ
  （名簿から無くなった＝辞退・失効・廃止など。理由は名簿からは分からない）。
- 同じ医療機関で、取り下げた届出と新規の届出が「数字・ローマ数字を除くと同じ名前」なら区分の変更（例：
  電子的診療情報連携体制整備加算３→２）として1行にまとめる。
- 全国で名簿から無くなった届出名（廃止された届出）や、全国で初めて出てきた届出名（新設）は、改定による名前の入れ替わり
  なので数えない。
- 医療機関ごと名簿に現れた／無くなった場合は「医療機関の動き」に開設／廃止等と書く（届出の新規・取り下げも残す）。
- 同じ都道府県・同じ新しい時点の行が既にあれば入れ替える（やり直しても二重にならない）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
NEW_DEFAULT = ROOT / "shisetsu_kijun_cache.parquet"
OUT = ROOT / "kijun_diff.parquet"
COLS = ["都道府県コード", "都道府県名", "医療機関番号", "医療機関名称", "住所", "施設種別", "受理届出名称", "受理記号",
        "受理番号", "算定開始年月日", "年月"]
KEY = ["都道府県名", "医療機関番号", "受理届出名称"]
OUT_COLS = ["新年月", "旧年月", "都道府県コード", "都道府県名", "医療機関番号", "医療機関名称", "住所", "施設種別",
            "種類", "受理届出名称", "前の届出名称", "受理記号", "受理番号", "算定開始年月日", "医療機関の動き"]

_NUM = re.compile(r"[0-9０-９ⅠⅡⅢⅣⅤⅥＩ]+")


def _base(name: str) -> str:
    """区分の数字を除いた名前（区分の変更を見分けるため）。"""
    return re.sub(r"[（(]\s*[)）]", "", _NUM.sub("", name))


def _read(path: Path) -> pd.DataFrame:
    d = pd.read_parquet(path, columns=COLS)
    for c in COLS:
        d[c] = d[c].astype(str).replace({"nan": "", "None": ""})
    return d


def diff(old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    om = old.groupby("都道府県名")["年月"].max()
    nm = new.groupby("都道府県名")["年月"].max()
    prefs = [p for p in nm.index if p in om.index and nm[p] > om[p]]
    gone_names = set(old["受理届出名称"]) - set(new["受理届出名称"])   # 廃止された届出
    born_names = set(new["受理届出名称"]) - set(old["受理届出名称"])   # 新設された届出
    o = old[old["都道府県名"].isin(prefs)].drop_duplicates(KEY)
    n = new[new["都道府県名"].isin(prefs)].drop_duplicates(KEY)
    m = o.merge(n[KEY], on=KEY, how="outer", indicator=True)
    added = n.merge(m[m["_merge"] == "right_only"][KEY], on=KEY)
    dropped = o.merge(m[m["_merge"] == "left_only"][KEY], on=KEY)
    added = added[~added["受理届出名称"].isin(born_names)].copy()
    dropped = dropped[~dropped["受理届出名称"].isin(gone_names)].copy()
    added["種類"], dropped["種類"] = "新規", "取り下げ"
    added["前の届出名称"] = dropped["前の届出名称"] = ""

    # 区分の変更：同じ医療機関で、数字を除くと同じ名前の取り下げと新規を組にする
    added["_b"], dropped["_b"] = added["受理届出名称"].map(_base), dropped["受理届出名称"].map(_base)
    fk = ["都道府県名", "医療機関番号", "_b"]
    a1 = added.drop_duplicates(fk, keep=False)
    d1 = dropped.drop_duplicates(fk, keep=False)
    pair = a1.merge(d1[fk + ["受理届出名称"]].rename(columns={"受理届出名称": "_prev"}), on=fk)
    pair = pair[pair["受理届出名称"] != pair["_prev"]]
    if len(pair):
        pk = set(map(tuple, pair[fk].values))
        added = added[~added[fk].apply(tuple, axis=1).isin(pk)]
        dropped = dropped[~dropped[fk].apply(tuple, axis=1).isin(pk)]
        pair = pair.assign(種類="変更", 前の届出名称=pair["_prev"])
    rows = pd.concat([added, dropped, pair], ignore_index=True)

    # 医療機関ごと名簿に現れた／無くなった
    fo = set(zip(o["都道府県名"], o["医療機関番号"]))
    fn = set(zip(n["都道府県名"], n["医療機関番号"]))
    fac = list(zip(rows["都道府県名"], rows["医療機関番号"]))
    born, closed = fn - fo, fo - fn
    rows["医療機関の動き"] = ["名簿に追加（開設など）" if f in born else "名簿から削除（廃止・休止など）" if f in closed else ""
                         for f in fac]
    rows["新年月"] = rows["都道府県名"].map(nm)
    rows["旧年月"] = rows["都道府県名"].map(om)
    return rows[OUT_COLS].sort_values(["新年月", "都道府県コード", "医療機関番号", "種類", "受理届出名称"]).reset_index(drop=True)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    old_p = Path(sys.argv[1])
    new_p = Path(sys.argv[2]) if len(sys.argv) > 2 else NEW_DEFAULT
    rows = diff(_read(old_p), _read(new_p))
    if OUT.exists():
        prev = pd.read_parquet(OUT)
        done = set(zip(rows["都道府県名"], rows["新年月"]))
        prev = prev[~pd.Series(list(zip(prev["都道府県名"], prev["新年月"])), index=prev.index).isin(done)]
        rows = pd.concat([prev, rows], ignore_index=True)
    rows.to_parquet(OUT, index=False, compression="zstd")
    cur = rows[rows["新年月"] == rows["新年月"].max()]
    print(f"比べた都道府県（今回）: {cur['都道府県名'].nunique()}  "
          + "・".join(f"{k}{v:,}" for k, v in cur["種類"].value_counts().items()))
    print(f"医療機関の動き: " + "・".join(f"{k} {v:,}行" for k, v in cur["医療機関の動き"].value_counts().items() if k))
    print(f"→ {OUT.name}（全{len(rows):,}行・{rows['新年月'].nunique()}か月分）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
