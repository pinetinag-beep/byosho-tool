"""info@medilenz.jp（ConoHa WING）からのメール送信（SMTP）。"""
import os
import smtplib
from email.header import Header
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import streamlit as st


def _get_config(key: str, default: str = "") -> str:
    try:
        val = st.secrets.get(key, "")
    except Exception:
        val = ""
    return val or os.environ.get(key, default)


SMTP_HOST = _get_config("SMTP_HOST", "mail1061.conoha.ne.jp")
SMTP_PORT = int(_get_config("SMTP_PORT", "587"))
SMTP_USER = _get_config("SMTP_USER", "info@medilenz.jp")
SMTP_PASSWORD = _get_config("SMTP_PASSWORD")


def _send(to_email: str, subject: str, body: str) -> None:
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = SMTP_USER
    msg["To"] = to_email

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_USER, [to_email], msg.as_string())


def send_backup_email(archive: bytes, filename: str) -> None:
    """会員データの日次バックアップ（backup.py）を運営者自身（SMTP_USER）宛に送る。"""
    msg = MIMEMultipart()
    msg["Subject"] = Header(f"【MedilenZ】会員データ自動バックアップ {filename}", "utf-8")
    msg["From"] = SMTP_USER
    msg["To"] = SMTP_USER
    msg.attach(MIMEText(
        "MedilenZの会員データ（会員アカウント・ログイン履歴・コミュニティ投稿）の"
        "日次自動バックアップです。\n"
        "VPSが失われた場合の復元用に、このメールは削除せず保管してください。\n\n"
        "復元方法はリポジトリの DEPLOY.md「会員データのバックアップと復元」を参照してください。\n",
        "plain", "utf-8",
    ))
    part = MIMEApplication(archive, "gzip")
    part.add_header("Content-Disposition", "attachment", filename=filename)
    msg.attach(part)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_USER, [SMTP_USER], msg.as_string())


def send_credentials_email(to_email: str, password: str) -> None:
    """決済完了後、発行したログイン情報をユーザーに送信する。"""
    body = f"""MedilenZにお申し込みいただきありがとうございます。

以下の情報でログインしてください。

ログインID（メールアドレス）: {to_email}
パスワード: {password}

https://medilenz.jp からログインできます。
ログイン後、画面右上の「⚙️ アカウント」からパスワードの変更・お支払い管理（解約）ができます。

--
MedilenZ
"""
    _send(to_email, "【MedilenZ】お申し込みが完了しました（ログイン情報のご案内）", body)


def send_password_reset_email(to_email: str, password: str) -> None:
    """パスワード再発行（forgot password）で発行した新しいパスワードを送信する。"""
    body = f"""MedilenZのパスワード再発行を承りました。

以下の新しいパスワードでログインしてください。

ログインID（メールアドレス）: {to_email}
新しいパスワード: {password}

https://medilenz.jp からログインできます。
ログイン後、画面右上の「⚙️ アカウント」からパスワードの変更・お支払い管理（解約）ができます。

心当たりがない場合は、お手数ですが info@medilenz.jp までご連絡ください。

--
MedilenZ
"""
    _send(to_email, "【MedilenZ】パスワードを再発行しました", body)
