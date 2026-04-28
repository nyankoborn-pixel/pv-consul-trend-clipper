"""完成した output.mp4 を YouTube Shorts として投稿する。

work/script.json からタイトル・出典情報を取得し、
output.mp4 を YouTube Data API v3 (videos.insert) でアップロードする。

OAuth は環境変数 (CLIENT_ID / CLIENT_SECRET / REFRESH_TOKEN) から構築。
カテゴリ ID 28 (Science & Technology)、概要欄に出典を必ず明記。

dry_run=true (env or argv) の場合はアップロードをスキップする。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

ROOT = Path(__file__).resolve().parent.parent
WORK_DIR = ROOT / "work"
LOGS_DIR = ROOT / "logs"
SCRIPT_PATH = WORK_DIR / "script.json"
OUTPUT_VIDEO = ROOT / "output.mp4"
UPLOAD_LOG_PATH = LOGS_DIR / "youtube_uploaded.jsonl"
POSTED_LOG_PATH = LOGS_DIR / "video_posted.jsonl"

CLIENT_ID = os.environ.get("YOUTUBE_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("YOUTUBE_CLIENT_SECRET", "")
REFRESH_TOKEN = os.environ.get("YOUTUBE_REFRESH_TOKEN", "")

CATEGORY_ID = "28"  # Science & Technology
DEFAULT_TAGS = [
    "Shorts", "衝撃映像", "ニュース", "NASA", "宇宙",
    "ニャンコンサル", "ずんだもん",
]

DESCRIPTION_TEMPLATE = """{title}

執事猫ニャンコンサルとずんだもんが、世界の驚き映像をゆるく解説。

▼ 元映像
{original_title}
{video_url}
出典: {source_name}

▼ 内容について
・公的機関 (NASA / SpaceX / USGS / NOAA / 米軍 など) や CC BY ライセンス映像をベースに、独自の解説を加えた二次創作です
・元映像の音声はカットし、当チャンネル独自の解説と音声を被せています

▼ 制作
ニュース解説: AI支援で制作
音声合成: VOICEVOX (ずんだもん、青山龍星)

#Shorts #衝撃映像 #ニュース #NASA #宇宙 #ニャンコンサル #ずんだもん
"""


def load_script() -> dict[str, Any]:
    """work/script.json を読み込む。"""
    if not SCRIPT_PATH.exists():
        print(f"[upload] FATAL: {SCRIPT_PATH} が存在しない。")
        sys.exit(1)
    with SCRIPT_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def is_dry_run() -> bool:
    """dry_run フラグの判定。env DRY_RUN または argv に --dry-run。"""
    if "--dry-run" in sys.argv:
        return True
    val = os.environ.get("DRY_RUN", "").strip().lower()
    return val in ("1", "true", "yes")


def build_credentials() -> Credentials:
    """OAuth 認証情報を refresh token から構築する。"""
    if not (CLIENT_ID and CLIENT_SECRET and REFRESH_TOKEN):
        raise RuntimeError(
            "YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET / YOUTUBE_REFRESH_TOKEN が未設定"
        )
    creds = Credentials(
        token=None,
        refresh_token=REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )
    print("[upload] アクセストークンを refresh ...")
    creds.refresh(Request())
    return creds


def build_description(script: dict[str, Any]) -> str:
    """概要欄テキストを生成する。"""
    meta = script["_meta"]
    return DESCRIPTION_TEMPLATE.format(
        title=script["title"],
        original_title=meta.get("original_title", ""),
        video_url=meta.get("video_url", ""),
        source_name=meta.get("source_name", ""),
    )


def append_log(path: Path, record: dict[str, Any]) -> None:
    """jsonl ログに 1 行追記する。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def upload(script: dict[str, Any]) -> str:
    """YouTube に動画をアップロードする。

    Returns:
        投稿された YouTube video_id
    """
    if not OUTPUT_VIDEO.exists():
        raise RuntimeError(f"動画ファイルが存在しない: {OUTPUT_VIDEO}")

    creds = build_credentials()
    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)

    title = script["title"]
    description = build_description(script)
    print(f"[upload] アップロード開始: title='{title}'")

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": DEFAULT_TAGS,
            "categoryId": CATEGORY_ID,
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(
        str(OUTPUT_VIDEO),
        mimetype="video/mp4",
        resumable=True,
        chunksize=8 * 1024 * 1024,
    )
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                pct = int(status.progress() * 100)
                print(f"[upload] 進捗: {pct}%")
        except HttpError as exc:
            print(f"[upload] FATAL: HttpError: {exc}")
            raise

    posted_id = response.get("id", "")
    print(f"[upload] 完了: https://www.youtube.com/watch?v={posted_id}")
    return posted_id


def main() -> int:
    """メインエントリポイント。"""
    try:
        script = load_script()
    except Exception as exc:
        print(f"[upload] FATAL: script 読み込み失敗: {exc}")
        return 1

    meta = script["_meta"]
    source_video_id = meta.get("video_id", "")

    if is_dry_run():
        print("[upload] DRY RUN: アップロードはスキップ")
        print("[upload] --- 投稿予定タイトル ---")
        print(script["title"])
        print("[upload] --- 概要欄プレビュー ---")
        print(build_description(script))
        # dry_run でも生成済み履歴は残さない (未投稿のため)
        return 0

    try:
        posted_id = upload(script)
    except Exception as exc:
        print(f"[upload] FATAL: アップロード失敗: {exc}")
        return 2

    now = datetime.now(timezone.utc).isoformat()
    upload_record = {
        "uploaded_at": now,
        "youtube_video_id": posted_id,
        "youtube_url": f"https://www.youtube.com/watch?v={posted_id}",
        "title": script["title"],
        "source_video_id": source_video_id,
        "source_name": meta.get("source_name"),
        "source_url": meta.get("video_url"),
    }
    append_log(UPLOAD_LOG_PATH, upload_record)
    print(f"[upload] log 追記: {UPLOAD_LOG_PATH}")

    posted_record = {
        "posted_at": now,
        "video_id": source_video_id,
        "youtube_video_id": posted_id,
    }
    append_log(POSTED_LOG_PATH, posted_record)
    print(f"[upload] log 追記: {POSTED_LOG_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
