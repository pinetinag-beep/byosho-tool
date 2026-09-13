"""病院1件分のデータをA4のPDF資料として出力する（人材紹介エージェント等が
「提案候補となる病院の診療データを一括で見て、そのまま出力できる」ことを
想定した機能）。

Streamlit/セッション状態には依存しない（app.py側で必要なデータを集めて
dictに詰め、build_hospital_report_pdf() に渡すだけの設計）。reportlabの
内蔵CID日本語フォント（HeiseiKakuGo-W5）を使うため、追加のフォントファイル
配置は不要。
"""
from __future__ import annotations

from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A3, A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import (
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

_FONT = "HeiseiKakuGo-W5"
_BRAND_GREEN = colors.HexColor("#12886D")
_INK = colors.HexColor("#26251F")
_INK_MUTED = colors.HexColor("#6E6A5E")
_LINE = colors.HexColor("#E8E4DB")
_TINT = colors.HexColor("#EAF4F0")

_fonts_registered = False


def _ensure_fonts() -> None:
    global _fonts_registered
    if _fonts_registered:
        return
    pdfmetrics.registerFont(UnicodeCIDFont(_FONT))
    _fonts_registered = True


def _styles() -> dict[str, ParagraphStyle]:
    return {
        "title": ParagraphStyle("title", fontName=_FONT, fontSize=18, textColor=_INK, leading=22),
        "meta": ParagraphStyle("meta", fontName=_FONT, fontSize=9.5, textColor=_INK_MUTED, leading=13),
        "section": ParagraphStyle(
            "section", fontName=_FONT, fontSize=12.5, textColor=_INK, leading=16,
            spaceBefore=14, spaceAfter=6, borderPadding=0,
        ),
        "body": ParagraphStyle("body", fontName=_FONT, fontSize=9.5, textColor=_INK, leading=14),
        "note": ParagraphStyle("note", fontName=_FONT, fontSize=8, textColor=_INK_MUTED, leading=11),
        "kpi_label": ParagraphStyle("kpi_label", fontName=_FONT, fontSize=8.5, textColor=_INK_MUTED, leading=11),
        "kpi_value": ParagraphStyle("kpi_value", fontName=_FONT, fontSize=15, textColor=_INK, leading=18),
    }


def _section_header(text: str, styles: dict) -> Table:
    """緑の左バー付きセクション見出し（アプリ本体の.section-headerに寄せたデザイン）。"""
    t = Table(
        [[Paragraph(text, styles["section"])]],
        colWidths=[None],
    )
    t.setStyle(TableStyle([
        ("LINEBEFORE", (0, 0), (0, 0), 3, _BRAND_GREEN),
        ("LEFTPADDING", (0, 0), (0, 0), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return t


def _source_note(text: str, styles: dict) -> Paragraph:
    return Paragraph(f"データ出典：{text}", styles["note"])


def _kpi_row(items: list[tuple[str, str]], styles: dict, col_widths=None) -> Table:
    """[(label, value), ...] を横並びのKPIカード風テーブルにする。"""
    cells = [
        [Paragraph(label, styles["kpi_label"]), Paragraph(value, styles["kpi_value"])]
        for label, value in items
    ]
    # 1カードを1セルにまとめるため、Paragraphを縦積みしたミニテーブルを作る
    card_cells = []
    for label, value in items:
        inner = Table(
            [[Paragraph(label, styles["kpi_label"])], [Paragraph(value, styles["kpi_value"])]],
            colWidths=[None],
        )
        inner.setStyle(TableStyle([
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ]))
        card_cells.append(inner)
    n = len(card_cells)
    outer = Table([card_cells], colWidths=col_widths or [None] * n)
    outer.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("BACKGROUND", (0, 0), (-1, -1), colors.white),
        ("BOX", (0, 0), (-1, -1), 0.5, _LINE),
        ("LINEAFTER", (0, 0), (-2, 0), 0.5, _LINE),
    ]))
    return outer


def _data_table(header: list[str], rows: list[list[str]], styles: dict, col_widths=None) -> Table:
    body_style = styles["body"]
    data = [[Paragraph(f"<b>{h}</b>", body_style) for h in header]]
    for row in rows:
        data.append([Paragraph(str(c), body_style) for c in row])
    t = Table(data, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), _TINT),
        ("GRID", (0, 0), (-1, -1), 0.4, _LINE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]))
    return t


def build_hospital_report_pdf(data: dict, page_size: str = "A4") -> bytes:
    """`data`（app.py側で組み立てた病院1件分の辞書）からPDFバイト列を作る。

    `data`のキーはこのモジュール専用の中間形式（docstring末尾のスキーマ参照）。
    ページサイズは"A4"（既定）または"A3"。
    """
    _ensure_fonts()
    styles = _styles()
    buf = BytesIO()
    pagesize = A3 if page_size == "A3" else A4
    doc = SimpleDocTemplate(
        buf, pagesize=pagesize,
        leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=16 * mm, bottomMargin=16 * mm,
        title=f"{data['hospital']} 医療機関情報資料",
    )
    story = []

    # ── ヘッダー ──
    story.append(Paragraph(data["hospital"], styles["title"]))
    story.append(Paragraph(
        f"{data['pref']} ／ {data['region']}　｜　{data['year']}年度（{data.get('nendo_label', '')}）",
        styles["meta"],
    ))
    if data.get("address"):
        story.append(Paragraph(data["address"], styles["meta"]))
    if data.get("emergency_designations"):
        story.append(Paragraph(
            "救急関連の届出：" + "、".join(data["emergency_designations"]),
            styles["meta"],
        ))
    story.append(Spacer(1, 4 * mm))

    # ── 病床数・稼働率 ──
    story.append(_section_header("病床数・病床稼働率", styles))
    story.append(_source_note("病床機能報告", styles))
    beds = data["beds"]
    kpi_items = [
        ("許可病床数", f"{beds['total_kyoka']:,}床"),
        ("稼働病床数", f"{beds['total_kado']:,}床"),
        ("総稼働率", f"{beds['occ_pct']:.1f}%" if beds.get("occ_pct") is not None else "―"),
    ]
    if data.get("region_share_pct") is not None:
        kpi_items.append(("地域シェア（許可病床数）", f"{data['region_share_pct']:.1f}%"))
    if data.get("region_rank") is not None:
        kpi_items.append(("地域内順位", f"{data['region_rank']} / {data['region_n']}院中"))
    story.append(_kpi_row(kpi_items, styles))
    story.append(Spacer(1, 3 * mm))
    story.append(_data_table(
        ["病床種別", "許可病床数", "平均在棟患者数/日", "病床稼働率", "構成比"],
        [[r["病床種別"], f"{r['許可病床数']:,}床", r["平均在棟患者数"], r["稼働率"], r["構成比"]]
         for r in beds["detail_rows"]],
        styles,
    ))

    # ── 医療専門職の人数 ──
    if data.get("staff"):
        story.append(_section_header("医療専門職の人数", styles))
        story.append(_source_note("病床機能報告", styles))
        story.append(_data_table(
            ["職種", "常勤", "非常勤"],
            [[lbl, f"{ft:,}人" if ft is not None else "―", f"{pt:,}人" if pt is not None else "―"]
             for lbl, ft, pt in data["staff"]],
            styles,
            col_widths=[None, 60 * mm, 60 * mm],
        ))

    # ── 診療実績（救急・手術・DPC）──
    story.append(_section_header("診療実績", styles))
    er = data.get("emergency")
    if er is not None:
        story.append(_source_note("病床機能報告", styles))
        items = [("救急搬送件数（年間）", f"{er['count']:,}件")]
        if er.get("region_share_pct") is not None:
            items.append(("二次医療圏シェア", f"{er['region_share_pct']:.1f}%"))
        story.append(_kpi_row(items, styles))
        story.append(Spacer(1, 2 * mm))

    surg = data.get("surgery")
    if surg is not None:
        story.append(_source_note(f"{data.get('nendo_label', '')}病床機能報告 様式2（手術）", styles))
        surg_items = [("手術総数（年間）", surg["total_disp"])]
        if surg.get("region_share_pct") is not None:
            surg_items.append(("二次医療圏内シェア", f"{surg['region_share_pct']:.1f}%"))
        story.append(_kpi_row(surg_items, styles))
        if surg.get("breakdown"):
            story.append(Spacer(1, 2 * mm))
            story.append(_data_table(
                ["内訳", "件数", "手術総数に占める割合"],
                surg["breakdown"],
                styles,
                col_widths=[None, 40 * mm, 60 * mm],
            ))
        story.append(Spacer(1, 2 * mm))

    dpc = data.get("dpc")
    if dpc is not None:
        story.append(_source_note(data.get("dpc_source", "DPC導入の影響評価に係る調査"), styles))
        dpc_items = [("DPC症例数（年間・全MDC合計）", f"{dpc['total_cases']:,}件")]
        if dpc.get("region_share_pct") is not None:
            dpc_items.append(("二次医療圏内シェア", f"{dpc['region_share_pct']:.1f}%"))
        story.append(_kpi_row(dpc_items, styles))
        if dpc.get("top_mdc"):
            story.append(Spacer(1, 2 * mm))
            story.append(_data_table(
                ["MDC（上位）", "症例数", "院内シェア"],
                dpc["top_mdc"],
                styles,
                col_widths=[None, 40 * mm, 60 * mm],
            ))
    elif data.get("dpc_unavailable_note"):
        story.append(Paragraph(data["dpc_unavailable_note"], styles["note"]))

    # ── 施設基準届出 ──
    story.append(_section_header("施設基準届出（診療報酬）", styles))
    sk = data.get("shisetsu")
    if sk:
        story.append(_source_note(sk.get("source", "診療報酬 施設基準届出情報"), styles))
        rows = [[item["受理届出名称"], item.get("区分", "") or "―"] for item in sk["items"]]
        story.append(_data_table(["受理届出名称", "区分"], rows, styles, col_widths=[None, 70 * mm]))
        if sk.get("truncated_count"):
            story.append(Paragraph(f"※ 他 {sk['truncated_count']} 件（紙面の都合上省略）", styles["note"]))
    else:
        story.append(Paragraph("届出情報が見つかりませんでした。", styles["note"]))

    # ── 外来機能報告 ──
    gairai = data.get("gairai")
    if gairai is not None:
        story.append(_section_header("外来（紹介・逆紹介の状況）", styles))
        story.append(_source_note(gairai.get("source", "外来機能報告"), styles))
        story.append(_data_table(
            ["初診患者数（年間）", "紹介患者数（年間）", "逆紹介患者数（年間）", "紹介率", "逆紹介率"],
            [[gairai["shoshin"], gairai["shokai"], gairai["gyakushokai"],
              gairai["shokai_rate"], gairai["gyakushokai_rate"]]],
            styles,
        ))

    story.append(Spacer(1, 6 * mm))
    story.append(Paragraph(
        f"MedilenZ（https://medilenz.jp）で生成　｜　"
        f"出典データの年度は各項目の表記に準拠し、数値の独自補正は行っていません。",
        styles["note"],
    ))

    doc.build(story)
    return buf.getvalue()


"""
`data` dictスキーマ（app.py側で組み立てる）:
{
    "hospital": str, "pref": str, "region": str, "year": int,
    "nendo_label": str,           # 例: "令和7年度"
    "address": str,               # 空文字可
    "emergency_designations": list[str] | None,  # 救急関連の受理届出名称（無ければキー省略可）
    "region_share_pct": float | None,
    "region_rank": int | None, "region_n": int,
    "beds": {
        "total_kyoka": int, "total_kado": int, "occ_pct": float | None,
        "detail_rows": [{"病床種別": str, "許可病床数": int,
                          "平均在棟患者数": str, "稼働率": str, "構成比": str}, ...],
    },
    "staff": [(label, fulltime_int_or_None, parttime_int_or_None), ...],
    "emergency": {"count": int, "region_share_pct": float | None} | None,
    "surgery": {
        "total_disp": str, "region_share_pct": float | None,
        "breakdown": [[label, count_str, pct_str], ...],
    } | None,
    "dpc": {
        "total_cases": int, "region_share_pct": float | None,
        "top_mdc": [[label, count_str, pct_str], ...],
    } | None,
    "dpc_source": str, "dpc_unavailable_note": str | None,
    "shisetsu": {"items": [{"受理届出名称": str, "区分": str}, ...],
                 "truncated_count": int, "source": str} | None,
    "gairai": {"shoshin": str, "shokai": str, "gyakushokai": str,
               "shokai_rate": str, "gyakushokai_rate": str, "source": str} | None,
}
"""
