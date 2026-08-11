"""info@medilenz.jp（ConoHa WING）からのメール送信（SMTP）。"""
import os
import smtplib
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


def send_credentials_email(to_email: str, password: str) -> None:
    """決済完了後、発行したログイン情報をユーザーに送信する。"""
    body = f"""MedilenZにお申し込みいただきありがとうございます。

以下の情報でログインしてください。

ログインID（メールアドレス）: {to_email}
パスワード: {password}

https://medilenz.jp からログインできます。

--
MedilenZ
"""
    msg = MIMEText(body)
    msg["Subject"] = "【MedilenZ】お申し込みが完了しました（ログイン情報のご案内）"
    msg["From"] = SMTP_USER
    msg["To"] = to_email

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_USER, [to_email], msg.as_string())
