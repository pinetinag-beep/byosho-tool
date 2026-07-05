"""
「届出受理医療機関名簿」全件スナップショットPDF（中部地方等）をパースする。
新規差分PDF（parse_shinki_pdf.py）とは列レイアウトが異なる:
  医療機関番号: x0 < 100 の列（レコード開始アンカー、"101,1002,3" 形式）
  医療機関名称: x0 ~110-215
  受理番号列（届出確定行）: x0 ~435-450 「（受理記号）第N号」
  備考列（内訳詳細、区分等）: x0 ~670-685 「ラベル:値」

このPDFには受理届出名称（正式名称）が無く、受理記号（略号）のみが載っている。
既存データ（東北・九州のExcel由来）から 受理記号→受理届出名称 の逆引き辞書を
構築し、正式名称を復元する。
"""
import re
import sys
import bisect
from pathlib import Path
import pdfplumber
import pandas as pd

from build_shisetsu_kijun import _normalize

# 項番が医療機関番号の直前にスペースなしで連結される（例:"13901,1802,6"=項番139+
# 医療機関番号01,1802,6）ため、行頭からではなく末尾から医療機関番号部分を検索する。
KIKAN_BANGO_ANCHOR_RE = re.compile(r'(\d{2})[,・-](\d{3,5})[,・-](\d{1,2})$')
# 病床数列の値（数字）が確定行のテキストに結合されることがある
# （例:"378（医療ＤＸ）第494号"）ため、^アンカーは付けずsearchで検出する。
CONFIRM_RE = re.compile(r'（(?P<label>.+?)）第(?P<num>\d+)号')
DETAIL_KV_RE = re.compile(r'^(?P<label>[^:：]+)[:：](?P<value>.*)$')

PREF_NAME_TO_CODE = {
    "北海道": "01", "青森県": "02", "岩手県": "03", "宮城県": "04", "秋田県": "05",
    "山形県": "06", "福島県": "07", "茨城県": "08", "栃木県": "09", "群馬県": "10",
    "埼玉県": "11", "千葉県": "12", "東京都": "13", "神奈川県": "14", "新潟県": "15",
    "富山県": "16", "石川県": "17", "福井県": "18", "山梨県": "19", "長野県": "20",
    "岐阜県": "21", "静岡県": "22", "愛知県": "23", "三重県": "24", "滋賀県": "25",
    "京都府": "26", "大阪府": "27", "兵庫県": "28", "奈良県": "29", "和歌山県": "30",
    "鳥取県": "31", "島根県": "32", "岡山県": "33", "広島県": "34", "山口県": "35",
    "徳島県": "36", "香川県": "37", "愛媛県": "38", "高知県": "39", "福岡県": "40",
    "佐賀県": "41", "長崎県": "42", "熊本県": "43", "大分県": "44", "宮崎県": "45",
    "鹿児島県": "46", "沖縄県": "47",
}
FILENAME_PREF_HINTS = {
    "三重": "三重県", "富山": "富山県", "岐阜": "岐阜県",
    "愛知": "愛知県", "石川": "石川県", "静岡": "静岡県",
}

COL_KIKAN_BANGO_X1 = 100
COL_MEISHO = (110, 215)
COL_JUKI_BANGO = (430, 585)   # 受理番号（受理記号+第N号）確定行
COL_BIKO = (665, 730)         # 備考（内訳詳細）

_KNOWN_DETAIL_LABELS = {"病棟種別", "病床区分", "病棟数", "病床数", "区分"}


def _wareki_to_seireki(text: str) -> str:
    m = re.search(r"令和\s*(\d+)\s*年\s*(\d+)\s*月", text)
    if m:
        return f"{2018 + int(m.group(1))}-{int(m.group(2)):02d}"
    return ""


def _guess_pref_from_filename(path: str) -> str:
    stem = Path(path).stem
    for key, pref in FILENAME_PREF_HINTS.items():
        if key in stem:
            return pref
    return ""


def _is_anchor_word(text: str) -> bool:
    """医療機関番号アンカーか判定する（括弧付き枝番"(01,3089,4)"は除外）。"""
    text = text.strip()
    if text.startswith("("):
        return False
    return bool(KIKAN_BANGO_ANCHOR_RE.search(text))


def _parse_kikan_bango(first_line: str) -> str:
    m = KIKAN_BANGO_ANCHOR_RE.search(first_line.strip())
    if not m:
        return ""
    _, mid, chk = m.groups()
    return f"{mid.zfill(4)}{chk}".zfill(6)


def _group_words_by_line(words: list) -> list[tuple[float, str]]:
    """(x0, text) を返す（インデント判定用）。doctop基準でグルーピング。"""
    lines: dict[float, list] = {}
    for w in words:
        key = round(w["doctop"], 0)
        lines.setdefault(key, []).append(w)
    out = []
    for dtop in sorted(lines.keys()):
        ws = sorted(lines[dtop], key=lambda w: w["x0"])
        out.append((ws[0]["x0"], "".join(w["text"] for w in ws)))
    return out


def _group_words_by_line_top(words: list) -> list[tuple[float, str]]:
    """(doctop, text) を返す（別列との行位置マッチング用。ページ跨ぎ対応のためtopでなくdoctopを使う）。"""
    lines: dict[float, list] = {}
    for w in words:
        key = round(w["doctop"], 0)
        lines.setdefault(key, []).append(w)
    out = []
    for dtop in sorted(lines.keys()):
        ws = sorted(lines[dtop], key=lambda w: w["x0"])
        out.append((dtop, "".join(w["text"] for w in ws)))
    return out


def build_kigou_to_meisho(parquet_path: str) -> dict:
    """既存データから 受理記号→受理届出名称（最頻値） の辞書を作る。"""
    df = pd.read_parquet(parquet_path, columns=["受理記号", "受理届出名称"])
    df = df[df["受理記号"].notna() & df["受理記号"].astype(str).ne("")]
    counts = df.groupby(["受理記号", "受理届出名称"]).size().reset_index(name="cnt")
    counts = counts.sort_values("cnt", ascending=False)
    best = counts.drop_duplicates(subset=["受理記号"])
    return dict(zip(best["受理記号"], best["受理届出名称"]))


def parse_pdf(path: str, kigou_map: dict, pref_name_hint: str = "") -> tuple[pd.DataFrame, list]:
    """
    1医療機関のレコードが複数ページにまたがることがあるため、全ページの単語を
    doctop（文書全体のy座標）で結合してから処理する（ページ単位では境界を誤検出する）。
    """
    pref_name = pref_name_hint or _guess_pref_from_filename(path)
    pref_code = PREF_NAME_TO_CODE.get(pref_name, "")

    rows = []
    unmapped_kigou = []
    with pdfplumber.open(path) as pdf:
        header_text = pdf.pages[0].extract_text() or ""
        ym = _wareki_to_seireki(header_text)

        # 各ページ冒頭のヘッダー行（タイトル・列見出し・「電話番号（FAX番号）」）は
        # top<96 に固定で出現する。レコードがページを跨ぐと、後続ページのヘッダーが
        # 医療機関名称等の列に混入してしまうため除外する。
        HEADER_TOP_CUTOFF = 96
        all_words = []
        for page in pdf.pages:
            all_words.extend(w for w in page.extract_words() if w["top"] >= HEADER_TOP_CUTOFF)
        doc_bottom = max((w["doctop"] for w in all_words), default=0.0) + 20.0

        # doctopでソートしておき、各レコード区間の単語をbisectで取り出す
        # （医療機関数×総単語数のO(n^2)スキャンを避けるため）。
        all_words.sort(key=lambda w: w["doctop"])
        sorted_doctops = [w["doctop"] for w in all_words]

        anchors = sorted(set(
            w["doctop"] for w in all_words
            if w["x0"] < COL_KIKAN_BANGO_X1 and _is_anchor_word(w["text"])
        ))
        if not anchors:
            return pd.DataFrame(), unmapped_kigou

        for i, dtop in enumerate(anchors):
            y0 = dtop - 1
            y1 = anchors[i + 1] - 1 if i + 1 < len(anchors) else doc_bottom + 1
            lo = bisect.bisect_left(sorted_doctops, y0)
            hi = bisect.bisect_left(sorted_doctops, y1)
            rec_words = all_words[lo:hi]

            kb_words = [w for w in rec_words if w["x0"] < COL_KIKAN_BANGO_X1]
            kb_lines = _group_words_by_line(kb_words)
            if not kb_lines:
                continue
            iryo_bango = _parse_kikan_bango(kb_lines[0][1])
            if not iryo_bango:
                continue

            mn_x0, mn_x1 = COL_MEISHO
            mn_words = [w for w in rec_words if mn_x0 <= w["x0"] < mn_x1]
            meisho = "".join(t for _, t in _group_words_by_line(mn_words))

            jb_x0, jb_x1 = COL_JUKI_BANGO
            jb_words = [w for w in rec_words if jb_x0 <= w["x0"] < jb_x1]
            jb_lines = _group_words_by_line_top(jb_words)

            bk_x0, bk_x1 = COL_BIKO
            bk_words = [w for w in rec_words if bk_x0 <= w["x0"] < bk_x1]
            biko_lines_with_top = _group_words_by_line_top(bk_words)

            # 受理番号確定行ごとに、次の確定行が現れるまでの備考行を集める
            confirm_entries = []  # (doctop, kigou, num)
            for dtop_, text in jb_lines:
                m = CONFIRM_RE.search(text)
                if m:
                    confirm_entries.append((dtop_, m.group("label"), m.group("num")))

            # 大病院では確定行・備考行がそれぞれ数百〜数千に達し、単純な二重ループは
            # O(件数^2)になるため、備考行をdoctopでソートしbisectで区間取得する。
            biko_tops_sorted = [t for t, _ in biko_lines_with_top]

            for ci, (c_top, kigou, num) in enumerate(confirm_entries):
                next_top = confirm_entries[ci + 1][0] if ci + 1 < len(confirm_entries) else y1
                lo = bisect.bisect_right(biko_tops_sorted, c_top)
                hi = bisect.bisect_left(biko_tops_sorted, next_top)
                details = []
                for b_top, b_text in biko_lines_with_top[lo:hi]:
                    m = DETAIL_KV_RE.match(b_text)
                    if m:
                        details.append((m.group("label").strip(), m.group("value").strip()))
                    elif b_text.strip() and details:
                        lbl, val = details[-1]
                        details[-1] = (lbl, val + b_text.strip())

                meisho_todokede = kigou_map.get(kigou)
                if meisho_todokede is None:
                    unmapped_kigou.append(kigou)
                    meisho_todokede = kigou  # フォールバック：略号そのまま

                dd = dict(details)
                other = [f"{k}:{v}" for k, v in details if k not in _KNOWN_DETAIL_LABELS]
                rows.append({
                    "都道府県コード": pref_code,
                    "都道府県名": pref_name,
                    "医療機関番号": iryo_bango,
                    "医療機関名称": meisho,
                    "医療機関名_正規化": _normalize(meisho),
                    "受理届出名称": meisho_todokede,
                    "受理記号": kigou,
                    "受理番号": f"第{num}号",
                    "病棟種別": dd.get("病棟種別", ""),
                    "病床区分": dd.get("病床区分", ""),
                    "病棟数": dd.get("病棟数", ""),
                    "病床数": dd.get("病床数", ""),
                    "区分": dd.get("区分", ""),
                    "内訳その他": "；".join(other),
                    "年月": ym,
                })
    return pd.DataFrame(rows), unmapped_kigou


if __name__ == "__main__":
    path = sys.argv[1]
    pref_hint = sys.argv[2] if len(sys.argv) > 2 else ""
    kigou_map = build_kigou_to_meisho("/home/user/byosho-tool/shisetsu_kijun_cache.parquet")
    df, unmapped = parse_pdf(path, kigou_map, pref_hint)
    print(f"抽出行数: {len(df)}")
    print(df.head(30).to_string())
    print()
    print("マッピング不能な受理記号（頻度上位20）:")
    from collections import Counter
    print(Counter(unmapped).most_common(20))
    print()
    print("区分あり行数:", (df["区分"] != "").sum())
    print(df[df["区分"] != ""][["医療機関名称","受理届出名称","区分","病棟数","病床数"]].head(10).to_string())
