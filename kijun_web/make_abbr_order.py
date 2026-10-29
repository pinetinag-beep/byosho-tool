"""厚労省「届出受理医療機関名簿の受理番号欄における略称一覧」PDFから、受理記号の分類
（基本診療料・特掲診療料など）と並び順を kijun_web/abbr_order.json に書き出す。

検索フォームの届出項目は、この順番・分類で並べる（地方厚生局の名簿や他の検索サイトと同じ並び）。
PDFは2段組みなので、ページごとに左右の段を分けて上から読む。

    python kijun_web/make_abbr_order.py  [PDFのパス]

開発環境だけで使う（pdfplumber が必要。サーバーの build_db.py は出来上がったJSONを読むだけ）。
改定で新しい略称一覧が出たら shisetsu_raw/ のPDFを差し替えて、これを実行し直すこと。
PDFに無い記号（改定後に新設されたもの等）は build_db.py が届出の並び順から推定する。
"""
from __future__ import annotations

import collections
import json
import re
import sys
import unicodedata
from pathlib import Path

import pdfplumber

HERE = Path(__file__).resolve().parent
# 新しい版を置いたらファイル名順で最後のものを使う（例: 略称一覧_R0810.pdf）
DEFAULT_PDF = sorted((HERE.parent / "shisetsu_raw").glob("*略称一覧*.pdf"))[-1]
OUT = HERE / "abbr_order.json"
SECTIONS = ("基本診療料", "特掲診療料", "入院時食事療養", "その他の届出", "先進医療")
_RANGE = re.compile(r"(.*?)(\d+)~(\d+)\s*(.*)")  # 「入ベ1~500」のように番号の範囲でまとめた略称


_TWO_ITEMS = re.compile(r"^\(.+?\).*\S\s*\(.{1,12}?\)")


def _lines(pdf_path: Path) -> list[str]:
    """ページの行を上から順に返す。版によって1段組み（令和8年10月版）と2段組み（令和6年版）が
    あるので、1行に略称が2つ並ぶページだけ左右の段に分けて読む。"""
    out = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text_lines = (page.extract_text() or "").splitlines()
            if not any(_TWO_ITEMS.match(unicodedata.normalize("NFKC", t)) for t in text_lines):
                out += text_lines
                continue
            words = page.extract_words(keep_blank_chars=True, x_tolerance=1.5)
            mid = page.width / 2
            for left in (True, False):
                rows = collections.defaultdict(list)
                for w in words:
                    if (w["x0"] < mid) == left:
                        rows[round(w["top"] / 3)].append(w)
                for k in sorted(rows):
                    out.append("".join(w["text"] for w in sorted(rows[k], key=lambda w: w["x0"])))
    return out


def extract(pdf_path: Path) -> list[dict]:
    section, items, seen = None, [], set()
    for raw in _lines(pdf_path):
        t = unicodedata.normalize("NFKC", raw).strip()
        head = re.search(r"略称一覧\s*\((.+?)\)", t) or re.fullmatch(r"\(?(.+?)\)?", t)
        if head and head.group(1) in SECTIONS:
            section = head.group(1)
            continue
        # 先進医療だけは括弧なしで「先003 名称」「先-386」と書かれている
        m = re.match(r"\((.+?)\)\s*(.*)", t) or re.match(r"(先-?\d+)\s*(.*)", t)
        if not (section and m and len(m.group(1)) <= 16):
            continue
        sym, name = m.group(1).strip(), m.group(2).strip()
        r = _RANGE.fullmatch(sym)
        syms = [f"{r[1]}{i}{r[4]}".replace(" ", "") for i in range(int(r[2]), int(r[3]) + 1)] if r else [sym]
        for s in syms:
            if s not in seen:
                seen.add(s)
                items.append({"section": section, "symbol": s, "name": name})
    return items


def main() -> int:
    pdf_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PDF
    items = extract(pdf_path)
    OUT.write_text("[\n" + ",\n".join(json.dumps(i, ensure_ascii=False) for i in items) + "\n]\n", encoding="utf-8")
    print(f"{len(items)}件 → {OUT}", collections.Counter(i["section"] for i in items))
    return 0


if __name__ == "__main__":
    sys.exit(main())
