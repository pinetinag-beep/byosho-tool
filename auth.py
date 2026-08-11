"""ユーザー認証（streamlit-authenticator）＋ Stripe決済連携。

会員情報（メールアドレス・ハッシュ化パスワード・ロール等）は
data/auth_config.yaml に保存する。このファイルはパスワードハッシュを含むため
.gitignore 対象（リポジトリにはコミットしない）。VPS上で初回起動時に自動生成される。

2026年8月、決済導入に伴い「誰でも自由に無料登録」の方式は廃止し、
Stripe Checkout（月額サブスクリプション）での決済完了後にのみアカウントが
発行される方式に変更した。フロー: サイトに来る→メールアドレス入力→Stripeの
決済ページへ→決済完了→アカウント自動発行（roles=["paid"]）→ログイン情報を
メールで通知→ログインして利用開始。
"""
import os
import secrets

import streamlit as st
import streamlit_authenticator as stauth
import yaml

import mailer
import payments

CONFIG_PATH = "data/auth_config.yaml"

_PASSWORD_INSTRUCTIONS = """
**パスワードの条件:**
- 8〜20文字
- 半角小文字を1文字以上
- 半角大文字を1文字以上
- 数字を1文字以上
- 記号（!@#$%^&*()_+-=[]{};:'"\\|,.<>/?`~）を1文字以上
"""


def _ensure_config() -> None:
    if os.path.exists(CONFIG_PATH):
        return
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    config = {
        "credentials": {"usernames": {}},
        "cookie": {
            "name": "medilenz_auth",
            "key": secrets.token_hex(32),
            "expiry_days": 30,
        },
        # "pre-authorized"キーは意図的に含めない。空リストにすると
        # 「事前許可されたメールアドレスしか登録できない」制限モードとして
        # 解釈され、誰も登録できなくなる（全ユーザー自由登録の想定と矛盾する）。
    }
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True)


def get_authenticator() -> stauth.Authenticate:
    _ensure_config()
    return stauth.Authenticate(
        CONFIG_PATH,
        password_instructions=_PASSWORD_INSTRUCTIONS,
    )


def _handle_payment_return(authenticator: stauth.Authenticate) -> None:
    """Stripe Checkoutからのリダイレクト（?payment=success&session_id=...）を処理する。

    決済完了を確認できたら、そのメールアドレスでアカウントを発行し
    （既に発行済みなら何もしない＝再読み込みしても二重発行・二重メールにならない）、
    ランダムなパスワードをメールで送る。
    """
    params = st.query_params
    if params.get("payment") != "success":
        return
    session_id = params.get("session_id", "")
    if not session_id:
        return

    email = payments.verify_paid_session(session_id)
    if not email:
        st.error("決済の確認ができませんでした。お手数ですがもう一度お試しください。")
        return

    password = payments.gen_password()
    try:
        authenticator.authentication_controller.register_user(
            "会員", "登録", email, email, password, password, "",
            roles=["paid"], captcha=False,
        )
    except stauth.RegisterError as e:
        if "already taken" in str(e):
            st.success("お申し込みは完了しています。「ログイン」タブからログインしてください。")
        else:
            st.error(str(e))
        return

    try:
        mailer.send_credentials_email(email, password)
        st.success(
            f"お申し込みが完了しました。{email} 宛にログイン情報をお送りしました。"
            "「ログイン」タブからログインしてください。"
        )
    except Exception as e:
        st.error(f"アカウントは発行されましたが、メール送信に失敗しました（{e}）。"
                  "お手数ですがサポートまでご連絡ください。")


def require_login(authenticator: stauth.Authenticate) -> None:
    """ログイン必須ゲート。未ログインならログイン/申込み画面を表示してst.stop()する。

    authenticator は呼び出し側（app.py）で1回だけ生成したインスタンスを渡すこと。
    stauth.Authenticate は内部でCookie管理用のカスタムコンポーネント（固定key）を
    生成するため、1回のスクリプト実行の中で複数回インスタンス化するとStreamlitの
    「同じkeyの要素が重複している」エラーになる。
    """
    if st.session_state.get("authentication_status"):
        return

    _handle_payment_return(authenticator)

    st.markdown(
        """
<div style="text-align:center;padding:40px 0 20px;">
  <h1 style="font-size:2.4rem;font-weight:900;color:#26251F;margin:0 0 6px;
             letter-spacing:-0.01em;font-family:'Helvetica Neue',Arial,sans-serif;">
    Medilen<span style="color:#12886D;">Z</span>
  </h1>
  <p style="font-size:0.9rem;font-weight:500;color:#6E6A5E;margin:0;">
    地域の医療をひらく、公的データのまど
  </p>
</div>""",
        unsafe_allow_html=True,
    )

    _tab_login, _tab_register = st.tabs(["ログイン", "新規申込み"])

    with _tab_login:
        authenticator.login(
            location="main",
            key="_login_form",
            fields={
                "Form name": "ログイン",
                "Username": "メールアドレス",
                "Password": "パスワード",
                "Login": "ログイン",
            },
        )
        if st.session_state.get("authentication_status") is False:
            st.error("メールアドレスまたはパスワードが違います")

    with _tab_register:
        st.markdown(
            "<p style='font-size:0.85rem;color:#6E6A5E;'>"
            "月額500円のお申し込みです。決済完了後、ログイン情報をメールでお送りします。"
            "</p>",
            unsafe_allow_html=True,
        )
        with st.form("_signup_form", clear_on_submit=False):
            _signup_email = st.text_input("メールアドレス", autocomplete="off")
            _signup_submitted = st.form_submit_button("お申し込みへ進む")
        if _signup_submitted:
            if not _signup_email or "@" not in _signup_email:
                st.error("正しいメールアドレスを入力してください")
            else:
                try:
                    _checkout_url = payments.create_checkout_session(_signup_email)
                    st.link_button(
                        "💳 決済ページへ進む（月額500円）", _checkout_url,
                        type="primary",
                    )
                except Exception as e:
                    st.error(f"決済ページの作成に失敗しました（{e}）")

    if st.session_state.get("authentication_status"):
        # クッキーによる自動ログインが成立した場合、ゲートUIを描き直さず
        # 素早く本来の画面に抜けるためrerunする
        st.rerun()

    st.stop()
