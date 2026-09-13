"""会員コミュニティ掲示板（質問／要望・不具合報告の投稿・返信）。

data/community_posts.json に投稿・返信を、data/community_nicknames.json に
ニックネーム（メールアドレス→表示名）を保存する。どちらも.gitignore対象で、
VPS上で初回投稿・初回ニックネーム設定時に自動生成される。

同時書き込みによる破損を防ぐため、auth.py の config_lock() と同じ
fcntl排他ロックのパターンを踏襲している（別プロセス・別セッションからの
同時書き込みで片方がファイルを空に切り詰めてしまう事故を防ぐ）。
"""
import fcntl
import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

POSTS_PATH = "data/community_posts.json"
NICKNAMES_PATH = "data/community_nicknames.json"
_LOCK_PATH = "data/community.lock"
_JST = timezone(timedelta(hours=9))

CATEGORIES = {"question": "❓ 質問", "request": "📝 要望・不具合"}


@contextmanager
def _lock():
    os.makedirs(os.path.dirname(_LOCK_PATH), exist_ok=True)
    with open(_LOCK_PATH, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _read_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return default


def _write_json(path, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def get_nickname(email: str) -> str:
    """未設定の場合はメールアドレスのローカル部（@より前）を返す。"""
    with _lock():
        nicknames = _read_json(NICKNAMES_PATH, {})
    return nicknames.get(email) or (email.split("@")[0] if email else "匿名")


def set_nickname(email: str, nickname: str) -> None:
    with _lock():
        nicknames = _read_json(NICKNAMES_PATH, {})
        nicknames[email] = nickname
        _write_json(NICKNAMES_PATH, nicknames)


def load_posts() -> list[dict]:
    """新しい投稿が先頭に来る順で返す。"""
    with _lock():
        posts = _read_json(POSTS_PATH, [])
    return sorted(posts, key=lambda p: p.get("created_at", ""), reverse=True)


def add_post(email: str, nickname: str, category: str, body: str) -> None:
    with _lock():
        posts = _read_json(POSTS_PATH, [])
        posts.append({
            "id": uuid.uuid4().hex,
            "category": category,
            "email": email,
            "nickname": nickname,
            "body": body,
            "created_at": datetime.now(_JST).isoformat(),
            "replies": [],
        })
        _write_json(POSTS_PATH, posts)


def add_reply(post_id: str, email: str, nickname: str, body: str) -> bool:
    """該当する投稿に返信を追加する。投稿が見つからなければFalseを返す。"""
    with _lock():
        posts = _read_json(POSTS_PATH, [])
        for post in posts:
            if post.get("id") == post_id:
                post.setdefault("replies", []).append({
                    "id": uuid.uuid4().hex,
                    "email": email,
                    "nickname": nickname,
                    "body": body,
                    "created_at": datetime.now(_JST).isoformat(),
                })
                _write_json(POSTS_PATH, posts)
                return True
        return False


def format_timestamp(iso_str: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_str)
    except (ValueError, TypeError):
        return ""
    return dt.strftime("%Y-%m-%d %H:%M")
