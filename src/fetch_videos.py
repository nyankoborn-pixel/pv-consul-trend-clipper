"""動画ソースから候補を収集する。

config/sources.yml の定義に従って:
- youtube_channel タイプ : チャンネル RSS から最新動画を取得
- search タイプ          : YouTube Data API search.list で検索
を実行し、過去 recent_hours 以内の動画リストを構造化して
work/candidates.json に書き出す。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import feedparser
import requests
import yaml
from dateutil import parser as date_parser

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "sources.yml"
WORK_DIR = ROOT / "work"
OUTPUT_PATH = WORK_DIR / "candidates.json"

YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
YOUTUBE_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
RSS_URL_TEMPLATE = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"


def load_config() -> dict[str, Any]:
    """config/sources.yml を読み込んで辞書として返す。"""
    print(f"[fetch] config を読み込み: {CONFIG_PATH}")
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def fetch_from_channel_rss(
    channel_id: str, source_name: str, weight: int, max_results: int
) -> list[dict[str, Any]]:
    """YouTube チャンネル RSS から最新動画を取得する。

    Args:
        channel_id: YouTube チャンネル ID
        source_name: ソース表示名
        weight: 選定優先度
        max_results: 最大取得件数 (RSS は仕様上 15 件まで)

    Returns:
        動画情報の辞書リスト
    """
    url = RSS_URL_TEMPLATE.format(channel_id=channel_id)
    print(f"[fetch] RSS取得: {source_name} ({url})")

    feed = feedparser.parse(url)
    if feed.bozo and not feed.entries:
        print(f"[fetch] WARNING: RSS取得失敗 source={source_name}: {feed.bozo_exception}")
        return []

    results: list[dict[str, Any]] = []
    for entry in feed.entries[:max_results]:
        video_id = entry.get("yt_videoid") or entry.get("id", "").split(":")[-1]
        if not video_id:
            continue
        published_iso = entry.get("published", "")
        try:
            published_dt = date_parser.parse(published_iso)
        except (ValueError, TypeError):
            print(f"[fetch] WARNING: 投稿日時パース失敗 video_id={video_id}")
            continue

        results.append({
            "video_id": video_id,
            "title": entry.get("title", ""),
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "published_at": published_dt.astimezone(timezone.utc).isoformat(),
            "source_name": source_name,
            "source_type": "youtube_channel",
            "channel_id": channel_id,
            "weight": weight,
            "license": "channel_default",
        })

    print(f"[fetch] {source_name}: {len(results)} 件取得")
    return results


def fetch_from_search(
    query: str, license_type: str, source_name: str, weight: int, max_results: int
) -> list[dict[str, Any]]:
    """YouTube Data API search.list で検索する。

    Args:
        query: 検索クエリ
        license_type: ライセンス絞り込み (例: "creativeCommon")
        source_name: ソース表示名
        weight: 選定優先度
        max_results: 最大取得件数

    Returns:
        動画情報の辞書リスト
    """
    if not YOUTUBE_API_KEY:
        print(f"[fetch] WARNING: YOUTUBE_API_KEY 未設定のため search スキップ: {source_name}")
        return []

    print(f"[fetch] 検索: {source_name} q='{query}' license={license_type}")

    params = {
        "key": YOUTUBE_API_KEY,
        "part": "snippet",
        "type": "video",
        "q": query,
        "videoLicense": license_type,
        "order": "date",
        "maxResults": max_results,
        "regionCode": "US",
        "relevanceLanguage": "en",
    }

    try:
        resp = requests.get(YOUTUBE_SEARCH_URL, params=params, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"[fetch] ERROR: search.list 失敗 source={source_name}: {exc}")
        return []

    items = resp.json().get("items", [])
    results: list[dict[str, Any]] = []
    for item in items:
        snippet = item.get("snippet", {})
        video_id = item.get("id", {}).get("videoId")
        if not video_id:
            continue
        try:
            published_dt = date_parser.parse(snippet.get("publishedAt", ""))
        except (ValueError, TypeError):
            continue

        results.append({
            "video_id": video_id,
            "title": snippet.get("title", ""),
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "published_at": published_dt.astimezone(timezone.utc).isoformat(),
            "source_name": source_name,
            "source_type": "search",
            "channel_id": snippet.get("channelId", ""),
            "weight": weight,
            "license": license_type,
        })

    print(f"[fetch] {source_name}: {len(results)} 件取得")
    return results


def filter_recent(videos: list[dict[str, Any]], recent_hours: int) -> list[dict[str, Any]]:
    """過去 recent_hours 以内に投稿された動画のみ残す。"""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=recent_hours)
    filtered: list[dict[str, Any]] = []
    for v in videos:
        try:
            pub = date_parser.parse(v["published_at"])
        except (ValueError, TypeError, KeyError):
            continue
        if pub >= cutoff:
            filtered.append(v)
    print(f"[fetch] 過去 {recent_hours}h フィルタ: {len(videos)} → {len(filtered)} 件")
    return filtered


def deduplicate(videos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一 video_id を排除する (先に出てきた方を残す)。"""
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for v in videos:
        vid = v.get("video_id")
        if not vid or vid in seen:
            continue
        seen.add(vid)
        deduped.append(v)
    print(f"[fetch] 重複排除: {len(videos)} → {len(deduped)} 件")
    return deduped


def main() -> int:
    """メインエントリポイント。"""
    try:
        config = load_config()
    except Exception as exc:
        print(f"[fetch] FATAL: config 読み込み失敗: {exc}")
        return 1

    fetch_cfg = config.get("fetch", {})
    recent_hours: int = fetch_cfg.get("recent_hours", 48)
    search_max: int = fetch_cfg.get("search_max_results", 10)
    channel_max: int = fetch_cfg.get("channel_max_results", 15)

    all_videos: list[dict[str, Any]] = []
    for src in config.get("sources", []):
        src_type = src.get("type")
        name = src.get("name", "(unnamed)")
        weight = int(src.get("weight", 1))

        try:
            if src_type == "youtube_channel":
                channel_id = src.get("channel_id", "")
                if not channel_id:
                    print(f"[fetch] WARNING: channel_id 未指定: {name}")
                    continue
                all_videos.extend(
                    fetch_from_channel_rss(channel_id, name, weight, channel_max)
                )
            elif src_type == "search":
                all_videos.extend(
                    fetch_from_search(
                        src.get("query", ""),
                        src.get("license", "creativeCommon"),
                        name,
                        weight,
                        search_max,
                    )
                )
            else:
                print(f"[fetch] WARNING: 未知の type={src_type} (source={name})")
        except Exception as exc:
            print(f"[fetch] ERROR: source={name} 取得中に例外: {exc}")
            continue

    print(f"[fetch] 全ソース取得完了: 計 {len(all_videos)} 件")

    deduped = deduplicate(all_videos)
    recent = filter_recent(deduped, recent_hours)

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(recent, f, ensure_ascii=False, indent=2)

    print(f"[fetch] 出力: {OUTPUT_PATH} ({len(recent)} 件)")
    if not recent:
        print("[fetch] FATAL: 候補動画が 0 件。後続処理を停止する。")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
