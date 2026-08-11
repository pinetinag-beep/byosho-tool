"""決済を経由せずにアカウントを手動発行する管理者用スクリプト（VPS上でSSH経由で実行する）。

運営者自身のアカウント（info@medilenz.jp 等）や、動作確認用アカウントを
Stripe決済を通さずに作成する場合に使う。

使い方:
    python3 create_account.py info@medilenz.jp
    python3 create_account.py info@medilenz.jp --password "任意のパスワード"
"""
import argparse
import sys

import streamlit_authenticator as stauth

import auth
import payments


def main() -> None:
    parser = argparse.ArgumentParser(description="決済を経由しないアカウントの手動発行")
    parser.add_argument("email", help="ログインID（メールアドレス）")
    parser.add_argument("--password", default=None, help="パスワード（省略時は自動生成）")
    parser.add_argument("--role", default="paid", help="付与するロール（既定: paid）")
    args = parser.parse_args()

    password = args.password or payments.gen_password()

    authenticator = auth.get_authenticator()
    try:
        authenticator.authentication_controller.register_user(
            "管理", "アカウント", args.email, args.email, password, password, "",
            roles=[args.role], captcha=False,
        )
    except stauth.RegisterError as e:
        print(f"エラー: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"アカウントを発行しました: {args.email}")
    print(f"パスワード: {password}")
    print("上記のパスワードは今しか表示されません（ハッシュ化されて保存されるため、忘れないよう控えてください）。")


if __name__ == "__main__":
    main()
