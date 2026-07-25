#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gakkai_nintei_raw/ に学会ごとに置かれた認定施設一覧（PDF）を読み込んで
gakkai_nintei_cache.parquet を作る。

学会ごとに形式が異なる前提（PDF・HTMLいずれもある）。新しい学会を
追加する際は、まず該当ファイルの構造を確認してから _SOCIETY_CONFIG に
追記すること（既存の抽出関数が流用できるとは限らない）。対応済み形式:
  - 内科学会: 「都道府県／施設名」の2列表がページ単位で並ぶPDF形式
  - 内分泌学会: 「都道府県／施設名+診療科/郵便番号/住所」の4列表PDF
    （施設名と診療科がスペース区切りで1セルに結合されている。診療科名は
    必ず「科」で終わる1トークンという前提で、セルの最後の空白区切り
    トークンを診療科として分離する）
  - 眼科学会・基幹施設: 元PDFが画像PDF（テキスト層なし）でextract_text不可
    だったため、学会公式ページのHTML表（<table>部分のみ抜粋）を代わりに
    使用。「認定番号／施設名」の2列表で、都道府県は
    <td colspan="2">県名</td> という区切り行で表現される。
  - 整形外科学会・消化器外科学会: Excel（都道府県／施設名の列を持つシート）。
    区分は単一（全件同じカテゴリ）。
  - 神経学会: Excel。都道府県・施設名に加え「施設区分」列（教育施設／
    准教育施設／教育関連施設）が行ごとにあるため、抽出関数は
    (都道府県, 施設名, 区分) の3要素タプルを返す。
  - 産婦人科学会: Excel。**都道府県列が無い**（施設名・施設区分のみ）。
    この場合は pref=None として返し、都道府県を使わない全国完全一致
    （曖昧な場合はマッチさせない）でのみ突合する。

使い方:
    python build_gakkai_nintei.py
"""
import re
import unicodedata
from pathlib import Path

import pandas as pd
import pdfplumber
from bs4 import BeautifulSoup

BASE = Path(__file__).parent
RAW_DIR = BASE / "gakkai_nintei_raw"
OUT_PARQUET = BASE / "gakkai_nintei_cache.parquet"
DATA_CACHE = BASE / "data_cache.parquet"


_LEGAL_PREFIXES = [
    "独立行政法人国立病院機構", "国家公務員共済組合連合会", "地方独立行政法人",
    "社会医療法人財団", "社会医療法人", "国立大学法人", "公立大学法人",
    "医療法人社団", "医療法人財団", "公益財団法人", "一般財団法人",
    "公益社団法人", "一般社団法人", "社会福祉法人", "特定医療法人", "医療法人",
    "学校法人", "宗教法人",
]


def _normalize(name: str) -> str:
    """app.py の _normalize_hospital_for_match と同じロジック（法人格プレフィックス除去＋正規化）。"""
    if not isinstance(name, str):
        return ""
    name = unicodedata.normalize("NFKC", name).strip()
    for prefix in _LEGAL_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    return re.sub(r"[\s　]", "", name).lower()


def _extract_pref_facility_pdf(path: Path) -> list[tuple[str, str]]:
    """「都道府県／施設名」2列表のPDFから (都道府県, 施設名) のリストを抽出する。"""
    rows = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            if not tables:
                continue
            for r in tables[0]:
                if not r or len(r) < 2:
                    continue
                pref, name = r[0], r[1]
                if not pref or not name or pref.strip() == "都道府県":
                    continue
                rows.append((pref.strip(), name.strip()))
    return rows


def _normalize_pref_suffix(pref: str) -> str:
    """「青森」等、都道府県の末尾サフィックス（都/道/府/県）が省略された
    表記を病床機能報告データ側の正式名称に合わせる。"""
    pref = pref.strip()
    if pref in ("北海道", "東京都", "大阪府", "京都府") or pref.endswith(("都", "道", "府", "県")):
        return pref
    if pref == "東京":
        return "東京都"
    if pref in ("大阪", "京都"):
        return pref + "府"
    return pref + "県"


def _extract_pref_no_facility_pdf(path: Path) -> list[tuple[str, str]]:
    """「県名／研修施設番号／研修施設名称」3列表のPDFから (都道府県, 施設名)
    のリストを抽出する（眼科学会・一般研修施設形式。施設番号は使わない）。
    県名は「青森」のようにサフィックス省略形で記載されているため正規化する。
    """
    rows = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            if not tables:
                continue
            for r in tables[0]:
                if not r or len(r) < 3:
                    continue
                pref, name = r[0], r[2]
                if not pref or not name or pref.strip() in ("県名", "都道府県"):
                    continue
                rows.append((_normalize_pref_suffix(pref), name.strip()))
    return rows


def _extract_kikan_html(path: Path) -> list[tuple[str, str]]:
    """「認定番号／施設名」2列表のHTMLから (都道府県, 施設名) のリストを
    抽出する（眼科学会・基幹施設。都道府県は colspan=2 の区切り行）。"""
    with open(path, encoding="utf-8") as f:
        soup = BeautifulSoup(f.read(), "html.parser")
    current_pref = None
    rows = []
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) == 1 and tds[0].get("colspan") == "2":
            current_pref = tds[0].get_text(strip=True)
        elif len(tds) == 2:
            num, name = tds[0].get_text(strip=True), tds[1].get_text(strip=True)
            if num == "認定番号" or not current_pref or not name:
                continue
            rows.append((current_pref, name))
    return rows


def _extract_pref_dept_facility_pdf(path: Path) -> list[tuple[str, str]]:
    """「都道府県／施設名+診療科名（1セルに結合）／郵便番号／住所」の4列表
    から (都道府県, 施設名) のリストを抽出する（内分泌学会形式）。
    診療科名は必ず「科」で終わる末尾の1トークンという前提で、セルを
    空白で分割し最後のトークンを診療科として除いた残りを施設名とする。
    """
    rows = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            if not tables:
                continue
            for r in tables[0]:
                if not r or len(r) < 2:
                    continue
                pref, cell = r[0], r[1]
                if not pref or not cell or pref.strip() == "都道府県":
                    continue
                tokens = cell.split()
                if len(tokens) < 2:
                    continue  # 診療科が分離できない行はスキップ（誤爆防止）
                facility = " ".join(tokens[:-1]).strip()
                if facility:
                    rows.append((pref.strip(), facility))
    return rows


def _extract_orthopedic_excel(path: Path) -> list[tuple[str, str]]:
    """整形外科学会: 「全施設一覧」シートの都道府県／施設名列を使う
    （施設数(都道府県内)列は単なる集計値なので無視）。"""
    df = pd.read_excel(path, sheet_name="全施設一覧")
    rows = []
    for _, r in df.iterrows():
        pref, name = r.get("都道府県"), r.get("施設名")
        if pd.isna(pref) or pd.isna(name):
            continue
        rows.append((str(pref).strip(), str(name).strip()))
    return rows


def _extract_gi_surgery_excel(path: Path) -> list[tuple[str, str]]:
    """消化器外科学会: 「認定施設一覧」シートの都道府県／認定施設名列を使う。"""
    df = pd.read_excel(path, sheet_name="認定施設一覧")
    rows = []
    for _, r in df.iterrows():
        pref, name = r.get("都道府県"), r.get("認定施設名")
        if pd.isna(pref) or pd.isna(name):
            continue
        rows.append((str(pref).strip(), str(name).strip()))
    return rows


def _extract_neurology_excel(path: Path) -> list[tuple[str, str, str]]:
    """神経学会: 「全国一覧」シート。施設区分（教育施設／准教育施設／
    教育関連施設）が行ごとに異なるため (都道府県, 施設名, 区分) を返す。"""
    df = pd.read_excel(path, sheet_name="全国一覧")
    rows = []
    for _, r in df.iterrows():
        pref, name, cat = r.get("都道府県"), r.get("施設名"), r.get("施設区分")
        if pd.isna(pref) or pd.isna(name):
            continue
        rows.append((
            str(pref).strip(), str(name).strip(),
            str(cat).strip() if pd.notna(cat) else "認定施設",
        ))
    return rows


def _extract_obgyn_excel(path: Path) -> list[tuple[None, str, str]]:
    """産婦人科学会: 「専門研修施設一覧」シート。**都道府県列が無い**ため
    pref は常に None を返す（都道府県を使わない全国完全一致でのみ突合）。
    施設区分（基幹／連携）は行ごとに異なるため区分も返す。"""
    df = pd.read_excel(path, sheet_name="専門研修施設一覧")
    rows = []
    for _, r in df.iterrows():
        name, cat = r.get("研修施設名称"), r.get("施設区分")
        if pd.isna(name):
            continue
        rows.append((
            None, str(name).strip(),
            str(cat).strip() if pd.notna(cat) else "認定施設",
        ))
    return rows


_SOCIETY_CONFIG = {
    "内科学会": {
        "extractor": _extract_pref_facility_pdf,
        "files": [
            ("kikan_2026.pdf", "基幹施設"),
            ("renkei_2026.pdf", "連携施設"),
            ("t_renkei_2026.pdf", "特別連携施設"),
        ],
    },
    "内分泌学会": {
        "extractor": _extract_pref_dept_facility_pdf,
        "files": [
            ("shisetsu_1.pdf", "認定教育施設"),
            ("shisetsu_2.pdf", "認定教育施設"),
            ("shisetsu_3.pdf", "認定教育施設"),
            ("shisetsu_4.pdf", "認定教育施設"),
            ("shisetsu_5.pdf", "認定教育施設"),
        ],
    },
    "眼科学会": {
        "extractor": _extract_pref_no_facility_pdf,
        "files": [
            ("一般研修施設.pdf", "一般研修施設"),
            # 基幹研修施設.pdf は画像PDF（テキスト層なし）のため未対応。
            # 代わりに学会公式ページのHTML表（kikan_shisetsu.html）を使う
            # （extractorをこのファイルだけ上書き）。
            ("kikan_shisetsu.html", "基幹施設", _extract_kikan_html),
        ],
    },
    "整形外科学会": {
        "extractor": _extract_orthopedic_excel,
        "files": [
            ("整形外科学会認定施設.xlsx", "認定施設"),
        ],
    },
    "消化器外科学会": {
        "extractor": _extract_gi_surgery_excel,
        "files": [
            ("消化器外科学会認定施設一覧.xlsx", "認定施設"),
        ],
    },
    "神経学会": {
        "extractor": _extract_neurology_excel,
        "files": [
            # 区分（教育施設／准教育施設／教育関連施設）は行ごとにextractorが
            # 返すため、ここでの"認定施設"は使われないプレースホルダー。
            ("日本神経学会_認定施設_全国一覧.xlsx", "認定施設"),
        ],
    },
    "産婦人科学会": {
        "extractor": _extract_obgyn_excel,
        "files": [
            # 区分（基幹／連携）は行ごとにextractorが返すためプレースホルダー。
            # 都道府県列が無いデータのため全国完全一致でのみ突合される。
            ("産婦人科学会_専門医研修施設一覧.xlsx", "認定施設"),
        ],
    },
}


def _build_hospital_lookup():
    """(都道府県名, 正規化施設名) -> 医療機関コード の完全一致辞書、
    都道府県名 -> [(正規化施設名, 医療機関コード), ...] の一覧（サフィックス
    一致フォールバック用）、および 正規化施設名 -> {医療機関コード, ...} の
    全国版完全一致辞書（都道府県が分からないデータ用）を作る。医療機関名は
    年度によって表記が変わることがあるため、全年度分の名称をキーとして
    登録する。
    """
    df = pd.read_parquet(DATA_CACHE, columns=["都道府県名", "医療機関名", "医療機関コード"])
    df = df.drop_duplicates(subset=["都道府県名", "医療機関名"])
    exact = {}
    by_pref: dict[str, list] = {}
    national: dict[str, set] = {}
    for _, r in df.iterrows():
        norm = _normalize(r["医療機関名"])
        key = (r["都道府県名"], norm)
        exact[key] = r["医療機関コード"]
        by_pref.setdefault(r["都道府県名"], []).append((norm, r["医療機関コード"]))
        national.setdefault(norm, set()).add(r["医療機関コード"])
    return exact, by_pref, national


def _match(pref, norm_name: str, exact: dict, by_pref: dict, national: dict):
    """都道府県が分かる場合: 完全一致 → 見つからなければ同一都道府県内での
    サフィックス一致（法人名等の付加語が片方だけに付いているケースを吸収。
    施設基準届出の突合ロジックと同じ考え方）。
    都道府県が分からない場合（産婦人科学会等）: 全国での完全一致のみを
    試す。同名施設が複数都道府県にまたがって存在すると誤マッチの元になる
    ため、サフィックス一致は行わず、候補が一意に定まる場合のみ採用する。
    """
    if not norm_name:
        return None
    # pd.DataFrame化した後は None が NaN（float）になるため、`pref is None`
    # では判定できない（実際にこれで全件マッチ失敗する事故を起こした）。
    if pref is None or (isinstance(pref, float) and pd.isna(pref)):
        codes = national.get(norm_name)
        if codes and len(codes) == 1:
            return next(iter(codes))
        return None
    code = exact.get((pref, norm_name))
    if code is not None:
        return code
    # 短すぎる名称同士のサフィックス一致は誤マッチの元になるため対象外
    # （例:「中央病院」のような一般的すぎる語での偶然一致を防ぐ）
    if len(norm_name) < 4:
        return None
    for cand_norm, cand_code in by_pref.get(pref, []):
        if cand_norm and len(cand_norm) >= 4 and (cand_norm.endswith(norm_name) or norm_name.endswith(cand_norm)):
            return cand_code
    return None


def main():
    exact, by_pref, national = _build_hospital_lookup()
    print(f"病院名逆引き辞書: {len(exact):,} 件（全年度の表記ゆれを含む）")

    all_rows = []
    for society, config in _SOCIETY_CONFIG.items():
        society_dir = RAW_DIR / society
        default_extractor = config["extractor"]
        for file_entry in config["files"]:
            fname, category = file_entry[0], file_entry[1]
            extractor = file_entry[2] if len(file_entry) > 2 else default_extractor
            path = society_dir / fname
            if not path.exists():
                print(f"⚠ 見つかりません: {path}（スキップ）")
                continue
            rows = extractor(path)
            print(f"  {society}/{fname} ({category}): {len(rows)}件")
            for row in rows:
                # 抽出関数が (都道府県, 施設名) の2要素、または区分が行ごとに
                # 異なる場合は (都道府県, 施設名, 区分) の3要素を返す。
                if len(row) == 3:
                    pref, name, row_category = row
                else:
                    pref, name = row
                    row_category = category
                all_rows.append({
                    "学会名": society,
                    "区分": row_category,
                    "都道府県名": pref,
                    "施設名": name,
                })

    if not all_rows:
        print("データがありません。")
        return

    out = pd.DataFrame(all_rows)
    # 診療科ごとに複数行あるデータ（内分泌学会等）は、区分での絞り込みには
    # 診療科の粒度が不要なため、学会×区分×施設単位に圧縮する。
    before_dedup = len(out)
    out = out.drop_duplicates(subset=["学会名", "区分", "都道府県名", "施設名"])
    if before_dedup != len(out):
        print(f"施設単位に重複除去: {before_dedup:,}行 → {len(out):,}行")
    out["施設名_正規化"] = out["施設名"].apply(_normalize)
    out["医療機関コード"] = out.apply(
        lambda r: _match(r["都道府県名"], r["施設名_正規化"], exact, by_pref, national), axis=1
    )
    out["マッチ状態"] = out["医療機関コード"].apply(lambda c: "一致" if pd.notna(c) else "未一致")

    for col in ["学会名", "区分", "都道府県名", "マッチ状態"]:
        out[col] = out[col].astype("category")

    out.to_parquet(OUT_PARQUET, index=False)

    print(f"\n=== 完了 === {OUT_PARQUET} 合計 {len(out):,}行")
    print(out.groupby(["学会名", "区分"], observed=True).size())
    matched = (out["マッチ状態"] == "一致").sum()
    print(f"\n病床機能報告との突合: {matched:,} / {len(out):,} 件一致（{matched/len(out)*100:.1f}%）")


if __name__ == "__main__":
    main()
