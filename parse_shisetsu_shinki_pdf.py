"""
「届出受理医療機関一覧表」新規・変更届出PDF（差分レポート）をパースする。

このPDFは shisetsu_kijun_cache.parquet と同じ列構成の「全件スナップショット」PDF
（parse_chubu_shisetsu_pdf.py が扱う形式）とは異なり、対象期間中に新規・変更が
あった届出だけを列挙する差分レポート。ただし列レイアウトは近く、
受理届出名称（正式名称）がそのまま載っているため、受理記号→正式名称の
逆引き辞書は不要。

列レイアウト（x0基準）:
  医療機関番号: x0 < 100（レコード開始アンカー、"010,674,9" や "01,1188,7" 等
                都道府県により桁の区切り方が違うが、カンマを除くと必ず7桁になる）
  医療機関名称: x0 ~100-224
  医療機関所在地: x0 ~224-347（郵便番号+住所、複数行に折り返すことがある）
  病床数: x0 ~347-384（入院を取らない診療所では空）
  受理内容: x0 >= 380。1つの届出につき2行1組:
    1行目: 受理届出名称（ラベル行）
    2行目: 「（受理記号）第N号 算定開始年月日：令和X年Y月Z日」（確定行）
    さらに3行目以降が続く場合は、その届出の内訳（病棟種別:X 等の
    key:value、またはラベル無しの追記テキスト）。次の届出のラベル行が
    現れるまでを内訳とみなす。
"""
import re
import bisect
from pathlib import Path

import pdfplumber
import pandas as pd

from build_shisetsu_kijun import _normalize, _normalize_label, _KNOWN_DETAIL_LABELS

# 医療機関番号の区切り文字は都道府県によりバラバラ（"010,674,9" "01-0308-5"
# "010,006.5" "01・1430・3" 等）で、さらに空白で2トークンに分割される県も
# ある（"011" "781.5"）ため、個々のトークンではなく行全体を連結してから
# 数字だけを抽出し7桁になるかで医療機関番号アンカー行を判定する
# （_find_anchors 参照）。
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
FILENAME_PREF_HINTS = {name[:2]: name for name in PREF_NAME_TO_CODE}

COL_KIKAN_BANGO_X1 = 100
COL_MEISHO = (100, 224)
COL_SHOZAICHI = (224, 347)
COL_BYOSHOSU = (347, 380)
COL_JUKI_NAIYOU_X0 = 380

HEADER_TOP_CUTOFF = 96


def _guess_pref_from_filename(path: str) -> str:
    stem = Path(path).stem
    for key, pref in FILENAME_PREF_HINTS.items():
        if stem.startswith(key):
            return pref
    return ""


def _iryo_bango_to_code(text: str) -> str:
    return re.sub(r"\D", "", text.strip())


def _group_by_doctop_join(words: list, sep: str = "") -> list[tuple[float, str]]:
    lines: dict[float, list] = {}
    for w in words:
        key = round(w["doctop"], 0)
        lines.setdefault(key, []).append(w)
    out = []
    for dtop in sorted(lines.keys()):
        ws = sorted(lines[dtop], key=lambda w: w["x0"])
        out.append((dtop, sep.join(w["text"] for w in ws)))
    return out


def _find_anchors(words: list, x0_lo: float, x0_hi: float) -> list[tuple[float, str]]:
    """
    医療機関番号アンカー行を検出する。区切り文字（, - ・ .）だけでなく、
    県によっては番号が空白で2トークンに分割される（例:"011" "781.5"）ため、
    トークン単体ではなく行全体を連結してから数字だけを7桁になるか判定する。
    括弧付き併設番号（"(01,3399,4)"）は除外する。
    """
    cand = [w for w in words if x0_lo <= w["x0"] < x0_hi and not w["text"].strip().startswith("(")]
    lines = _group_by_doctop_join(cand, sep="")
    out = []
    for dtop, text in lines:
        digits = re.sub(r"\D", "", text)
        if len(digits) == 7:
            out.append((dtop, digits))
    return out


def parse_pdf(path: str, pref_name_hint: str = "") -> pd.DataFrame:
    """
    1医療機関のレコードが複数ページにまたがることがあるため、全ページの単語を
    doctop（文書全体のy座標）で結合してから処理する。
    """
    pref_name = pref_name_hint or _guess_pref_from_filename(path)
    pref_code = PREF_NAME_TO_CODE.get(pref_name, "")

    rows = []
    with pdfplumber.open(path) as pdf:
        header_text = pdf.pages[0].extract_text() or ""
        m_ym = re.search(r"令和\s*(\d+)\s*年\s*(\d+)\s*月\s*(\d+)日\s*作成", header_text)
        created = f"令和{m_ym.group(1)}年{m_ym.group(2)}月{m_ym.group(3)}日" if m_ym else ""

        all_words = []
        for page in pdf.pages:
            all_words.extend(w for w in page.extract_words() if w["top"] >= HEADER_TOP_CUTOFF)
        doc_bottom = max((w["doctop"] for w in all_words), default=0.0) + 20.0

        all_words.sort(key=lambda w: w["doctop"])
        sorted_doctops = [w["doctop"] for w in all_words]

        anchor_hits = _find_anchors(all_words, 0, COL_KIKAN_BANGO_X1)
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

            sz_x0, sz_x1 = COL_SHOZAICHI
            sz_words = [w for w in rec_words if sz_x0 <= w["x0"] < sz_x1]
            juusho = "".join(t for _, t in _group_by_doctop_join(sz_words))
            juusho = re.sub(r"^〒\d{3}[－-]\d{4}", "", juusho).strip()

            jn_words = [w for w in rec_words if w["x0"] >= COL_JUKI_NAIYOU_X0]
            jn_lines = _group_by_doctop_join(jn_words, sep=" ")
            if not jn_lines:
                continue

            confirm_idx = [
                idx for idx, (_, text) in enumerate(jn_lines) if CONFIRM_RE.search(text)
            ]
            for ci, idx in enumerate(confirm_idx):
                c_dtop, c_text = jn_lines[idx]
                m = CONFIRM_RE.search(c_text)
                if not m:
                    continue
                kigou, num = m.group("label"), m.group("num")
                label = jn_lines[idx - 1][1].strip() if idx > 0 else kigou

                m_date = re.search(r"算定開始年月日\s*[：:]\s*(?P<date>.+)$", c_text)
                date_s = m_date.group("date").strip() if m_date else ""

                next_label_idx = confirm_idx[ci + 1] - 1 if ci + 1 < len(confirm_idx) else len(jn_lines)
                detail_lines = jn_lines[idx + 1:next_label_idx]

                d: dict[str, str] = {}
                others: list[str] = []
                for _, dtext in detail_lines:
                    dm = DETAIL_KV_RE.match(dtext.strip())
                    if dm:
                        lbl = _normalize_label(dm.group("label"))
                        val = dm.group("value").strip()
                        key = _KNOWN_DETAIL_LABELS.get(lbl)
                        if key and key not in d:
                            d[key] = val
                        elif val:
                            others.append(f"{lbl}:{val}")
                    elif dtext.strip():
                        others.append(dtext.strip())

                rows.append({
                    "都道府県コード": pref_code,
                    "都道府県名": pref_name,
                    "医療機関番号": iryo_bango,
                    "医療機関名称": meisho,
                    "医療機関名_正規化": _normalize(meisho),
                    "住所": juusho,
                    "受理届出名称": label,
                    "受理記号": kigou,
                    "受理番号": f"第{num}号",
                    "算定開始年月日": date_s,
                    "病棟種別": d.get("病棟種別", ""),
                    "病床区分": d.get("病床区分", ""),
                    "病棟数": d.get("病棟数", ""),
                    "病床数": d.get("病床数", ""),
                    "区分": d.get("区分", ""),
                    "内訳その他": "；".join(others),
                    "年月": created,
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
