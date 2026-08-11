"""ユーザー認証（streamlit-authenticator）。

会員情報（メールアドレス・ハッシュ化パスワード・ロール等）は
data/auth_config.yaml に保存する。このファイルはパスワードハッシュを含むため
.gitignore 対象（リポジトリにはコミットしない）。VPS上で初回起動時に自動生成される。

ロール（roles）には現状 "free" を自動付与するのみで、機能制限には未使用
（有料プラン運用が固まった後、admin_manage_users.py 等で "paid" に昇格する想定）。
"""
import os
import secrets

import streamlit as st
import streamlit_authenticator as stauth
import yaml

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


def require_login(authenticator: stauth.Authenticate) -> None:
    """ログイン必須ゲート。未ログインならログイン/新規登録画面を表示してst.stop()する。

    authenticator は呼び出し側（app.py）で1回だけ生成したインスタンスを渡すこと。
    stauth.Authenticate は内部でCookie管理用のカスタムコンポーネント（固定key）を
    生成するため、1回のスクリプト実行の中で複数回インスタンス化するとStreamlitの
    「同じkeyの要素が重複している」エラーになる。
    """
    if st.session_state.get("authentication_status"):
        return

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

    _tab_login, _tab_register = st.tabs(["ログイン", "新規登録"])

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
        try:
            _email, _username, _name = authenticator.register_user(
                location="main",
                key="_register_form",
                merge_username_email=True,
                roles=["free"],
                fields={
                    "Form name": "新規登録",
                    "First name": "姓",
                    "Last name": "名",
                    "Email": "メールアドレス",
                    "Password": "パスワード",
                    "Repeat password": "パスワード（確認）",
                    "Password hint": "パスワードのヒント（任意）",
                    "Captcha": "画像に表示されている文字を入力",
                    "Register": "登録する",
                },
            )
            if _email:
                st.success("登録が完了しました。「ログイン」タブからログインしてください。")
        except stauth.RegisterError as e:
            st.error(str(e))

    if st.session_state.get("authentication_status"):
        # クッキーによる自動ログインが成立した場合、ゲートUIを描き直さず
        # 素早く本来の画面に抜けるためrerunする
        st.rerun()

    st.stop()
