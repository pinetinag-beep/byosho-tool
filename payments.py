"""Stripe決済連携（Checkout・月額サブスクリプション）。

APIキー等は st.secrets（Streamlit Cloud用）→ 環境変数（VPS用）の順で読む
（既存のANTHROPIC_API_KEY読み込みと同じフォールバック方式。app.py参照）。
"""
import os
import secrets as _secrets
import string

import streamlit as st
import stripe


def _get_config(key: str, default: str = "") -> str:
    try:
        val = st.secrets.get(key, "")
    except Exception:
        val = ""
    return val or os.environ.get(key, default)


STRIPE_SECRET_KEY = _get_config("STRIPE_SECRET_KEY")
STRIPE_PRICE_ID = _get_config("STRIPE_PRICE_ID")
APP_BASE_URL = _get_config("APP_BASE_URL", "https://medilenz.jp")

stripe.api_key = STRIPE_SECRET_KEY


def create_checkout_session(email: str) -> str:
    """Stripe Checkoutセッション（月額サブスクリプション）を作成し、決済ページのURLを返す。"""
    session = stripe.checkout.Session.create(
        mode="subscription",
        customer_email=email,
        line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
        success_url=f"{APP_BASE_URL}/?payment=success&session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{APP_BASE_URL}/?payment=cancel",
    )
    return session.url


def verify_paid_session(session_id: str) -> str:
    """Checkoutセッションが決済完了しているか確認し、メールアドレスを返す（未完了なら空文字）。"""
    session = stripe.checkout.Session.retrieve(session_id)
    if session.payment_status != "paid":
        return ""
    if session.customer_details and session.customer_details.email:
        return session.customer_details.email
    return session.customer_email or ""


def gen_password(length: int = 14) -> str:
    """streamlit-authenticatorのパスワードポリシー
    （8〜20文字・大小英字/数字/記号を各1文字以上）を満たすランダムパスワードを生成する。
    """
    upper = _secrets.choice(string.ascii_uppercase)
    lower = _secrets.choice(string.ascii_lowercase)
    digit = _secrets.choice(string.digits)
    special = _secrets.choice("!@#$%^&*()_+-=")
    rest_chars = string.ascii_letters + string.digits
    rest = "".join(_secrets.choice(rest_chars) for _ in range(length - 4))
    chars = list(upper + lower + digit + special + rest)
    _secrets.SystemRandom().shuffle(chars)
    return "".join(chars)
