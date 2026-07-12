"""
「保険医療機関の届出関係失効一覧表」PDF（失効・取消の差分レポート）をパースする。

新規PDF（parse_shisetsu_shinki_pdf.py）と違い、正式名称は載っておらず
受理記号（略号）と失効事由・失効年月日のみが載っている。ただし失効データは
shisetsu_kijun_cache.parquet からの「行削除」にしか使わないため、正式名称への
逆引きは不要（都道府県名＋医療機関番号＋受理記号の組で一致する既存行を消す）。

列レイアウト（x0基準）:
  項番: x0 ~30-51
  医療機関番号: x0 ~64（レコード開始アンカー）
  医療機関名称: x0 ~143-233
  医療機関所在地・開設者/管理者氏名: x0 ~233-490（削除には使わないので読み捨て）
  失効内容（受理記号）: x0 ~493-586
  失効事由: x0 ~587-635
  失効年月日: x0 >= 636

1医療機関が複数の届出を同時に失効することがあり、その場合「失効内容」列に
複数行（＝複数の受理記号）が並ぶ。
"""
import bisect
from pathlib import Path

import pdfplumber
import pandas as pd

from parse_shisetsu_shinki_pdf import (
    PREF_NAME_TO_CODE, FILENAME_PREF_HINTS, _group_by_doctop_join, _find_anchors,
)

# 項番（x0~30-52）と医療機関番号（x0~64+）が同じ x0<100 域にあるため、
# 医療機関番号アンカーの検出には項番を含まない下限（58）を使う。
COL_KIKAN_BANGO = (58, 100)
COL_MEISHO = (110, 233)
COL_SHIKKOU_NAIYOU = (480, 587)
COL_SHIKKOU_JIYUU = (587, 636)
COL_SHIKKOU_YMD_X0 = 636

HEADER_TOP_CUTOFF = 96


def _guess_pref_from_filename(path: str) -> str:
    stem = Path(path).stem
    for key, pref in FILENAME_PREF_HINTS.items():
        if stem.startswith(key):
            return pref
    return ""


def parse_pdf(path: str, pref_name_hint: str = "") -> pd.DataFrame:
    pref_name = pref_name_hint or _guess_pref_from_filename(path)
    pref_code = PREF_NAME_TO_CODE.get(pref_name, "")

    rows = []
    with pdfplumber.open(path) as pdf:
        all_words = []
        for page in pdf.pages:
            all_words.extend(w for w in page.extract_words() if w["top"] >= HEADER_TOP_CUTOFF)
        doc_bottom = max((w["doctop"] for w in all_words), default=0.0) + 20.0

        all_words.sort(key=lambda w: w["doctop"])
        sorted_doctops = [w["doctop"] for w in all_words]

        anchor_hits = _find_anchors(all_words, *COL_KIKAN_BANGO)
        anchors = sorted({dtop for dtop, _ in anchor_hits})
        if not anchors:
            return pd.DataFrame()
        anchor_code_by_dtop = dict(anchor_hits)

        for i, dtop in enumerate(anchors):
            y0 = dtop - 1
            y1 = anchors[i + 1] - 1 if i + 1 < len(anchors) else doc_bottom + 1
            lo = bisect.bisect_left(sorted_doctops, y0)
            hi = bisect.bisect_left(sorted_doctops, y1)
            rec_words = all_words[lo:hi]

            iryo_bango = anchor_code_by_dtop[dtop]

            mn_x0, mn_x1 = COL_MEISHO
            mn_words = [w for w in rec_words if mn_x0 <= w["x0"] < mn_x1]
            meisho = "".join(t for _, t in _group_by_doctop_join(mn_words))

            sn_x0, sn_x1 = COL_SHIKKOU_NAIYOU
            sn_words = [w for w in rec_words if sn_x0 <= w["x0"] < sn_x1]
            sn_lines = _group_by_doctop_join(sn_words)

            sj_x0, sj_x1 = COL_SHIKKOU_JIYUU
            sj_words = [w for w in rec_words if sj_x0 <= w["x0"] < sj_x1]
            sj_lines = dict(_group_by_doctop_join(sj_words))

            ym_words = [w for w in rec_words if w["x0"] >= COL_SHIKKOU_YMD_X0]
            ym_lines = dict(_group_by_doctop_join(ym_words, sep=" "))

            for dtop_, kigou in sn_lines:
                kigou = kigou.strip()
                if not kigou:
                    continue
                rows.append({
                    "都道府県コード": pref_code,
                    "都道府県名": pref_name,
                    "医療機関番号": iryo_bango,
                    "医療機関名称": meisho,
                    "受理記号": kigou,
                    "失効事由": sj_lines.get(dtop_, ""),
                    "失効年月日": ym_lines.get(dtop_, ""),
                })
    return pd.DataFrame(rows)


if __name__ == "__main__":
    import sys
    path = sys.argv[1]
    pref_hint = sys.argv[2] if len(sys.argv) > 2 else ""
    df = parse_pdf(path, pref_hint)
    print(f"抽出行数: {len(df)}")
    with pd.option_context("display.max_columns", None, "display.width", 200):
        print(df.head(30))
