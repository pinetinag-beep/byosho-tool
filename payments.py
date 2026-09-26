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


# 解約手続き中（cancel_at_period_end）でも期間末まではStripe上 "active" のまま。
# past_due（カード失敗でStripeが再請求を試みている猶予期間）も利用可として扱う。
_USABLE_STATUSES = {"active", "trialing", "past_due"}


def _customers_for(emails) -> list:
    """メールアドレス（複数候補）に紐づくStripe顧客を新しい順に返す。

    Stripeのemail絞り込みは大文字小文字を区別する一方、会員IDはstreamlit-authenticator
    が小文字化して保存するため、呼び出し側から「会員ID」と「登録時のメールアドレス」
    の両方を候補として渡してもらう。Checkoutはcustomer_email指定で毎回新しい顧客を
    作るため、同じメールアドレスで顧客が複数存在しうる（解約後の再契約等）。
    """
    seen, customers = set(), []
    for email in {e.strip() for e in emails if e}:
        for c in stripe.Customer.list(email=email, limit=20).auto_paging_iter():
            if c.id not in seen:
                seen.add(c.id)
                customers.append(c)
    customers.sort(key=lambda c: c.created, reverse=True)
    return customers


def subscription_state(emails) -> str:
    """会員の契約状態を返す。

    "active"  … 利用可能なサブスクリプションがある
    "ended"   … Stripe顧客は存在するが、利用可能なサブスクリプションが1つも無い（解約済み等）
    "none"    … Stripe顧客自体が無い（管理画面・create_account.pyで手動発行した
                 運営者・招待アカウント。決済を経由していないので制限しない）
    """
    customers = _customers_for(emails)
    if not customers:
        return "none"
    for c in customers:
        for s in stripe.Subscription.list(customer=c.id, status="all", limit=20).auto_paging_iter():
            if s.status in _USABLE_STATUSES:
                return "active"
    return "ended"


def create_portal_url(emails) -> str:
    """Stripe Customer Portal（解約・カード変更・領収書）のURLを返す（顧客が無ければ空文字）。

    Portalの表示内容（解約を許可するか等）はStripeダッシュボードの
    「設定 → Billing → カスタマーポータル」で保存した設定に従う。
    その設定を一度も保存していないとAPIがエラーを返す（DEPLOY.md参照）。
    """
    customers = _customers_for(emails)
    if not customers:
        return ""
    target = customers[0]
    for c in customers:
        subs = stripe.Subscription.list(customer=c.id, status="all", limit=20)
        if any(s.status in _USABLE_STATUSES for s in subs.auto_paging_iter()):
            target = c
            break
    session = stripe.billing_portal.Session.create(customer=target.id, return_url=APP_BASE_URL)
    return session.url


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
