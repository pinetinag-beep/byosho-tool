"""各地方厚生局のサイトから、施設基準の届出受理医療機関名簿（医科）を集める。

    python fetch_kouseikyoku.py            # ダウンロードして中身を判定し、一覧を表示するだけ
    python fetch_kouseikyoku.py --apply    # さらに shisetsu_raw/ ・ shisetsu_kubun_raw/ に入れる

集めるもの:
  - 全件の名簿（24列。県ごとの全届出）→ shisetsu_raw/
  - 届出項目別の名簿（19列。区分・病床数などの内訳あり）→ shisetsu_kubun_raw/
  - 北海道の新規・変更届出のPDF（北海道は届出項目別の名簿が無いため）→ shisetsu_kubun_raw/北海道/

厚生局はファイル名（番号）を毎月変え、ページの作りも局ごとに違う。そのため、決まったページにある
Excel/ZIPをまとめて取り、**中身で**見分ける（ファイル名やリンクの文字には頼らない）:
  - build_shisetsu_kijun.parse_file() で読めれば全件の名簿（医科の行だけを読む）
  - build_shisetsu_kijun._parse_kubun_file() で読めて種別が「医科」なら届出項目別の名簿
  - どちらでもない（歯科・薬局・訪問看護・保険外併用療養費など）は捨てる
--apply では、同じ県・同じ種類の古いファイル（時点が同じか古いもの）を消してから新しいファイルを入れる。
ダウンロードしたものは shisetsu_fetch/（git管理外）に残る。

ページの場所は2026年11月に調べたもの（PAGES）。厚生局がページを作り替えると取れなくなるので、
取れなかった局は最後に一覧で知らせる。その場合はブラウザで新しいページを探して PAGES を直す。
"""
from __future__ import annotations

import argparse
import html
import re
import shutil
import sys
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import pandas as pd

import build_shisetsu_kijun as bk

ROOT = Path(__file__).resolve().parent
BASE = "https://kouseikyoku.mhlw.go.jp"
STAGE = ROOT / "shisetsu_fetch"
RAW_DIR = ROOT / "shisetsu_raw"
KUBUN_DIR = bk.KUBUN_DIR

# 厚生局ごとの、名簿のExcel/ZIPが並んでいるページ
PAGES = {
    "北海道": ["/hokkaido/gyomu/gyomu/hoken_kikan/todokede_juri_ichiran.html"],
    "東北": ["/tohoku/gyomu/gyomu/hoken_kikan/documents/201805koushin.html"],
    "関東信越": ["/kantoshinetsu/chousa/kijyun.html"],
    "東海北陸": ["/tokaihokuriku/newpage_00349.html"],
    "近畿": ["/kinki/gyomu/gyomu/hoken_kikan/shitei_jokyo_00004.html"],
    "中国四国": ["/chugokushikoku/chousaka/shisetsukijunjuri.html",
                 "/chugokushikoku/chousaka/shisetsukijunjuri_00002.html"],
    "四国": ["/shikoku/gyomu/gyomu/hoken_kikan/shitei/index.html"],
    "九州": ["/kyushu/gyomu/gyomu/hoken_kikan/index_00007.html"],
}
# 北海道の新規・変更届出のページ（表の各行に 医科・歯科・薬局 のPDFが並ぶので、各行の最初＝医科を取る）
HOKKAIDO_SHINKI = "/hokkaido/iryo_shido/shisetsukijyun_jyuri_iryoukikan.html"

# 明らかに医科の施設基準の名簿ではないもの（ダウンロードを減らすため。最終的な判定は中身で行う）
SKIP_URL = re.compile(r"heiyo|houmon|houkan|hshitei|hkijun|shika|sika|yakkyoku|r\d+jisseki|chukan", re.I)
SKIP_TEXT = re.compile(r"歯科|薬局|訪問看護|併用|歯料")


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; MedilenZ data update)"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def _links(page_url: str) -> list[tuple[str, str]]:
    s = _get(page_url).decode("utf-8", "ignore")
    s = re.sub(r"<script.*?</script>", "", s, flags=re.S)
    out = []
    for m in re.finditer(r'<a [^>]*href="([^"]+)"[^>]*>(.*?)</a>', s, flags=re.S):
        text = html.unescape(re.sub(r"<[^>]+>|\s+", " ", m.group(2))).strip()
        out.append((urllib.parse.urljoin(page_url, m.group(1)), text))
    return out


def _download(url: str, dest_dir: Path) -> Path | None:
    dest = dest_dir / url.rsplit("/", 1)[1]
    if not dest.exists():
        try:
            dest.write_bytes(_get(url))
        except Exception as e:  # noqa: BLE001
            print(f"    ダウンロード失敗: {url}（{e}）")
            return None
    return dest


def _extract(zpath: Path, dest_dir: Path) -> list[Path]:
    """ZIPの中のExcelだけを取り出す（ファイル名だけを使い、フォルダ構成は無視する）。"""
    out = []
    try:
        with zipfile.ZipFile(zpath) as z:
            for info in z.infolist():
                name = info.filename
                if info.flag_bits & 0x800 == 0:   # UTF-8フラグが無い＝Shift_JISで入っている
                    try:
                        name = name.encode("cp437").decode("cp932")
                    except (UnicodeEncodeError, UnicodeDecodeError):
                        pass
                base = Path(name.replace("\\", "/")).name
                if not base.lower().endswith((".xlsx", ".xls")) or info.is_dir():
                    continue
                dest = dest_dir / f"{zpath.stem}__{base}"
                if not dest.exists():
                    dest.write_bytes(z.read(info))
                out.append(dest)
    except zipfile.BadZipFile:
        print(f"    ZIPを開けない: {zpath.name}")
    return out


def _peek(path: Path) -> tuple[str, str]:
    """先頭の数行だけ読んで (時点, 種別) を返す。種別は「医科」「歯科」「薬局」か、書いていなければ ""。
    中身を全部読む _classify は重いので、その前にこれで最新の医科の名簿だけに絞る。"""
    try:
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True)
        ws = wb.worksheets[0]
        text = " ".join(str(v) for row in ws.iter_rows(max_row=8, values_only=True) for v in row if v)
        wb.close()
    except Exception:  # noqa: BLE001
        return "", ""
    kind = next((k for k in ("医科", "歯科", "薬局") if k in text), "")
    return bk._wareki_date(text), kind


def _is_full_roster(path: Path) -> bool:
    """全件の名簿（24列）は見出しに「区分」（医科・歯科の別）と「市町村コード」の列がある。
    届出項目別の名簿（19列）には無い。どちらも「受理届出名称」「備考（見出し）」の列はあるので、
    そこでは見分けられない（実際に全件の名簿を届出項目別と取り違えた）。"""
    try:
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True)
        rows = [set(str(v).strip() for v in r if v) for r in wb.worksheets[0].iter_rows(max_row=8, values_only=True)]
        wb.close()
    except Exception:  # noqa: BLE001
        return False
    return any({"受理届出名称", "区分", "市町村コード"} <= r for r in rows)


def _classify(path: Path) -> dict | None:
    """全件の名簿 / 届出項目別の名簿（医科）/ それ以外 を中身で見分ける。"""
    if _is_full_roster(path):
        try:
            df, _ym, snap = bk.parse_file(str(path))
        except Exception:  # noqa: BLE001
            return None
        if df.empty:   # 医科の行が無い（歯科・薬局の全件の名簿）
            return None
        return {"type": "full", "path": path, "snap": snap,
                "prefs": sorted(df["都道府県名"].unique()), "group": "全件", "n": len(df)}
    try:
        sup, snap, kind = bk._parse_kubun_file(path)
    except Exception:  # noqa: BLE001
        sup, snap, kind = pd.DataFrame(), "", ""
    if not sup.empty:
        if kind != "医科":
            return None
        return {"type": "kubun", "path": path, "snap": snap,
                "prefs": sorted(sup["都道府県名"].unique()), "group": sup["受理届出名称"].iloc[0], "n": len(sup)}
    return None


def fetch() -> tuple[list[dict], list[Path], list[str]]:
    STAGE.mkdir(exist_ok=True)
    found: list[dict] = []
    failed: list[str] = []
    for kyoku, pages in PAGES.items():
        d = STAGE / kyoku
        d.mkdir(exist_ok=True)
        print(f"■ {kyoku}")
        files: list[Path] = []
        for p in pages:
            try:
                links = _links(BASE + p)
            except Exception as e:  # noqa: BLE001
                print(f"    ページを開けない: {p}（{e}）")
                continue
            for url, text in links:
                if not re.search(r"\.(xlsx?|zip)$", url, re.I) or not url.startswith(BASE):
                    continue
                if SKIP_URL.search(url.rsplit("/", 1)[1]) or SKIP_TEXT.search(text):
                    continue
                f = _download(url, d)
                if f is None:
                    continue
                files += _extract(f, d) if f.suffix.lower() == ".zip" else [f]
        # 九州などは過去の月の名簿も同じページに並んでいるので、医科以外と、最新の時点でないものは読まない
        peeked = {f: _peek(f) for f in dict.fromkeys(files)}
        peeked = {f: v for f, v in peeked.items() if v[1] in ("", "医科")}
        newest = max((v[0] for v in peeked.values()), default="")
        old = [f for f, v in peeked.items() if v[0] != newest]
        if old:
            print(f"    最新（{newest}）でない名簿{len(old)}件は使わない（過去の掲載分）")
        got = [c for c in (_classify(f) for f, v in peeked.items() if v[0] == newest) if c]
        for c in got:
            print(f"    {c['type']:5} {c['snap']:10} {'・'.join(c['prefs'])[:30]:30} {c['group'][:16]:16} {c['n']:>7,}件  {c['path'].name}")
        if not any(c["type"] == "full" for c in got):
            failed.append(kyoku)
        found += got

    # 北海道の新規・変更届出のPDF（医科）
    hk = STAGE / "北海道_新規変更"
    hk.mkdir(exist_ok=True)
    pdfs: list[Path] = []
    try:
        s = _get(BASE + HOKKAIDO_SHINKI).decode("utf-8", "ignore")
        for tr in re.findall(r"<tr.*?</tr>", s, flags=re.S):
            links = re.findall(r'href="([^"]+\.pdf)"', tr)
            if len(links) >= 2:   # 医科・歯科・薬局が並ぶ行。最初が医科
                f = _download(urllib.parse.urljoin(BASE + HOKKAIDO_SHINKI, links[0]), hk)
                if f:
                    pdfs.append(f)
        print(f"■ 北海道の新規・変更PDF（医科）: {len(pdfs)}件")
    except Exception as e:  # noqa: BLE001
        print(f"■ 北海道の新規・変更PDF: ページを開けない（{e}）")
        failed.append("北海道（新規・変更PDF）")
    return found, pdfs, failed


def _existing(dir_: Path, type_: str) -> list[dict]:
    out = []
    for f in sorted(dir_.glob("*.xlsx")):
        c = _classify(f)
        if c and c["type"] == type_:
            out.append(c)
    return out


def apply(found: list[dict], pdfs: list[Path]) -> None:
    """新しいファイルを入れ、同じ県・同じ種類で時点が同じか古いファイルを消す。"""
    for type_, dir_ in (("full", RAW_DIR), ("kubun", KUBUN_DIR)):
        new = [c for c in found if c["type"] == type_]
        if not new:
            continue
        # 同じ県・同じ種類の中で、新しく取ったものの最新
        latest: dict[tuple[str, str], str] = {}
        for c in new:
            for p in c["prefs"]:
                k = (p, c["group"])
                latest[k] = max(latest.get(k, ""), c["snap"])
        for c in _existing(dir_, type_):
            if all(latest.get((p, c["group"]), "") >= c["snap"] for p in c["prefs"]):
                print(f"  削除（新しい名簿に置き換え）: {dir_.name}/{c['path'].name}")
                c["path"].unlink()
        for c in new:
            # 取ったものの中でも、同じ県・種類でより新しいものがあれば入れない
            if any(latest[(p, c["group"])] > c["snap"] for p in c["prefs"]):
                continue
            dest = dir_ / c["path"].name.split("__")[-1]
            if dest.exists():
                dest = dir_ / c["path"].name
            shutil.copy2(c["path"], dest)
            print(f"  追加: {dir_.name}/{dest.name}（{c['snap']}）")
    hk_dir = KUBUN_DIR / "北海道"
    hk_dir.mkdir(exist_ok=True)
    for f in pdfs:
        if not (hk_dir / f.name).exists():
            shutil.copy2(f, hk_dir / f.name)
            print(f"  追加: {hk_dir.relative_to(ROOT)}/{f.name}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="shisetsu_raw/ ・ shisetsu_kubun_raw/ に入れる")
    args = ap.parse_args()
    found, pdfs, failed = fetch()
    full_prefs = {p for c in found if c["type"] == "full" for p in c["prefs"]}
    missing = [p for p in bk_pref_names() if p not in full_prefs]
    print(f"\n全件の名簿: {len(full_prefs)}都道府県" + (f"（取れなかった: {'・'.join(missing)}）" if missing else ""))
    if failed:
        print(f"全件の名簿が見つからなかった厚生局: {'・'.join(failed)} → PAGES を確認すること")
    if args.apply:
        apply(found, pdfs)
    return 0


def bk_pref_names() -> list[str]:
    from parse_shisetsu_shinki_pdf import PREF_NAME_TO_CODE
    return list(PREF_NAME_TO_CODE)


if __name__ == "__main__":
    sys.exit(main())
