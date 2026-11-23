"""医療情報ネット（ナビイ）のオープンデータから、診療科・診療時間・休診日・ホームページを取り込む。

厚生労働省「医療情報ネットのオープンデータ」
  https://www.mhlw.go.jp/stf/seisakunitsuite/bunya/kenkou_iryou/iryou/newpage_43373.html
を、施設基準の名簿の医療機関（医科・歯科・薬局）と名前・住所で突き合わせ、navi_cache.parquet に書く。
公表は年2回（6月1日・12月1日時点）。営利・非営利を問わず二次利用できる（同ページの利用規約）。

    python build_navi.py              # 最新の時点をダウンロードして navi_{YYYYMMDD}/ に置き、取り込む
    python build_navi.py --dir navi_20260601   # ダウンロード済みのフォルダから取り込む

医療情報ネットのIDは医療機関番号と別の体系なので、
  1. 都道府県＋名前（法人名を外して正規化）が両方の側で1件ずつのもの
  2. 1で残ったうち、都道府県＋住所（建物名を外して正規化）が両方で1件ずつで、名前も似ているもの
を同じ医療機関とみなす（1対1）。
休診日は医療情報ネットの「休診」フラグ（意味が項目によって逆になっている）は使わず、
曜日ごとの診療時間が空いているかで判断する。
"""
from __future__ import annotations

import argparse
import difflib
import functools
import io
import json
import re
import sys
import unicodedata
import urllib.request
import zipfile
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "navi_cache.parquet"
PAGE = "https://www.mhlw.go.jp/stf/seisakunitsuite/bunya/kenkou_iryou/iryou/newpage_43373.html"
BASE = "https://www.mhlw.go.jp"
DAYS = ["月", "火", "水", "木", "金", "土", "日", "祝"]

# (種類, 施設票, 診療科・診療時間票, 施設基準の名簿, IDの3桁目)
KINDS = [
    ("医科", ["01-1_hospital_facility_info", "02-1_clinic_facility_info"],
     ["01-2_hospital_speciality_hours", "02-2_clinic_speciality_hours"], "shisetsu_kijun_cache.parquet", ""),
    ("歯科", ["03-1_dental_facility_info"], ["03-2_dental_speciality_hours"], "shika_cache.parquet", "3"),
    ("薬局", ["05_pharmacy"], [], "yakkyoku_cache.parquet", "4"),
]
FILES = [f for _, a, b, _, _ in KINDS for f in a + b]

_CORP = ("法人", "株式会社", "有限会社", "合同会社", "合資会社")
_CORP_RE = re.compile(r"^(?:(?:医療|社会医療|特定医療|一般社団|一般財団|公益社団|公益財団|社会福祉|地方独立行政|独立行政"
                      r"|国立大学|公立大学|学校|宗教|社団|財団)法人(?:社団|財団)?|医療法人(?:社団|財団)?|株式会社|有限会社|合同会社|合資会社)")
_PREF_RE = re.compile(r"^(北海道|東京都|大阪府|京都府|.{2,3}県)")


# ---------- ダウンロード ----------

def fetch(dest_root: Path = ROOT) -> Path:
    html = urllib.request.urlopen(PAGE, timeout=60).read().decode("utf-8")
    dates = sorted(set(re.findall(r"_(\d{8})(?:\.csv)?\.zip", html)), reverse=True)
    if not dates:
        sys.exit("医療情報ネットのオープンデータのページにZIPが見つからない")
    day = dates[0]
    d = dest_root / f"navi_{day}"
    d.mkdir(exist_ok=True)
    for f in FILES:
        if (d / f"{f}_{day}.csv").exists():
            continue
        href = re.search(rf'href="([^"]*{re.escape(f)}_{day}(?:\.csv)?\.zip)"', html)
        if not href:
            sys.exit(f"{f} のZIPが見つからない（{day}）")
        url = href.group(1) if href.group(1).startswith("http") else BASE + href.group(1)
        print("download", url, flush=True)
        z = zipfile.ZipFile(io.BytesIO(urllib.request.urlopen(url, timeout=600).read()))
        for n in z.namelist():
            if n.lower().endswith(".csv"):
                (d / f"{f}_{day}.csv").write_bytes(z.read(n))
    return d


def _csv(d: Path, stem: str) -> pd.DataFrame:
    p = next(d.glob(f"{stem}_*.csv"))
    for enc in ("utf-8-sig", "cp932"):
        try:
            return pd.read_csv(p, dtype=str, encoding=enc, keep_default_na=False)
        except UnicodeDecodeError:
            continue
    raise SystemExit(f"{p} を読めない")


# ---------- 突き合わせ ----------

_ABBR = {"(株)": "株式会社", "(有)": "有限会社", "(医)": "医療法人", "(医社)": "医療法人社団", "(医財)": "医療法人財団",
         "(社医)": "社会医療法人", "(公財)": "公益財団法人", "(一財)": "一般財団法人", "(公社)": "公益社団法人",
         "(一社)": "一般社団法人", "(社福)": "社会福祉法人", "(合)": "合同会社"}


def norm_name(s: str) -> str:
    """法人名・括弧書き・空白・記号を外した名前（突き合わせ用）。"""
    s = unicodedata.normalize("NFKC", s or "")
    for k, v in _ABBR.items():
        s = s.replace(k, v + " ")
    s = re.sub(r"\([^)]*\)", "", s)                     # 括弧書き（診療科・呼称など）
    toks = [t for t in re.split(r"\s+", s) if t]
    if len(toks) > 1:
        rest = _CORP_RE.sub("", toks[0])
        if rest != toks[0]:                              # 頭が法人格
            if (rest == "" or rest.endswith("会") or rest.endswith("財団")) and len("".join(toks[1:])) >= 4:
                toks.pop(0)
                if (len(toks) > 1 and rest == "" and (toks[0].endswith("会") or toks[0].endswith("財団"))
                        and len("".join(toks[1:])) >= 4):
                    toks.pop(0)                          # 「医療法人社団　〇〇会　××病院」の〇〇会
            else:
                toks[0] = rest
    s = "".join(toks)
    s = _CORP_RE.sub("", s)
    s = re.sub(r"(株式会社|有限会社|合同会社|合資会社)", "", s)
    s = re.sub(r"[・･\.\-ー‐－「」『』、,]", "", s)
    return s


_KNUM = {c: i for i, c in enumerate("〇一二三四五六七八九")}


def _kanji_num(m: re.Match) -> str:
    t = m.group(1)
    if "十" in t:
        a, _, b = t.partition("十")
        n = (_KNUM.get(a, 1) if a else 1) * 10 + (_KNUM.get(b, 0) if b else 0)
    else:
        n = int("".join(str(_KNUM[c]) for c in t))
    return str(n) + m.group(2)


def norm_addr(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").strip()
    s = re.sub(r"([〇一二三四五六七八九十]+)(丁目|番|号|条|線|地割)", _kanji_num, s)
    s = re.sub(r"\s+", "#", s)                 # 空白は区切りとして残す（番地と建物の階がつながらないように）
    s = _PREF_RE.sub("", s)
    s = re.sub(r"(大字|字)", "", s)
    s = re.sub(r"[‐‑‒–—―−ーｰ－]", "-", s)
    s = re.sub(r"(丁目|番地の|番地|番|号|の)(?=\d)", "-", s)
    s = re.sub(r"(丁目|番地|番|号)", "-", s)
    s = re.sub(r"-+", "-", s)
    m = re.match(r"^([^0-9]*?[0-9]+(?:[条線][0-9]+|地割[0-9]+)*(?:-[0-9]+)*)", s)   # 番地の数字までで切る（建物名・階を外す）
    if m:
        s = m.group(1)
    return s.replace("#", "").strip("-")


def town(addr: str) -> str:
    """正規化した住所の、最初の数字より前（市区町村＋町名）。"""
    m = re.match(r"^([^0-9]+)", addr or "")
    return m.group(1) if m else ""


def _unique(keys: pd.Series) -> pd.Series:
    return ~keys.duplicated(keep=False) & keys.ne("") & keys.notna()


def match(fac: pd.DataFrame, nav: pd.DataFrame) -> pd.DataFrame:
    """fac(facility_id, pref, name, address) と nav(navi_id, pref, name, address) を1対1で突き合わせる。"""
    fac = fac.assign(nk=fac["pref"] + "|" + fac["name"].map(norm_name),
                     ak=fac["pref"] + "|" + fac["address"].map(norm_addr),
                     nn=fac["name"].map(norm_name))
    nav = nav.assign(nk=nav["pref"] + "|" + nav["name"].map(norm_name),
                     ak=nav["pref"] + "|" + nav["address"].map(norm_addr),
                     nn=nav["name"].map(norm_name))
    f1 = fac[_unique(fac["nk"])]
    n1 = nav[_unique(nav["nk"])]
    m1 = f1[["facility_id", "nk"]].merge(n1[["navi_id", "nk"]], on="nk")[["facility_id", "navi_id"]]
    m1["how"] = "名前"
    fr = fac[~fac["facility_id"].isin(m1["facility_id"])]
    nr = nav[~nav["navi_id"].isin(m1["navi_id"])]
    fr, nr = fr[_unique(fr["ak"])], nr[_unique(nr["ak"])]
    m2 = fr[["facility_id", "ak", "nn"]].merge(nr[["navi_id", "ak", "nn"]], on="ak", suffixes=("", "_n"))
    ok = [difflib.SequenceMatcher(None, a, b).ratio() >= 0.5 or a in b or b in a
          for a, b in zip(m2["nn"], m2["nn_n"])]
    m2 = m2[ok][["facility_id", "navi_id"]].assign(how="住所")
    # 3. 同じ県に同じ名前が複数ある時は、名前＋町名（住所の最初の数字より前）が両方で1件ずつのもの
    done_f = set(m1["facility_id"]) | set(m2["facility_id"])
    done_n = set(m1["navi_id"]) | set(m2["navi_id"])
    fr = fac[~fac["facility_id"].isin(done_f)]
    nr = nav[~nav["navi_id"].isin(done_n)]
    fr = fr.assign(tk=fr["nk"] + "|" + fr["ak"].map(lambda a: town(a.split("|", 1)[1])))
    nr = nr.assign(tk=nr["nk"] + "|" + nr["ak"].map(lambda a: town(a.split("|", 1)[1])))
    fr = fr[_unique(fr["tk"]) & fr["tk"].str.split("|").str[-1].str.len().ge(2)]
    nr = nr[_unique(nr["tk"])]
    m3 = fr[["facility_id", "tk"]].merge(nr[["navi_id", "tk"]], on="tk")[["facility_id", "navi_id"]].assign(how="名前と町名")
    return pd.concat([m1, m2, m3], ignore_index=True)


# ---------- 診療時間 ----------

@functools.lru_cache(maxsize=None)
def _t(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").strip()
    m = re.match(r"^(\d{1,2}):(\d{2})", s)
    return f"{int(m.group(1))}:{m.group(2)}" if m else ""


def _cell(a: str, b: str) -> str:
    a, b = _t(a), _t(b)
    if not a or not b or a == b:
        return ""
    return f"{a}-{b}"


def _cells(df: pd.DataFrame, cols: list[tuple[str, str]]) -> pd.Series:
    """各行の「月..祝」の時間を「9:00-12:00,,…」の文字列にする（全部空なら空文字）。"""
    parts = []
    for a, b in cols:
        if a not in df.columns:
            parts.append(pd.Series("", index=df.index))
            continue
        x = df[a].fillna("").map(_t)
        y = df[b].fillna("").map(_t)
        parts.append(((x + "-" + y).where((x != "") & (y != "") & (x != y), "")))
    joined = parts[0]
    for p in parts[1:]:
        joined = joined + "," + p
    return joined.where(joined.str.replace(",", "", regex=False) != "", "")


def _from_rows(rows: str) -> list[list[str]]:
    return [r.split(",") for r in rows.split(";") if r]


def schedules(hours: pd.DataFrame) -> dict[str, dict]:
    """navi_id → {"specs": [...], "groups": [{"s": [診療科], "r": [[月..祝]...], "u": [[受付]...]}]}。
    時間が同じ診療科はまとめる。"""
    hours = hours.rename(columns=lambda c: c.strip())
    h = pd.DataFrame({
        "id": hours["ID"],
        "code": hours["診療科目コード"],
        "slot": pd.to_numeric(hours["診療時間帯"], errors="coerce").fillna(0),
        "spec": hours["診療科目名"].map(lambda v: unicodedata.normalize("NFKC", v or "").strip()),
        "r": _cells(hours, [(f"{d}_診療開始時間", f"{d}_診療終了時間") for d in DAYS]),
        "u": _cells(hours, [(f"{d}_外来受付開始時間", f"{d}_外来受付終了時間") for d in DAYS]),
    })
    h = h[(h["spec"] != "") & ~h["spec"].isin(["なし", "その他"])]
    h = h.sort_values(["id", "code", "slot"], kind="stable")
    order = h.drop_duplicates(["id", "spec"])[["id", "spec"]]
    order["o"] = range(len(order))
    j = lambda s: ";".join(x for x in s if x)
    ps = h.groupby(["id", "spec"], sort=False).agg(r=("r", j), u=("u", j)).reset_index()
    ps = ps.merge(order, on=["id", "spec"]).sort_values("o")
    specs = ps.groupby("id", sort=False)["spec"].agg(list)
    timed = ps[(ps["r"] != "") | (ps["u"] != "")]
    grp = timed.groupby(["id", "r", "u"], sort=False).agg(s=("spec", list), o=("o", "min")).reset_index().sort_values("o")
    groups: dict[str, list] = {}
    for nid, r, u, s_, _ in grp.itertuples(index=False):
        groups.setdefault(nid, []).append({"s": s_, "r": _from_rows(r), "u": _from_rows(u)})
    return {nid: {"specs": sp, "groups": groups.get(nid, [])} for nid, sp in specs.items()}


def pharmacy_schedules(ph: pd.DataFrame) -> dict[str, dict]:
    rows = [_cells(ph, [(f"{d}_開店時間帯{k}_開始時間", f"{d}_開店時間帯{k}_終了時間") for d in DAYS]) for k in range(1, 5)]
    allr = rows[0]
    for r in rows[1:]:
        allr = allr + ";" + r
    out = {}
    for nid, r in zip(ph["ID"], allr):
        rr = _from_rows(r)
        out[nid] = {"specs": [], "groups": [{"s": [], "r": rr, "u": []}] if rr else []}
    return out


def closed_days(sched: dict) -> list[str]:
    """どの診療科・時間帯にも時間が入っていない曜日（＝休診）。時間が1つも無ければ判断しない。"""
    open_ = [False] * len(DAYS)
    anyt = False
    for g in sched.get("groups", []):
        for row in g["r"] + g["u"]:
            for i, c in enumerate(row):
                if c:
                    open_[i] = True
                    anyt = True
    return [d for d, o in zip(DAYS, open_) if not o] if anyt else []


# ---------- 本体 ----------

def _facilities(path: Path, digit: str) -> pd.DataFrame:
    df = pd.read_parquet(ROOT / path, columns=["都道府県コード", "医療機関番号", "医療機関名称", "住所"])
    df = df.astype(str).drop_duplicates(["都道府県コード", "医療機関番号"])
    pref = df["都道府県コード"].str.zfill(2)
    code = df["医療機関番号"].str.zfill(7)
    return pd.DataFrame({"facility_id": pref + digit + code, "pref": pref,
                         "name": df["医療機関名称"], "address": df["住所"]})


def build(d: Path) -> pd.DataFrame:
    as_of = re.search(r"(\d{8})", d.name)
    as_of = f"{as_of.group(1)[:4]}-{as_of.group(1)[4:6]}-{as_of.group(1)[6:]}" if as_of else ""
    frames = []
    for kind, fstems, hstems, src, digit in KINDS:
        info = pd.concat([_csv(d, s) for s in fstems], ignore_index=True)
        name_col = "正式名称" if "正式名称" in info.columns else "名称"
        url_col = next(c for c in info.columns if "ホームページ" in c)
        other_col = next((c for c in info.columns if c.startswith("その他の休診日") or c.startswith("その他の閉店日")), None)
        nav = pd.DataFrame({"navi_id": info["ID"], "pref": info["都道府県コード"].str.zfill(2),
                            "name": info[name_col], "address": info["所在地"]})
        fac = _facilities(src, digit)
        m = match(fac, nav)
        if hstems:
            sched = schedules(pd.concat([_csv(d, s) for s in hstems], ignore_index=True))
        else:
            sched = pharmacy_schedules(info)
        meta = info.set_index("ID")
        rows = []
        for fid, nid, how in m.itertuples(index=False):
            s = sched.get(nid, {"specs": [], "groups": []})
            url = unicodedata.normalize("NFKC", meta.at[nid, url_col] or "").strip()
            url = re.sub(r"^(https?)[:;]?/+", r"\1://", url, flags=re.I)
            if url and not re.match(r"^https?://", url, flags=re.I):
                url = "http://" + url if re.match(r"^[\w.-]+\.[a-z]{2,}(/|$)", url, flags=re.I) else ""
            if " " in url or "." not in url:
                url = ""
            other = (meta.at[nid, other_col] if other_col else "") or ""
            rows.append({"kind": kind, "facility_id": fid, "navi_id": nid, "how": how,
                         "navi_name": meta.at[nid, name_col], "url": url,
                         "specs": "｜".join(s["specs"]),
                         "sched": json.dumps(s["groups"], ensure_ascii=False, separators=(",", ":")) if s["groups"] else "",
                         "closed": "・".join(closed_days(s)),
                         "other_holiday": re.sub(r"[、,\s]*\(?改行\)?[、,\s]*", "、", unicodedata.normalize("NFKC", other)).strip("、, ")[:200],
                         "as_of": as_of})
        df = pd.DataFrame(rows)
        print(f"{kind}: 施設基準の名簿 {len(fac):,} 件のうち {len(df):,} 件（{len(df) / len(fac):.1%}）がつながった"
              "（" + "・".join(f"{k} {v:,}" for k, v in df["how"].value_counts().items()) + "）。"
              f"医療情報ネット {len(nav):,} 件", flush=True)
        frames.append(df)
    out = pd.concat(frames, ignore_index=True)
    for c in ("kind", "how", "closed", "as_of"):
        out[c] = out[c].astype("category")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", type=Path, help="ダウンロード済みのフォルダ（navi_YYYYMMDD）")
    ap.add_argument("--out", type=Path, default=OUT)
    a = ap.parse_args()
    d = a.dir or fetch()
    out = build(d)
    out.to_parquet(a.out, index=False, compression="zstd")
    print(f"{a.out.name}: {len(out):,} 行・{a.out.stat().st_size / 1e6:.1f}MB", flush=True)


if __name__ == "__main__":
    main()
