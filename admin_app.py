"""MedilenZ 会員管理アプリ（本体アプリとは別プロセス・別サブドメインで動かす）。

会員一覧・Stripe決済状況の確認・手動アカウント発行・ロール編集・利用停止/退会処理・
ログイン履歴を1画面にまとめた管理者専用ツール。認証は本体アプリと同じ
data/auth_config.yaml を共有し、adminロールを持つ会員のみアクセスできる
（auth.require_admin_login()）。

起動: streamlit run admin_app.py --server.port 8502
"""
import os

import pandas as pd
import streamlit as st
import stripe
import streamlit_authenticator as stauth
import yaml

import auth
import mailer
import payments

st.set_page_config(page_title="MedilenZ 会員管理", page_icon="🛠️", layout="wide")

_STRIPE_STATUS_LABELS = {
    "active": "有効",
    "trialing": "トライアル中",
    "past_due": "支払い遅延",
    "canceled": "キャンセル済み",
    "unpaid": "未払い",
    "incomplete": "未完了",
    "incomplete_expired": "期限切れ",
    "paused": "一時停止",
}

if "_admin_authenticator" not in st.session_state:
    st.session_state["_admin_authenticator"] = auth.get_authenticator()
_authenticator = st.session_state["_admin_authenticator"]

auth.require_admin_login(_authenticator)


def _load_users() -> dict:
    with open(auth.CONFIG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    return (cfg.get("credentials") or {}).get("usernames") or {}


def _save_users(users: dict) -> None:
    with open(auth.CONFIG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    cfg.setdefault("credentials", {})["usernames"] = users
    with open(auth.CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)


@st.cache_data(ttl=60, show_spinner=False)
def _stripe_status(email: str) -> str:
    if not payments.STRIPE_SECRET_KEY:
        return "（Stripe未設定）"
    try:
        customers = stripe.Customer.list(email=email, limit=1)
        if not customers.data:
            return "Stripe顧客情報なし"
        subs = stripe.Subscription.list(customer=customers.data[0].id, status="all", limit=1)
        if not subs.data:
            return "サブスクなし"
        status = subs.data[0].status
        return _STRIPE_STATUS_LABELS.get(status, status)
    except Exception:
        return "取得エラー"


def _load_login_log() -> pd.DataFrame:
    if not os.path.exists(auth.LOGIN_LOG_PATH):
        return pd.DataFrame(columns=["日時", "メールアドレス"])
    return pd.read_csv(auth.LOGIN_LOG_PATH)


# ── ヘッダー ──────────────────────────────────────────
_hc1, _hc2, _hc3 = st.columns([7, 1.4, 1.4])
with _hc1:
    st.markdown("### 🛠️ MedilenZ 会員管理")
    st.caption(st.session_state.get("username", ""))
with _hc2:
    with st.popover("🔑 変更"):
        try:
            if _authenticator.reset_password(
                st.session_state.get("username", ""),
                location="main",
                key="_admin_reset_pw_form",
                fields={
                    "Form name": "パスワード変更",
                    "Current password": "現在のパスワード",
                    "New password": "新しいパスワード",
                    "Repeat password": "新しいパスワード（確認）",
                    "Reset": "変更する",
                },
            ):
                st.success("パスワードを変更しました。")
        except Exception as e:
            st.error(str(e))
with _hc3:
    _authenticator.logout("ログアウト", "main", key="_admin_logout_btn")

st.divider()

_tab_list, _tab_new, _tab_log = st.tabs(["👥 会員一覧", "➕ アカウント発行", "🕑 ログイン履歴"])

# ── 会員一覧 ──────────────────────────────────────────
with _tab_list:
    _users = _load_users()

    if not _users:
        st.caption("登録済みの会員はいません。")
    else:
        _kw = st.text_input("🔍 メールアドレスで絞り込み", key="_admin_kw")
        _emails = [e for e in _users if _kw.lower() in e.lower()] if _kw else list(_users.keys())

        _log_df = _load_login_log()
        _last_login = (
            _log_df.groupby("メールアドレス")["日時"].max()
            if not _log_df.empty
            else pd.Series(dtype=str)
        )

        with st.spinner("Stripeの決済状況を確認しています..."):
            _rows = [
                {
                    "メールアドレス": e,
                    "ロール": "・".join(_users[e].get("roles") or []),
                    "Stripe状況": _stripe_status(e),
                    "最終ログイン": _last_login.get(e, "―"),
                }
                for e in _emails
            ]
        _list_df = pd.DataFrame(_rows)
        st.dataframe(_list_df, use_container_width=True, hide_index=True)
        st.caption(f"{len(_list_df)}件（全{len(_users)}件中）")

        st.download_button(
            "⬇️ CSVダウンロード",
            _list_df.to_csv(index=False).encode("utf-8-sig"),
            file_name="medilenz_members.csv",
            mime="text/csv",
        )

        st.divider()
        st.markdown("#### 個別操作")
        _target = st.selectbox("対象のメールアドレス", _emails, key="_admin_target_sel")

        if _target:
            _target_roles = list(_users[_target].get("roles") or [])
            _is_suspended = "suspended" in _target_roles

            _oc1, _oc2 = st.columns(2)
            with _oc1:
                st.caption("ロールの編集")
                _new_roles = st.multiselect(
                    "ロール", ["paid", "admin", "suspended"],
                    default=_target_roles, key="_admin_role_edit",
                )
                if st.button("ロールを保存", key="_admin_role_save"):
                    _users[_target]["roles"] = _new_roles
                    _save_users(_users)
                    st.success(f"{_target} のロールを更新しました: {_new_roles}")
                    st.rerun()

            with _oc2:
                st.caption("利用停止・退会処理")
                _suspend_label = "✅ 利用停止を解除する" if _is_suspended else "🚫 利用を停止する"
                if st.button(_suspend_label, use_container_width=True, key="_admin_toggle_suspend"):
                    if _is_suspended:
                        _target_roles = [r for r in _target_roles if r != "suspended"]
                    else:
                        _target_roles.append("suspended")
                    _users[_target]["roles"] = _target_roles
                    _save_users(_users)
                    st.success(f"{_target} のロールを更新しました: {_target_roles}")
                    st.rerun()

                _confirm_del = st.checkbox(
                    f"{_target} を完全に削除することを確認", key="_admin_confirm_delete",
                )
                if st.button(
                    "🗑️ 退会処理（アカウント削除）", use_container_width=True,
                    disabled=not _confirm_del, key="_admin_delete_user",
                ):
                    del _users[_target]
                    _save_users(_users)
                    st.success(f"{_target} を削除しました。")
                    st.rerun()

# ── アカウント発行 ──────────────────────────────────────
with _tab_new:
    st.caption("Stripe決済を経由せずに会員アカウントを手動発行します（運営者アカウント・動作確認用など）。")
    with st.form("_admin_new_account_form"):
        _new_email = st.text_input("メールアドレス（ログインID）")
        _new_password = st.text_input(
            "パスワード（空欄なら自動生成）", type="password",
        )
        _new_roles = st.multiselect("付与するロール", ["paid", "admin"], default=["paid"])
        _send_mail = st.checkbox("ログイン情報をメールで送信する", value=True)
        _submitted = st.form_submit_button("アカウントを発行する")

    if _submitted:
        if not _new_email or "@" not in _new_email:
            st.error("正しいメールアドレスを入力してください")
        else:
            _pw = _new_password or payments.gen_password()
            try:
                _authenticator.authentication_controller.register_user(
                    "会員", "登録", _new_email, _new_email, _pw, _pw, "",
                    roles=_new_roles, captcha=False,
                )
            except stauth.RegisterError as e:
                st.error(str(e))
            else:
                st.success(f"アカウントを発行しました: {_new_email}")
                st.code(_pw, language=None)
                st.caption("上記のパスワードは今しか表示されません。忘れないよう控えてください。")
                if _send_mail:
                    try:
                        mailer.send_credentials_email(_new_email, _pw)
                        st.success(f"{_new_email} 宛にログイン情報を送信しました。")
                    except Exception as e:
                        st.error(f"メール送信に失敗しました（{e}）。パスワードは上記からお伝えください。")

# ── ログイン履歴 ──────────────────────────────────────
with _tab_log:
    _log_df = _load_login_log()
    if _log_df.empty:
        st.caption("ログイン履歴はまだありません。")
    else:
        _log_kw = st.text_input("🔍 メールアドレスで絞り込み", key="_admin_log_kw")
        _disp = _log_df[_log_df["メールアドレス"].str.contains(_log_kw, case=False, na=False)] if _log_kw else _log_df
        st.dataframe(
            _disp.sort_values("日時", ascending=False),
            use_container_width=True, hide_index=True,
        )
        st.caption(f"{len(_disp)}件（全{len(_log_df)}件中）")
        st.download_button(
            "⬇️ CSVダウンロード",
            _disp.to_csv(index=False).encode("utf-8-sig"),
            file_name="medilenz_login_log.csv",
            mime="text/csv",
        )
