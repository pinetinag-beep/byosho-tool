"""同じIPアドレスからのアクセスを数える（会社などで使われているかを知るため）。

- ページ（HTML）を開いたアクセスだけを、日×IPごとに数える。ボット・巡回プログラムは数えない。
- 同じ日に同じIPから別の端末（ブラウザの種類の違い）で開かれていれば「複数の端末」として数える。
  ブラウザの種類（User-Agent）はそのまま残さず、ハッシュにして数えるだけにする。
- 記録は 60 日で消す（プライバシーポリシーに記載）。
- 集計ページ（/admin/visits）は管理用の鍵が無いと開けない。IPは末尾を伏せて表示し、
  逆引き（PTR）で分かる組織のドメインと、その種類（企業・団体／行政／大学など／個人回線）でまとめる。

DB は検索用の kijun.sqlite（毎月作り直す・読み取り専用）とは別のファイル（既定 kijun_web/data/visits.sqlite）。
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import ipaddress
import os
import re
import socket
import sqlite3
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("KIJUN_VISITS_DB", HERE / "data" / "visits.sqlite"))
KEEP_DAYS = 60

# ボット・巡回プログラム・確認用のツールは数えない
_BOT = re.compile(r"bot|crawl|spider|slurp|facebookexternalhit|bingpreview|mediapartners|adsbot|google-|"
                  r"python|curl|wget|httpx|headless|lighthouse|pagespeed|preview|monitor|uptime|scan|"
                  r"semrush|ahrefs|petal|bytespider|gptbot|claude|perplexity|ccbot|dataforseo", re.I)

# 逆引きの名前から分かる、個人向けの回線（固定回線・携帯）。ここに当たれば会社の回線とはみなさない
_ISP = re.compile(r"(ocn|plala|so-net|biglobe|nifty|dion|au-net|kddi|ppp|flets|bbtec|softbank|"
                  r"nuro|eonet|commufa|jcom|zaq|tnc|itscom|asahi-net|dti|mesh|v6|ipv6|ap\.|dynamic|"
                  r"docomo|spmode|mopera|rakuten|mineo|uqwimax|wimax|ymobile|povo|ahamo|"
                  r"amazonaws|googleusercontent|cloud|azure|vultr|linode|digitalocean|sakura\.ne|"
                  r"xserver|conoha|gmo|fiber|catv|cable|home|user|client|pool|dsl|ftth)", re.I)

_lock = threading.Lock()
_ready = False
_last_purge = 0.0


def _con() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=5, check_same_thread=False)
    con.execute("PRAGMA busy_timeout = 5000")
    return con


def _init() -> None:
    global _ready
    if _ready:
        return
    with _lock:
        if _ready:
            return
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        con = _con()
        con.executescript("""
            PRAGMA journal_mode = WAL;
            CREATE TABLE IF NOT EXISTS hits (day TEXT, ip TEXT, ua TEXT, n INTEGER, PRIMARY KEY (day, ip, ua));
            CREATE TABLE IF NOT EXISTS ptr (ip TEXT PRIMARY KEY, host TEXT, checked REAL);
        """)
        con.close()
        _ready = True


def is_bot(ua: str) -> bool:
    return not ua or bool(_BOT.search(ua))


def record(ip: str | None, ua: str) -> None:
    """ページを開いたアクセスを1件数える（失敗してもページの表示は止めない）。"""
    global _last_purge
    if not ip or is_bot(ua):
        return
    try:
        _init()
        day = time.strftime("%Y-%m-%d")
        uah = hashlib.sha256(ua.encode("utf-8", "ignore")).hexdigest()[:12]
        con = _con()
        con.execute("INSERT INTO hits VALUES (?,?,?,1) ON CONFLICT(day, ip, ua) DO UPDATE SET n = n + 1",
                    (day, ip, uah))
        if time.time() - _last_purge > 3600:
            limit = time.strftime("%Y-%m-%d", time.localtime(time.time() - KEEP_DAYS * 86400))
            con.execute("DELETE FROM hits WHERE day < ?", (limit,))
            con.execute("DELETE FROM ptr WHERE ip NOT IN (SELECT DISTINCT ip FROM hits)")
            _last_purge = time.time()
        con.commit()
        con.close()
    except Exception:
        pass


def mask(ip: str) -> str:
    """表示用にIPの末尾を伏せる（203.0.113.x、IPv6は前半4つだけ）。"""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return "?"
    if a.version == 4:
        return ".".join(ip.split(".")[:3]) + ".x"
    return ":".join(a.exploded.split(":")[:4]) + "::"


def org_domain(host: str) -> str:
    """逆引きの名前から組織のドメインらしい部分（example.co.jp・example.com）を取り出す。"""
    if not host:
        return ""
    p = host.lower().rstrip(".").split(".")
    if len(p) >= 3 and p[-1] == "jp" and p[-2] in {"co", "or", "ne", "ac", "go", "lg", "ed", "gr", "ad"}:
        return ".".join(p[-3:])
    return ".".join(p[-2:])


def kind(host: str) -> str:
    """逆引きの名前から回線の種類を推定する（逆引きが無いものは「不明」）。"""
    if not host:
        return "不明（逆引きなし）"
    h = host.lower()
    if h.endswith(".go.jp") or h.endswith(".lg.jp") or ".pref." in h or ".city." in h:
        return "行政"
    if h.endswith(".ac.jp") or h.endswith(".ed.jp") or h.endswith(".edu"):
        return "大学・研究・学校"
    if h.endswith(".ne.jp") or _ISP.search(h):
        return "個人向け回線・携帯"
    return "企業・団体"


def _lookup(ips: list[str], budget: float = 8.0) -> None:
    """まだ逆引きしていないIPを並列で引いて保存する（全体で budget 秒まで）。"""
    con = _con()
    done = {r[0] for r in con.execute(f"SELECT ip FROM ptr WHERE ip IN ({','.join('?' * len(ips))})", ips)} if ips else set()
    todo = [ip for ip in ips if ip not in done]
    if not todo:
        con.close()
        return

    def one(ip):
        try:
            return ip, socket.gethostbyaddr(ip)[0]
        except Exception:
            return ip, ""

    ex = concurrent.futures.ThreadPoolExecutor(max_workers=16)
    futs = [ex.submit(one, ip) for ip in todo]
    try:
        for f in concurrent.futures.as_completed(futs, timeout=budget):
            ip, host = f.result()
            con.execute("INSERT OR REPLACE INTO ptr VALUES (?,?,?)", (ip, host, time.time()))
    except concurrent.futures.TimeoutError:
        pass
    ex.shutdown(wait=False, cancel_futures=True)
    con.commit()
    con.close()


def summary(days: int = 30, top: int = 100) -> dict:
    """集計ページの中身。"""
    _init()
    since = time.strftime("%Y-%m-%d", time.localtime(time.time() - (days - 1) * 86400))
    con = _con()
    rows = con.execute("""
        SELECT ip, COUNT(DISTINCT day) AS days, MAX(dev) AS devices, SUM(pv) AS pv, MAX(day) AS last
        FROM (SELECT day, ip, COUNT(*) AS dev, SUM(n) AS pv FROM hits WHERE day >= ? GROUP BY day, ip)
        GROUP BY ip ORDER BY days DESC, devices DESC, pv DESC""", (since,)).fetchall()
    con.close()
    # 逆引きは「何度も来ている・複数の端末がある」IPから優先して引く
    _lookup([r[0] for r in rows[:300]])
    con = _con()
    ptr = dict(con.execute("SELECT ip, host FROM ptr").fetchall())
    con.close()

    total = len(rows)
    multi_day = sum(1 for r in rows if r[1] >= 2)
    multi_dev = sum(1 for r in rows if r[2] >= 2)
    both = sum(1 for r in rows if r[1] >= 2 and r[2] >= 2)
    kinds: dict[str, dict] = {}
    orgs: dict[str, dict] = {}
    for ip, d, dev, pv, last in rows:
        host = ptr.get(ip, "")
        k = kind(host) if ip in ptr else "未確認"
        s = kinds.setdefault(k, {"ips": 0, "repeat": 0, "pv": 0})
        s["ips"] += 1
        s["repeat"] += d >= 2 or dev >= 2
        s["pv"] += pv
        if k in ("企業・団体", "行政", "大学・研究・学校"):
            dom = org_domain(host)
            o = orgs.setdefault(dom, {"domain": dom, "kind": k, "ips": 0, "days": 0, "devices": 0, "pv": 0, "last": ""})
            o["ips"] += 1
            o["days"] = max(o["days"], d)
            o["devices"] = max(o["devices"], dev)
            o["pv"] += pv
            o["last"] = max(o["last"], last)
    detail = [{"ip": mask(ip), "days": d, "devices": dev, "pv": pv, "last": last,
               "host": org_domain(ptr.get(ip, "")), "kind": kind(ptr.get(ip, "")) if ip in ptr else "未確認"}
              for ip, d, dev, pv, last in rows[:top]]
    return {"days": days, "since": since, "total": total, "multi_day": multi_day, "multi_dev": multi_dev,
            "both": both, "kinds": sorted(kinds.items(), key=lambda x: -x[1]["ips"]),
            "orgs": sorted(orgs.values(), key=lambda o: (-o["days"], -o["devices"], -o["pv"])),
            "detail": detail}
