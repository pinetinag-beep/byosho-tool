"""会員データ（data/配下のgit管理外ファイル）の日次自動バックアップ。

会員アカウント（auth_config.yaml）・ログイン履歴・コミュニティ投稿はgitに入らず
VPS上にしか存在しないため、ファイル破損（auth_config.yamlが空になる事故は
本番で実際に起きた。auth.config_lock参照）やVPS自体の障害で失われると復元
できない。そこで1日1回、次の2か所に退避する:

1. VPS内: data/backups/medilenz_userdata_YYYYMMDD.tar.gz（30日分保持）
   … ファイル破損・誤操作からの復元用
2. info@medilenz.jp 宛のメール添付（ConoHa WINGのメールサーバー＝VPSとは別サーバー）
   … VPSごと失われた場合の復元用。メールボックスが漏れた場合に全会員の
     ログインCookieを偽造されないよう、auth_config.yamlのCookie署名鍵だけは
     伏せ字にしてある（復元時は新しい鍵を入れればよい。全員が一度再ログイン
     になるだけで、アカウント・パスワードはそのまま使える）

cronの設定を不要にするため、アプリへのその日最初のアクセス時にバックグラウンド
スレッドで実行する（app.pyから run_daily_if_needed() を呼ぶ）。アクセスが無い日は
データも変化しないので、その日のバックアップが無くても失うものは無い。
"""
import fcntl
import io
import os
import tarfile
import threading
import traceback
from datetime import datetime, timedelta, timezone

import yaml

import auth
import mailer

BACKUP_DIR = "data/backups"
KEEP_DAYS = 30
_TARGETS = [
    auth.CONFIG_PATH,
    auth.LOGIN_LOG_PATH,
    "data/community_posts.json",
    "data/community_nicknames.json",
]
_LOCK_PATH = os.path.join(BACKUP_DIR, ".lock")
_JST = timezone(timedelta(hours=9))
_started_day = ""


def _today() -> str:
    return datetime.now(_JST).strftime("%Y%m%d")


def _archive_name(day: str) -> str:
    return f"medilenz_userdata_{day}.tar.gz"


def _read_targets() -> dict[str, bytes]:
    files = {}
    for path in _TARGETS:
        if not os.path.exists(path):
            continue
        if path == auth.CONFIG_PATH:
            # streamlit_authenticatorは書き込み時にファイルを一度空に切り詰めるため、
            # 書き込み途中の空ファイルを退避しないようロックの中で読む
            with auth.config_lock():
                with open(path, "rb") as f:
                    files[path] = f.read()
        else:
            with open(path, "rb") as f:
                files[path] = f.read()
    return files


def _redact_cookie_key(raw: bytes) -> bytes:
    cfg = yaml.safe_load(raw) or {}
    if isinstance(cfg.get("cookie"), dict):
        cfg["cookie"]["key"] = "REDACTED_SET_A_NEW_RANDOM_KEY_WHEN_RESTORING"
    return yaml.dump(cfg, default_flow_style=False, allow_unicode=True).encode("utf-8")


def _make_archive(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path, data in files.items():
            info = tarfile.TarInfo(path)
            info.size = len(data)
            info.mtime = int(datetime.now().timestamp())
            info.mode = 0o600
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _prune() -> None:
    cutoff = (datetime.now(_JST) - timedelta(days=KEEP_DAYS)).strftime("%Y%m%d")
    for name in os.listdir(BACKUP_DIR):
        if not (name.startswith("medilenz_userdata_") and name.endswith(".tar.gz")):
            continue
        if name[len("medilenz_userdata_"):-len(".tar.gz")] < cutoff:
            os.remove(os.path.join(BACKUP_DIR, name))


def run_backup(send_mail: bool = True) -> str:
    """バックアップを実行し、作成したアーカイブのパスを返す（手動実行にも使える）。"""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    os.chmod(BACKUP_DIR, 0o700)
    day = _today()
    path = os.path.join(BACKUP_DIR, _archive_name(day))

    files = _read_targets()
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(_make_archive(files))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    _prune()

    if send_mail and mailer.SMTP_PASSWORD:
        mail_files = dict(files)
        if auth.CONFIG_PATH in mail_files:
            mail_files[auth.CONFIG_PATH] = _redact_cookie_key(mail_files[auth.CONFIG_PATH])
        mailer.send_backup_email(_make_archive(mail_files), _archive_name(day))
    return path


def _run_locked() -> None:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    with open(_LOCK_PATH, "w") as lf:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return  # 別のスレッド/プロセスが実行中
        try:
            if os.path.exists(os.path.join(BACKUP_DIR, _archive_name(_today()))):
                return
            path = run_backup()
            print(f"[backup] 会員データをバックアップしました: {path}", flush=True)
        except Exception:
            print("[backup] 会員データのバックアップに失敗しました", flush=True)
            traceback.print_exc()
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def run_daily_if_needed() -> None:
    """その日のバックアップがまだ無ければ、バックグラウンドで1回実行する。

    Streamlitは操作のたびにスクリプト全体を再実行するため、ここはファイルの
    存在確認だけで即座に戻る軽い処理にしてある。
    """
    global _started_day
    day = _today()
    if _started_day == day:
        return
    if os.path.exists(os.path.join(BACKUP_DIR, _archive_name(day))):
        _started_day = day
        return
    _started_day = day
    threading.Thread(target=_run_locked, daemon=True).start()


if __name__ == "__main__":
    # 手動実行: python3 backup.py （--no-mail でメール送信なし）
    import sys
    print(run_backup(send_mail="--no-mail" not in sys.argv))
