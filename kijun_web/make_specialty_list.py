"""specialties.py と specialty_desc.py から、診療科ごとの届出の一覧（specialties.md）を作る。

    python kijun_web/make_specialty_list.py   （先に build_db.py で kijun.sqlite を作っておく）
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from specialties import SPECIALTIES, build  # noqa: E402
from specialty_desc import DESC  # noqa: E402

DB = HERE / "data" / "kijun.sqlite"
OUT = HERE / "specialties.md"


def _cell(s: str) -> str:
    return s.replace("|", "｜")


def main() -> int:
    missing = [p for s in SPECIALTIES for p in s["items"] if p not in DESC]
    if missing:
        print("説明が無い届出:", *missing, sep="\n  ")
        return 1
    con = sqlite3.connect(DB)
    asof = con.execute("SELECT value FROM meta WHERE key = 'as_of_max_label'").fetchone()[0]
    head = [
        "# 診療科ごとの特徴を示す施設基準の届出",
        "",
        f"全国の施設基準の届出（地方厚生局の名簿、{asof}までの公表分）から、診療科ごとに、その診療科で主に行う手術・検査・治療・入院管理の届出を**MedilenZが選んで分けた**一覧です。公式の「診療科別の届出一覧」はありません。",
        "",
        "- 「どんなものか」は、どんな手技・検査・治療・体制かをMedilenZが一般向けに短く説明したものです。届出の要件（施設基準）そのものではありません。正式な定義は厚生労働省の告示・通知（「基本診療料の施設基準等」「特掲診療料の施設基準等」）によります。",
        "- 名前の似た届出（１〜３などの区分、手術支援ロボットを使うものなど）は1行にまとめ、件数はそのどれかを届け出ている医療機関の数です。",
        "- 1つの届出が複数の診療科に入ることがあります（例：経カテーテル弁置換術は循環器内科と心臓血管外科）。",
        "- ほとんどの医療機関が届け出ていて特徴にならない届出（感染対策・ベースアップ評価料など）は入れていません。",
        "- 届出が要らない手術・治療（ふつうのPCI・胃のESD・ステントグラフトなど）は名簿に載らないため、ここにもありません。届出があっても、診療の量（件数）は分かりません。",
        "- 定義は `kijun_web/specialties.py`（分け方）と `kijun_web/specialty_desc.py`（説明）。",
        "",
        "## 目次",
        "",
    ]
    body: list[str] = []
    for s in build(con):
        head.append(f"- [{s['name']}](#{s['name']})（{len(s['rows'])}項目）")
        body += ["", f"## {s['name']}", "",
                 f"いずれかを届け出ている医療機関：{s['n']:,}件（うち病院 {s['n_hosp']:,}件）", "",
                 "| 届出 | どんなものか | 届け出ている医療機関 |", "|---|---|--:|"]
        for r in s["rows"]:
            name = r["name"] + (f"（{r['variants']}種類）" if r["variants"] > 1 else "")
            body.append(f"| {_cell(name)} | {_cell(r['desc'])} | {r['n']:,} |")
    OUT.write_text("\n".join(head + body) + "\n", encoding="utf-8")
    print(f"{len(SPECIALTIES)}分野・{sum(len(s['items']) for s in SPECIALTIES)}行 → {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
