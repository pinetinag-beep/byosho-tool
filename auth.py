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
import csv
import os
import secrets
import time
from datetime import datetime, timedelta, timezone

import streamlit as st
import streamlit_authenticator as stauth
import yaml

import mailer
import payments

CONFIG_PATH = "data/auth_config.yaml"
LOGIN_LOG_PATH = "data/login_log.csv"
_JST = timezone(timedelta(hours=9))

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


def _log_login(email: str) -> None:
    """ログイン成功をdata/login_log.csvに記録する（1ブラウザセッションにつき1回）。"""
    if not email:
        return
    os.makedirs(os.path.dirname(LOGIN_LOG_PATH), exist_ok=True)
    is_new = not os.path.exists(LOGIN_LOG_PATH)
    with open(LOGIN_LOG_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(["日時", "メールアドレス"])
        writer.writerow([datetime.now(_JST).strftime("%Y-%m-%d %H:%M:%S"), email])


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


def _render_landing_content() -> None:
    """未ログイン時のLPコンテンツ（機能紹介・実画面・料金・使い方・利用条件）。"""
    st.markdown(
        """
<div style="max-width:680px;margin:0 auto;">
  <div style="text-align:center;margin:0 0 32px;">
    <h2 style="font-size:1.7rem;font-weight:900;color:#26251F;margin:0 0 14px;line-height:1.5;">
      地域医療のリアルを、公的データで一目に。
    </h2>
    <p style="font-size:0.95rem;color:#6E6A5E;max-width:520px;margin:0 auto;line-height:1.9;">
      病床機能報告・DPC・施設基準届出など、バラバラな公的統計をMedilenZが横断的に統合。
      競合病院との比較や地域内でのポジション把握が、ひとつの画面で完結します。
    </p>
  </div>

  <div style="display:flex;flex-wrap:wrap;justify-content:center;gap:8px 24px;
              margin:0 0 32px;font-size:0.78rem;color:#9c9890;">
    <div>病床機能報告 <strong style="color:#6E6A5E;">4年分</strong></div>
    <div>DPC <strong style="color:#6E6A5E;">3年分</strong></div>
    <div>施設基準届出 <strong style="color:#6E6A5E;">全国47都道府県</strong></div>
    <div>対応病院 <strong style="color:#6E6A5E;">7,000件以上</strong></div>
  </div>
</div>""",
        unsafe_allow_html=True,
    )

    st.markdown(
        "<p style='text-align:center;font-size:0.8rem;font-weight:700;color:#0B6653;"
        "margin:0 0 8px;'>📋 実際の病院詳細画面</p>",
        unsafe_allow_html=True,
    )
    _shot_l, _shot_c, _shot_r = st.columns([1, 8, 1])
    with _shot_c:
        with st.container(border=True):
            st.image("assets/lp_hospital_detail.png", use_container_width=True)
        st.markdown(
            "<p style='text-align:center;font-size:0.78rem;color:#6E6A5E;margin:6px 0 0;'>"
            "許可病床数・稼働率・地域内順位・地域シェアなどを自動集計して表示します"
            "</p>",
            unsafe_allow_html=True,
        )

    st.markdown("<div style='margin:36px 0 20px;'></div>", unsafe_allow_html=True)

    st.markdown(
        """
<div style="max-width:820px;margin:0 auto;">
  <div style="display:flex;flex-wrap:wrap;gap:16px;margin:0 0 32px;">
    <div style="flex:1;min-width:220px;background:#FFFFFF;border:1px solid #E8E4DB;
                border-radius:14px;padding:20px;">
      <div style="font-size:1.4rem;">🏆</div>
      <div style="font-weight:800;color:#26251F;margin:8px 0 4px;">自院の立ち位置が分かる</div>
      <div style="font-size:0.85rem;color:#6E6A5E;line-height:1.6;">
        稼働率・地域シェア・地域内順位を自動算出。自院が地域でどんな役割を担っているか、
        数字で把握できます。
      </div>
    </div>
    <div style="flex:1;min-width:220px;background:#FFFFFF;border:1px solid #E8E4DB;
                border-radius:14px;padding:20px;">
      <div style="font-size:1.4rem;">📈</div>
      <div style="font-weight:800;color:#26251F;margin:8px 0 4px;">経年トレンドで推移を把握</div>
      <div style="font-size:0.85rem;color:#6E6A5E;line-height:1.6;">
        病床数・稼働率・手術件数などの推移を年度ごとに確認可能。自院や
        気になる病院の変化を追えます。
      </div>
    </div>
    <div style="flex:1;min-width:220px;background:#FFFFFF;border:1px solid #E8E4DB;
                border-radius:14px;padding:20px;">
      <div style="font-size:1.4rem;">🔍</div>
      <div style="font-weight:800;color:#26251F;margin:8px 0 4px;">条件で全国から検索</div>
      <div style="font-size:0.85rem;color:#6E6A5E;line-height:1.6;">
        CT/MRI台数・手術件数・救急搬送件数・DPC疾患名など、豊富な条件で
        全国の病院を絞り込めます。住所からの距離・所要時間での検索にも対応。
      </div>
    </div>
  </div>
</div>""",
        unsafe_allow_html=True,
    )

    st.markdown(
        "<p style='text-align:center;font-size:0.8rem;font-weight:700;color:#0B6653;"
        "margin:0 0 8px;'>📋 実際の検索結果画面</p>",
        unsafe_allow_html=True,
    )
    _shot2_l, _shot2_c, _shot2_r = st.columns([1, 8, 1])
    with _shot2_c:
        with st.container(border=True):
            st.image("assets/lp_search_results.png", use_container_width=True)
        st.markdown(
            "<p style='text-align:center;font-size:0.78rem;color:#6E6A5E;margin:6px 0 0;'>"
            "エリア・設備・手術件数などの条件で絞り込むと、該当する病院が一覧表示されます"
            "</p>",
            unsafe_allow_html=True,
        )

    st.markdown("<div style='margin:36px 0 0;'></div>", unsafe_allow_html=True)

    st.markdown(
        """
<div style="max-width:820px;margin:0 auto;">
  <div style="background:#FFFFFF;border:2px solid #12886D;border-radius:14px;
              padding:24px;text-align:center;margin:0 0 24px;">
    <div style="font-size:0.8rem;font-weight:700;color:#0B6653;letter-spacing:0.05em;">
      料金プラン
    </div>
    <div style="margin:6px 0 4px;">
      <span style="font-size:2.2rem;font-weight:900;color:#26251F;">¥500</span>
      <span style="font-size:0.95rem;color:#6E6A5E;"> / 月（税込）</span>
    </div>
    <div style="font-size:0.8rem;color:#6E6A5E;">
      クレジットカード決済・お申し込み後すぐにご利用いただけます
    </div>
  </div>

  <div style="background:#EAF4F0;border:1px solid #BFDFD4;border-radius:14px;
              padding:20px 24px;margin:0 0 28px;">
    <div style="font-weight:800;color:#26251F;margin-bottom:10px;">使い方</div>
    <ol style="margin:0;padding-left:20px;color:#4b5563;font-size:0.9rem;line-height:1.9;">
      <li>下の「新規利用申し込み」タブでメールアドレスを入力</li>
      <li>Stripeの決済ページでお支払い（月額500円）</li>
      <li>決済完了後、自動でアカウントが発行され、ログイン情報がメールで届きます</li>
      <li>メールに記載のIDとパスワードで「ログイン」タブからログイン</li>
    </ol>
  </div>
</div>""",
        unsafe_allow_html=True,
    )

def _render_tokushoho() -> None:
    """特定商取引法に基づく表示。ページ最下部に、目立たない大きさで配置する。"""
    st.markdown(
        """
<style>
.st-key-_tokushoho_box details summary {
    font-size: 0.75rem !important;
    color: #9c9890 !important;
}
.st-key-_tokushoho_box details summary svg { width: 14px !important; height: 14px !important; }
</style>""",
        unsafe_allow_html=True,
    )
    with st.container(key="_tokushoho_box"):
        with st.expander("利用条件・特定商取引法に基づく表示"):
            st.markdown(
                """
**料金プラン**：月額500円（税込）

**お支払い方法**：クレジットカード決済（Stripe）

**お支払い時期**：お申し込み時に決済、以降は毎月自動更新（同日課金）

**サービス提供時期**：決済完了後、即時にご利用いただけます

**解約について**：現在、解約手続きはご自身では行えません。解約をご希望の場合は
info@medilenz.jp までご連絡ください。デジタルサービスの性質上、お支払い済みの
期間分の返金には原則対応しておりません。

---

**特定商取引法に基づく表示**

| 項目 | 内容 |
|---|---|
| 販売業者 | MedilenZ |
| 運営統括責任者 | 高橋 信一 |
| 所在地 | ご請求いただければ遅滞なく開示いたします |
| 電話番号 | ご請求いただければ遅滞なく開示いたします |
| メールアドレス | info@medilenz.jp |
| 販売価格 | 月額500円（税込） |
"""
            )


def _try_cookie_login(authenticator: stauth.Authenticate) -> None:
    """再ログイン用Cookieが有効なら、そこからセッションを復元する。

    streamlit-authenticator組み込みのlogin()が内部でやっている処理と同じ
    （このモジュールは独自のログインフォームを使うため、この部分だけ個別に呼ぶ必要がある。
    sleepもライブラリ本家のPRE_LOGIN_SLEEP_TIME=0.7秒に合わせている——
    CookieManagerコンポーネントがブラウザのCookieを読み取ってPython側に
    値を返すまでには往復が必要で、間を置かないとまだ読み取れていない
    Noneのまま「未ログイン」と判定してしまうことがある）。
    """
    if not st.session_state.get("authentication_status"):
        token = authenticator.cookie_controller.get_cookie()
        if token:
            authenticator.authentication_controller.login(token=token)
        time.sleep(0.7)


def _render_login_form(authenticator: stauth.Authenticate, key: str) -> None:
    """ログインフォームを描画する。

    streamlit-authenticator組み込みのlogin()を使わず独自にフォームを組んでいる理由は2つ：

    1. **Chromeのパスワードマネージャー対策**: ライブラリ側はUsername/Password両方の
       入力欄に固定で `autocomplete='off'` を付けており、公開APIから上書きする手段が
       無い。Chromeはパスワード欄自体は`autocomplete='off'`でも保存対象にする一方、
       ペアとなるメールアドレス欄の特定にはautocomplete属性を参照するため、
       「パスワードだけ保存され、メールアドレスは提案されない」という状態になっていた
       （2026年8月、本番で発覚）。`autocomplete="username"`/`"current-password"`を
       明示することで両方とも正しく提案・保存されるようにする。
    2. **「ログイン状態を保持する」チェックボックス**: streamlit-authenticatorは
       ログイン成功時に必ず`cookie.expiry_days`（既定30日）ぶんの再ログイン用Cookieを
       発行する設計で、チェックボックス等でON/OFFする仕組みが無い。
       `cookie_model.cookie_expiry_days`を送信直前に一時的に0へ書き換えると
       `set_cookie()`が何もしなくなる（ライブラリ側の仕様）ことを利用し、
       未チェック時はCookieを発行しない＝ブラウザを閉じたら再ログインが必要、
       という制御を実現している。

    呼び出し側で`st.rerun()`しないこと（重要）: `set_cookie()`はCookieManager
    コンポーネントに「このCookieをセットして」という指示を送るだけで、実際に
    ブラウザへ書き込まれるのは少し後（フロントエンドの次の描画サイクル）になる。
    ここで即座に`st.rerun()`すると、書き込みが完了する前に画面が再構築されて
    しまい、Cookieが実際には保存されない競合状態になる（本番で発覚・実際に
    4回中3回の頻度で再現した）。streamlit-authenticator本家のlogin()も同じ理由で
    セット直後には無条件でrerunしておらず、フォーム以外の残りのUI描画を続けて
    時間を稼いでから、呼び出し元が最後にまとめてrerunする作りになっている。
    このモジュールもそれに倣い、rerunは呼び出し元（require_login等）が
    残りのUIを描画し終えた後に行う。
    """
    with st.form(key, clear_on_submit=False):
        st.subheader("ログイン")
        _email = st.text_input("メールアドレス", autocomplete="username")
        _pw = st.text_input("パスワード", type="password", autocomplete="current-password")
        _remember = st.checkbox("ログイン状態を保持する", value=True)
        _submitted = st.form_submit_button("ログイン")

    if not _submitted:
        return
    if not _email or not _pw:
        st.error("メールアドレスとパスワードを入力してください")
        return
    try:
        ok = authenticator.authentication_controller.login(_email, _pw)
    except Exception as e:
        st.error(str(e))
        return
    if not ok:
        st.error("メールアドレスまたはパスワードが違います")
        return
    if not _remember:
        authenticator.cookie_controller.cookie_model.cookie_expiry_days = 0
    authenticator.cookie_controller.set_cookie()
    time.sleep(0.7)


def require_login(authenticator: stauth.Authenticate) -> None:
    """ログイン必須ゲート。未ログインならログイン/申込み画面を表示してst.stop()する。

    authenticator は呼び出し側（app.py）で1回だけ生成したインスタンスを渡すこと。
    stauth.Authenticate は内部でCookie管理用のカスタムコンポーネント（固定key）を
    生成するため、1回のスクリプト実行の中で複数回インスタンス化するとStreamlitの
    「同じkeyの要素が重複している」エラーになる。
    """
    _try_cookie_login(authenticator)

    if st.session_state.get("authentication_status"):
        if "suspended" in (st.session_state.get("roles") or []):
            authenticator.logout(location="unrendered")
            st.error(
                "このアカウントは利用停止中です。心当たりがない場合は "
                "info@medilenz.jp までご連絡ください。"
            )
            st.stop()
        if not st.session_state.get("_login_logged"):
            _log_login(st.session_state.get("username", ""))
            st.session_state["_login_logged"] = True
        return

    _handle_payment_return(authenticator)

    # margin-top:-110pxは、この見出しの手前にある非表示ユーティリティ要素
    # （notranslate等のスクリプト・CookieManagerコンポーネント）がStreamlit既定の
    # 縦gapを消費して積み上がった空白を打ち消すための調整（2026年8月、ヘッダーの
    # 上が間延びして見えると指摘され対応）。ヘッダー（高さ60px・絶対配置）とは
    # 重ならない範囲で詰めている。
    st.markdown(
        """
<div style="text-align:center;padding:40px 0 20px;margin-top:-110px;">
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

    _render_landing_content()

    _tab_register, _tab_login = st.tabs(["🆕 新規利用申し込み", "ログイン"])

    with _tab_login:
        _render_login_form(authenticator, "_login_form")

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

    st.markdown("<div style='margin:40px 0 8px;'></div>", unsafe_allow_html=True)
    _render_tokushoho()

    if st.session_state.get("authentication_status"):
        # ここまでLPの残りを描画し終えたことで、直前のログイン成功時に発行した
        # Cookieがブラウザへ書き込まれる時間を稼げている（_render_login_form参照）。
        st.rerun()

    st.stop()


def require_admin_login(authenticator: stauth.Authenticate) -> None:
    """会員管理アプリ（admin_app.py）用のログインゲート。

    本体アプリのrequire_login()と違いLP・新規申込みタブは持たず、ログインフォーム
    のみを表示する。adminロールを持たないユーザーはログインできてもここで弾く。
    """
    _try_cookie_login(authenticator)

    if st.session_state.get("authentication_status"):
        if "admin" not in (st.session_state.get("roles") or []):
            authenticator.logout(location="unrendered")
            st.error("このアプリは管理者専用です。")
            st.stop()
        if not st.session_state.get("_login_logged"):
            _log_login(st.session_state.get("username", ""))
            st.session_state["_login_logged"] = True
        return

    st.markdown(
        "<h2 style='text-align:center;margin:60px 0 24px;'>MedilenZ 会員管理</h2>",
        unsafe_allow_html=True,
    )
    _c1, _c2, _c3 = st.columns([1, 2, 1])
    with _c2:
        _render_login_form(authenticator, "_admin_login_form")

    if st.session_state.get("authentication_status"):
        # 即rerunしない理由は_render_login_form参照（Cookie書き込みのレース対策）。
        # このアプリはLPのような「間を持たせる」他の描画が無いため、追加の
        # sleepでCookie書き込みの猶予を確保する。
        time.sleep(0.5)
        st.rerun()

    st.stop()
