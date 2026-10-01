"""Streamlit Community Cloud のアプリを眠らせないための定期アクセス（GitHub Actionsから実行）。

Community Cloud は12時間アクセスが無いとアプリをスリープさせ、起こすには
ブラウザで開いて「Yes, get this app back up!」ボタンを押す必要がある。
URLへの単純なHTTPアクセス（requests等）では200が返ってもアプリは起きないため、
ヘッドレスブラウザ（Playwright）で実際にページを開き、眠っていればボタンを押す。

起動確認は、Streamlitアプリ本体（iframe内の [data-testid="stApp"]）が
表示されるまで待って判定する。起動できなかった場合は終了コード1で失敗させ、
GitHub Actionsの失敗通知で気づけるようにする。
"""
import os
import re
import sys
import time

from playwright.sync_api import sync_playwright

APP_URL = os.environ.get("APP_URL", "https://byosho-tool-testver.streamlit.app")
WAKE_BUTTON = re.compile(r"get this app back up", re.I)
TIMEOUT_SEC = 240


def _app_loaded(page) -> bool:
    for frame in page.frames:
        try:
            if frame.locator('[data-testid="stApp"]').count():
                return True
        except Exception:
            pass
    return False


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(APP_URL, wait_until="domcontentloaded", timeout=60_000)

        woke = False
        deadline = time.time() + TIMEOUT_SEC
        while time.time() < deadline:
            if _app_loaded(page):
                print(f"OK: アプリは起動しています（{'スリープから起こしました' if woke else '起動中でした'}）")
                browser.close()
                return 0
            button = page.get_by_role("button", name=WAKE_BUTTON)
            if not woke and button.count():
                print("スリープ中だったため、起こすボタンを押します")
                button.first.click()
                woke = True
            page.wait_for_timeout(5_000)

        page.screenshot(path="keep_awake_failure.png", full_page=True)
        browser.close()
        print(f"NG: {TIMEOUT_SEC}秒待ってもアプリが表示されませんでした", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
