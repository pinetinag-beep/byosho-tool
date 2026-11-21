"""地域医療構想の医療機関機能ごとに、関係する施設基準の届出を並べるページ（/vision/…）の定義。

医療機関機能（急性期拠点機能など）は、令和8年度から各医療機関が都道府県に「報告」するもので、
届出から決まるものではない。ここでは厚生労働省「地域医療構想策定ガイドライン」の各機能の説明に
出てくる医療（救急・手術・リハビリ・退院調整・在宅・慢性期の入院など）に対応する診療報酬の届出を
MedilenZが選んで並べるだけで、どの病院がどの機能かは判定しない（画面にもそう書く）。

列を変えたら build_db.py の「前回の更新からの変化」（vision_change）も同じ定義で作られる。
"""
from __future__ import annotations

# like: 届出名称（kijun.name）のLIKEパターン。kubun: 入院基本料の区分（公式の値だけを使う）
FUNCS = [
    {
        "key": "kyoten",
        "name": "急性期拠点機能",
        "desc": "手術や救急医療など、医療資源を多く要する症例を集約して提供する機能。"
                "人口20〜30万人に1つが目安とされています。",
        "cols": [
            {"key": "kyumei", "label": "救命救急入院料", "like": ["救命救急入院料%"]},
            {"key": "icu", "label": "特定集中治療室管理料", "like": ["特定集中治療室管理料%"]},
            {"key": "kyusou", "label": "急性期総合体制加算", "like": ["急性期総合体制加算%"]},
            {"key": "kyubyo", "label": "急性期病院Ａ／Ｂ一般入院料",
             "kubun": {"急性期病院Ａ一般入院料": "Ａ", "急性期病院Ｂ一般入院料": "Ｂ"}},
        ],
    },
    {
        "key": "koreisha",
        "name": "高齢者救急・地域急性期機能",
        "desc": "高齢者などの救急搬送を受け入れ、入院早期からのリハビリテーションや退院調整を行う機能。",
        "cols": [
            {"key": "chiho", "label": "地域包括医療病棟入院料", "like": ["地域包括医療病棟入院料%"]},
            {"key": "chicare", "label": "地域包括ケア病棟入院料", "like": ["地域包括ケア病棟入院料%"]},
            {"key": "nyutai", "label": "入退院支援加算", "like": ["入退院支援加算"]},
        ],
    },
    {
        "key": "zaitaku",
        "name": "在宅医療等連携機能",
        "desc": "在宅医療を行い、ほかの医療機関・介護施設・訪問看護などと連携して、24時間の対応や入院の受け入れを行う機能。",
        "cols": [
            {"key": "shien", "label": "在宅療養支援病院", "like": ["%に規定する在宅療養支援病院"]},
            {"key": "kohou", "label": "在宅療養後方支援病院", "like": ["在宅療養後方支援病院"]},
        ],
    },
    {
        "key": "senmon",
        "name": "専門等機能",
        "desc": "集中的なリハビリテーションや慢性期の入院医療など、上の3つに当てはまらない機能。",
        "cols": [
            {"key": "kaifuku", "label": "回復期リハビリテーション病棟入院料", "like": ["回復期リハビリテーション病棟入院料%"]},
            {"key": "ryoyo", "label": "療養病棟入院基本料", "like": ["療養病棟入院基本料"]},
            {"key": "shogai", "label": "障害者施設等入院基本料", "like": ["障害者施設等入院基本料"]},
        ],
    },
]

COLS = [c for f in FUNCS for c in f["cols"]]
COL_LABEL = {c["key"]: c["label"] for c in COLS}
COL_FUNC = {c["key"]: f["name"] for f in FUNCS for c in f["cols"]}


def presence(con, where: str = "", params: tuple = ()) -> dict[tuple[str, str], str]:
    """{(医療機関ID, 列のkey): 表示する値}。届け出ていれば "○"、急性期病院一般入院料は "Ａ"/"Ｂ"。
    where は facilities f に対する条件（例: "f.pref_code = ? AND f.area = ?"）。病院だけを対象にする。"""
    cond = "f.ftype = '病院'" + (f" AND {where}" if where else "")
    out: dict[tuple[str, str], str] = {}
    for c in COLS:
        if "like" in c:
            likes = " OR ".join("k.name LIKE ?" for _ in c["like"])
            for (fid,) in con.execute(
                f"""SELECT DISTINCT f.id FROM filings fl JOIN kijun k ON k.id = fl.kijun_id
                    JOIN facilities f ON f.id = fl.facility_id WHERE ({likes}) AND {cond}""",
                (*c["like"], *params)):
                out[(fid, c["key"])] = "○"
        else:
            names = list(c["kubun"])
            for fid, name in con.execute(
                f"""SELECT f.id, kb.name FROM fac_kubun fk JOIN kubuns kb ON kb.id = fk.kubun_id
                    JOIN facilities f ON f.id = fk.facility_id
                    WHERE fk.ref = 0 AND kb.name IN ({",".join("?" * len(names))}) AND {cond}
                    ORDER BY kb.name""",
                (*names, *params)):
                v = c["kubun"][name]
                prev = out.get((fid, c["key"]))
                out[(fid, c["key"])] = v if not prev or prev == v else prev + "・" + v
    return out
