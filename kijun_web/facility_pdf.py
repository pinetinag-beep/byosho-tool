"""医療機関・歯科診療所・薬局のページの内容をPDFにする（/facility/{id}.pdf・/shika/{id}.pdf・/yakkyoku/{id}.pdf）。

右上にそのページを開くQRコードを入れる（reportlab 内蔵の QrCodeWidget。追加の依存なし）。
日本語は IPAゴシック（kijun_web/fonts/）を、使う文字だけ埋め込む。
中身はページと同じ公表データで、出典（PDL1.0：出典・加工した旨）を末尾に付ける。
"""
from __future__ import annotations

import time
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from pathlib import Path

from reportlab.pdfbase.ttfonts import TTFont

# 使う文字だけを埋め込む（埋め込まないと、日本語フォントを持たない閲覧ソフトで文字が出ない）。
# IPAゴシック（IPAフォントライセンス。kijun_web/fonts/IPA_Font_License.txt）。無ければ埋め込まないCIDフォント
_FONT_FILES = [Path(__file__).resolve().parent / "fonts" / "ipag.ttf",
               Path("/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf")]
_ttf = next((p for p in _FONT_FILES if p.exists()), None)
if _ttf:
    FONT = "IPAGothic"
    pdfmetrics.registerFont(TTFont(FONT, str(_ttf)))
else:
    FONT = "HeiseiKakuGo-W5"
    pdfmetrics.registerFont(UnicodeCIDFont(FONT))

# DESIGN.md のトークンと同じ値（ブランドの緑・インク・罫線）
BRAND = colors.HexColor("#12886D")
BRAND_DARK = colors.HexColor("#0B4F40")
BRAND_SOFT = colors.HexColor("#E8F4F0")
INK = colors.HexColor("#26251F")
MUTED = colors.HexColor("#6E6A5E")
LINE = colors.HexColor("#E8E4DB")
HEAD_BG = colors.HexColor("#F3F1EC")

PAGE_W, PAGE_H = A4
MARGIN = 14 * mm
BODY_W = PAGE_W - 2 * MARGIN


def _st(size, color=INK, lead=None, **kw):
    return ParagraphStyle("s", fontName=FONT, fontSize=size, leading=lead or size * 1.45, textColor=color,
                          wordWrap="CJK", **kw)


S_TITLE = _st(17, INK, 23)
S_SUB = _st(9, MUTED)
S_H2 = _st(12, BRAND_DARK, 16, spaceBefore=10, spaceAfter=4)
S_H3 = _st(9.5, INK, 13, spaceBefore=6, spaceAfter=3)
S_P = _st(8.5)
S_SMALL = _st(7.2, MUTED)
S_CELL = _st(7.6, INK, 10.2)
S_CELL_C = _st(7.6, INK, 10.2, alignment=1)
S_HEAD = _st(7.2, MUTED, 9.5)
S_HEAD_C = _st(7.2, MUTED, 9.5, alignment=1)


def _p(text, style=S_P):
    return Paragraph(escape(str(text)) if text is not None else "", style)


def _num(v, unit=""):
    if v is None or v == "":
        return "—"
    try:
        return f"{int(v):,}{unit}"
    except (TypeError, ValueError):
        return f"{v}{unit}"


def _table(header, rows, widths, center_from=None):
    """header: 列名、rows: 文字列の行、widths: 本文幅に対する割合。center_from 以降の列は中央寄せ。"""
    cw = [BODY_W * w / sum(widths) for w in widths]

    def cell(v, i, head=False):
        c = center_from is not None and i >= center_from
        return _p(v, (S_HEAD_C if c else S_HEAD) if head else (S_CELL_C if c else S_CELL))

    data = [[cell(h, i, True) for i, h in enumerate(header)]] + [[cell(v, i) for i, v in enumerate(r)] for r in rows]
    t = Table(data, colWidths=cw, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), HEAD_BG),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE),
        ("BOX", (0, 0), (-1, -1), 0.4, LINE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]))
    return t


def _facts(pairs, cols=4):
    """(見出し, 値) を枠付きの小さな箱で並べる（ページの .facts と同じ見せ方）。"""
    cells = [[_p(k, S_SMALL), Paragraph(escape(str(v)), _st(10.5, INK, 13))] for k, v in pairs]
    rows = [cells[i:i + cols] for i in range(0, len(cells), cols)]
    rows[-1] += [""] * (cols - len(rows[-1]))
    t = Table([[c if c else "" for c in r] for r in rows], colWidths=[BODY_W / cols] * cols)
    t.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.4, LINE), ("INNERGRID", (0, 0), (-1, -1), 0.4, LINE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    return t


def _qr(url: str, size: float) -> Drawing:
    w = QrCodeWidget(url, barLevel="M")
    x0, y0, x1, y1 = w.getBounds()
    d = Drawing(size, size, transform=[size / (x1 - x0), 0, 0, size / (y1 - y0), 0, 0])
    d.add(w)
    return d


def _header(name: str, sub: str, url: str, hp: str = ""):
    qr = 26 * mm
    left = [Paragraph('<font color="#12886D">MedilenZ</font>', _st(9, BRAND, 12)), Spacer(1, 2),
            _p(name, S_TITLE), Spacer(1, 3), _p(sub, S_SUB)] + ([_p(f"ホームページ：{hp}", S_SUB)] if hp else [])
    right = [_qr(url, qr), _p("スマホで読み取ると、このページの最新の内容を開けます", _st(6.2, MUTED, 8.5, alignment=1))]
    t = Table([[left, right]], colWidths=[BODY_W - qr - 8 * mm, qr + 8 * mm])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                           ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("ALIGN", (1, 0), (1, 0), "CENTER")]))
    return [t, Spacer(1, 2), _p(url, S_SMALL),
            Table([[""]], colWidths=[BODY_W], style=[("LINEBELOW", (0, 0), (-1, -1), 1.2, BRAND)]), Spacer(1, 4)]


# ── 診療科・診療時間（医療情報ネット） ──────────────────────────
def _hours(navi: dict, word: str) -> list:
    if not navi:
        return []
    out = [_p("営業時間" if word == "薬局" else "診療科・診療時間", S_H2)]
    if navi.get("specs"):
        out.append(_p("診療科：" + "・".join(navi["specs"]), S_P))
    if navi.get("closed") or navi.get("other"):
        out.append(_p(f"{'定休日' if word == '薬局' else '休診日'}：{navi.get('closed') or '—'}"
                      + (f"（{navi['other']}）" if navi.get("other") else ""), S_P))
    days = navi["days"]
    for g in navi["groups"]:
        block = []
        if g["specs"] and (len(navi["groups"]) > 1 or len(g["specs"]) < len(navi["specs"])):
            block.append(_p("・".join(g["specs"]), S_H3))
        rows = [[lab] + [c.replace("-", "〜") if c else "—" for c in cells] for lab, cells in g["rows"]]
        block.append(_table([""] + list(days), rows, [1.1] + [2] * len(days), center_from=1))
        block.append(Spacer(1, 4))
        out.append(KeepTogether(block))
    out.append(_p(f"{'薬局' if word == '薬局' else '医療機関'}が都道府県に報告した内容で、変わっていることがあります。"
                  f"{'行く' if word == '薬局' else '受診の'}前に確かめてください。", S_SMALL))
    out.append(_p(f"出典：「医療情報ネットのオープンデータ」（厚生労働省）（{navi['src_url']}）の"
                  f"{navi.get('as_of_label', '')}時点のデータを加工して作成", S_SMALL))
    return out


def _kijun_source(pref) -> str:
    if pref is None:
        return ""
    if pref["bureau"]:
        s = f"出典：「{pref['bureau_page']}」（{pref['bureau']}）（{pref['bureau_url']}）"
        if pref["used_on_label"]:
            s += f"（{pref['used_on_label']}に利用）"
    else:
        s = f"出典：{pref['name']}を管轄する地方厚生局「施設基準の届出受理状況」"
    return s + f"を加工して作成（{pref['as_of_label']}時点の名簿）"


def _changes(kd, month_label) -> list:
    if not kd:
        return []
    lab = {"新規": "新規", "変更": "区分の変更", "取り下げ": "取り下げ等"}
    rows = [[f"{month_label(r['prev_month'])}→{month_label(r['month'])}", lab.get(r["kind"], r["kind"]),
             (f"{r['prev_name']} → " if r["kind"] == "変更" else "") + r["kijun_name"]] for r in kd]
    return [_p("届出の動き（毎月の名簿の比較）", S_H2), _table(["名簿の時点", "動き", "届出"], rows, [2.2, 1.3, 6.5])]


# ── 病院・診療所（医科） ─────────────────────────────────────
def medical(c: dict, url: str, jdate, month_label, src_urls: dict) -> list:
    f = c["f"]
    code10 = f"{f['pref_code']}1{f['code']}"
    area = f["area"] if f["area"] and not f["area"].startswith("（") else ""
    story = _header(f["name"], f"{f['ftype']}　{f['pref_name']}{f['address'] or ''}"
                    + (f"（二次医療圏：{area}）" if area else ""), url, (c["navi"] or {}).get("url", ""))
    story.append(_facts([("届け出ている施設基準", f"{f['n_kijun']} 種類"), ("医療機関コード（10桁）", code10),
                         (f"医療機関番号（{f['pref_name']}）", f["code"]), ("データ時点", c["pref"]["as_of_label"])]))
    story += _hours(c["navi"], "医療機関")

    b = c["byosho"]
    if b:
        story.append(_p("病床・人員・設備（病床機能報告）", S_H2))
        story.append(_p(f"{b['label']}（{b['year']}年7月1日時点）の病床機能報告による。", S_SMALL))
        story.append(_facts([("許可病床数", _num(b["total"], " 床")),
                             ("病床稼働率 ※", f"{b['occ']} %" if b["occ"] is not None else "—"),
                             ("常勤医師数", _num(b["doctors"], " 人")), ("常勤看護師数", _num(b["nurses"], " 人")),
                             ("救急車の受入件数（年間）", _num(b["ambulance"], " 件"))], cols=5))
        if b["total"]:
            story.append(Spacer(1, 4))
            story.append(_p("病床機能の内訳：" + "　".join(f"{x['name']} {x['beds']:,}床（{x['pct']}%）" for x in b["funcs"]), S_P))
        if b["equipment"] or b["staff"]:
            items = [f"{e['label']} {e['n']}台" + (f"（{e['detail']}）" if e.get("detail") else "") for e in b["equipment"]]
            items += [f"常勤{lab} {n:,}人" for lab, n in b["staff"]]
            story.append(_p("主な医療設備・医療職：" + "、".join(items), S_P))
        if b["wards"]:
            story.append(_p(f"病棟ごとの機能区分と入院基本料（{len(b['wards'])}病棟）", S_H3))
            story.append(_table(["機能区分", "入院基本料・特定入院料", "許可病床数"],
                                [[w["func"] or "—", w["fee"] or "—", _num(w["beds"])] for w in b["wards"]], [1.6, 6, 1.4], 2))
        if len(b["trend"]) > 1:
            story.append(_p("年度ごとの推移", S_H3))
            names = ["高度急性期", "急性期", "回復期", "慢性期"]
            story.append(_table(["報告年度", "許可病床数", "病床機能の内訳", "稼働率※", "常勤医師", "常勤看護師", "救急車受入"],
                                [[t["label"], _num(t["total"]),
                                  " ".join(f"{n}{v}" for n, v in zip(names, t["beds"]) if v),
                                  f"{t['occ']}%" if t["occ"] is not None else "—", _num(t["doctors"]), _num(t["nurses"]),
                                  _num(t["ambulance"])] for t in b["trend"]], [1.5, 1.2, 3.6, 1, 1, 1.1, 1.1], 1))
        story.append(_p("※ 病床稼働率＝在棟患者延べ数 ÷（許可病床数 × 365日）。病床機能報告の定義による。許可病床数は各年度の7月1日時点、"
                        "在棟患者延べ数はその前年度1年間の実績のため、期間が1年ずれています。", S_SMALL))
        story.append(_p(f"出典：「病床機能報告」（厚生労働省）（{src_urls['病床機能報告']}）の{b['years']}分を加工して作成", S_SMALL))

    d = c["dpc"]
    if d:
        story.append(_p("入院患者の診療実績（DPC）", S_H2))
        story.append(_p(f"{d['label']}（{d['year']}年4月〜{d['year'] + 1}年3月の退院患者）のDPC退院患者調査による。{d['kind']}"
                        + (f"　DPC算定病床数 {d['beds']:,}床" if d.get("beds") else ""), S_SMALL))
        story.append(_facts([("退院患者数（年間）", _num(d["total"], " 件"))]
                            + [(x["label"], f"{x['n']:,} 件（{x['pct']}%）" if x["n"] is not None else "—") for x in d["procs"]], cols=3))
        if d["mdc"]:
            story.append(_p("疾患の分野（主要診断群）ごとの患者数", S_H3))
            story.append(_table(["主要診断群（MDC）", "手術あり", "手術なし", "計"],
                                [[m["label"], _num(m["surg"]), _num(m["nosurg"]), _num(m["known"]) + ("＋" if m["partial"] else "")]
                                 for m in d["mdc"]], [6, 1.3, 1.3, 1.3], 1))
            story.append(_p("「—」は10件未満のため公表されていない件数（「＋」の付いた計は公表されている件数だけの合計）。", S_SMALL))
        story.append(_p(f"出典：{d['sources']}（厚生労働省）（{src_urls['DPC']}）を加工して作成", S_SMALL))

    g = c["gairai"]
    if g:
        pct = lambda v: f"{v} %" if v is not None else "—"
        story.append(_p("外来の状況（外来機能報告）", S_H2))
        story.append(_p(f"{g['label']}の外来機能報告による。" + (f"設置主体：{g['owner']}" if g.get("owner") else ""), S_SMALL))
        story.append(_facts([("初診患者数（年間）", _num(g["n_first"], " 人")), ("紹介率 ※1", pct(g["referral_rate"])),
                             ("逆紹介率 ※1", pct(g["back_rate"])), ("紹介受診重点外来（初診）※2", pct(g["focus_first"])),
                             ("紹介受診重点外来（再診）※2", pct(g["focus_re"]))], cols=5))
        story.append(_p("※1 紹介率＝紹介患者数 ÷ 初診患者数、逆紹介率＝逆紹介患者数 ÷ 初診患者数（外来機能報告の定義）。"
                        "※2 初診（再診）の外来の患者延べ数のうち、紹介受診重点外来の患者延べ数の割合。", S_SMALL))
        story.append(_p(f"出典：「外来機能報告」（厚生労働省）（{src_urls['外来機能報告']}）の{g['years']}分を加工して作成", S_SMALL))

    story.append(_p(f"施設基準の届出一覧（{f['n_kijun']}種類）", S_H2))
    rows, last = [], None
    for x in c["filings"]:
        if x["section"] != last:
            rows.append([f"［{x['section']}］", "", "", "", ""])
            last = x["section"]
        kubun = x["kubun"] or (f"（参考）{x['kubun_ref']}" if x["kubun_ref"] else "")
        if x["other"]:
            kubun = (kubun + " " if kubun else "") + x["other"]
        beds = " ".join(s for s in [x["byoto"] or "", x["byosho_kubun"] or "",
                                     f"{x['n_byoto']}病棟" if x["n_byoto"] else "", f"{x['n_byosho']}床" if x["n_byosho"] else ""] if s)
        rows.append([x["kijun"], kubun, beds, jdate(x["start_date"]) or (x["start_raw"] or ""),
                     f"{x['symbol'] or ''}{x['juri_no'] or ''}"])
    story.append(_table(["施設基準（受理届出名称）", "区分・内訳", "病棟・病床", "算定開始日", "受理番号"], rows, [4.4, 2.5, 1.5, 1.4, 1.9]))
    if any(x["kubun_ref"] and not x["kubun"] for x in c["filings"]):
        story.append(_p("（参考）の区分は、区分が公表されていない届出に厚生労働省「病床機能報告」の病棟の入院基本料を加工して補った値で、"
                        "その後に届出を出し直した場合は変わっていることがあります。", S_SMALL))
    story.append(_p(_kijun_source(c["pref"]), S_SMALL))
    story += _changes(c["kd"], month_label)
    return story


# ── 歯科診療所・薬局 ───────────────────────────────────────
def other(c: dict, url: str, jdate, month_label) -> list:
    K, f = c["K"], c["f"]
    story = _header(f["name"], f"{K.word}　{f['pref_name']}{f['address'] or ''}", url, (c["navi"] or {}).get("url", ""))
    story.append(_facts([(K.col1, K.v1(f["kihon"]) if f["kihon"] else "届出なし"),
                         (K.col2, K.v2(f["chiiki"]) if f["chiiki"] else "届出なし"),
                         ("医療機関コード（10桁）", f["id"]),
                         ("データ時点", c["pref"]["as_of_label"] if c["pref"] else "—")]))
    story += _hours(c["navi"], K.word)
    story.append(_p(f"届け出ている施設基準（{c['n_filings']}種類）", S_H2))
    for g in c["groups"]:
        story.append(_p(g["label"], S_H3))
        story.append(_table(["届出", "どんなものか", "受理番号", "算定開始"],
                            [[r["name"] + (f"（{r['symbol']}）" if r.get("symbol") else ""), r.get("desc", ""),
                              r.get("juri_no") or "", jdate(r.get("start_date"))] for r in g["rows"]], [3.6, 5, 1.3, 1.5]))
    story.append(_p("説明はMedilenZがまとめたもので、届出の要件そのものではありません。", S_SMALL))
    story.append(_p(_kijun_source(c["pref"]), S_SMALL))
    story += _changes(c["kd"], month_label)
    return story


def render(story: list, title: str, site_url: str) -> bytes:
    made = time.strftime("%Y年%-m月%-d日")
    foot = (f"MedilenZ（{site_url.replace('https://', '')}）が公表データを編集・加工して作成。"
            f"厚生労働省・地方厚生局が作成したものではありません。　{made}作成")

    def on_page(canvas, doc):
        canvas.saveState()
        canvas.setFont(FONT, 6.5)
        canvas.setFillColor(MUTED)
        canvas.drawString(MARGIN, 8 * mm, foot)
        canvas.drawRightString(PAGE_W - MARGIN, 8 * mm, f"{doc.page}")
        canvas.restoreState()

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=MARGIN, rightMargin=MARGIN, topMargin=12 * mm,
                            bottomMargin=14 * mm, title=title, author="MedilenZ", subject=title)
    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    return buf.getvalue()
