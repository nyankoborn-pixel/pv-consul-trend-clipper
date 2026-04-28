"""候補動画から1本を選定する。

work/candidates.json (fetch_videos.py 出力) を読み:
1. logs/video_posted.jsonl で既に動画化済みの video_id を除外
2. weight (高い順) → published_at (新しい順) でソート
3. トップ1件を work/selected.json に書き出す
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from dateutil import parser as date_parser

ROOT = Path(__file__).resolve().parent.parent
WORK_DIR = ROOT / "work"
LOGS_DIR = ROOT / "logs"
CANDIDATES_PATH = WORK_DIR / "candidates.json"
SELECTED_PATH = WORK_DIR / "selected.json"
POSTED_LOG_PATH = LOGS_DIR / "video_posted.jsonl"


def load_candidates() -> list[dict[str, Any]]:
    """work/candidates.json を読み込む。"""
    if not CANDIDATES_PATH.exists():
        print(f"[select] FATAL: {CANDIDATES_PATH} が存在しない。fetch_videos.py を先に実行する。")
        sys.exit(1)
    with CANDIDATES_PATH.open("r", encoding="utf-8") as f:
        candidates = json.load(f)
    print(f"[select] 候補動画 {len(candidates)} 件を読み込み")
    return candidates


def load_posted_video_ids() -> set[str]:
    """logs/video_posted.jsonl から動画化済み video_id 集合を読み込む。

    Returns:
        既に動画化された video_id の集合
    """
    if not POSTED_LOG_PATH.exists():
        print(f"[select] {POSTED_LOG_PATH} なし。初回実行扱い。")
        return set()

    posted: set[str] = set()
    with POSTED_LOG_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                print(f"[select] WARNING: jsonl パース失敗: {line[:80]}")
                continue
            vid = rec.get("video_id")
            if vid:
                posted.add(vid)
    print(f"[select] 動画化済み video_id: {len(posted)} 件")
    return posted


def exclude_posted(
    candidates: list[dict[str, Any]], posted: set[str]
) -> list[dict[str, Any]]:
    """既に動画化された video_id を除外する。"""
    fresh = [c for c in candidates if c.get("video_id") not in posted]
    print(f"[select] 既動画化を除外: {len(candidates)} → {len(fresh)} 件")
    return fresh


def sort_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """weight (降順) → published_at (降順) でソートする。"""

    def sort_key(c: dict[str, Any]) -> tuple[int, float]:
        weight = int(c.get("weight", 0))
        try:
            ts = date_parser.parse(c.get("published_at", "")).timestamp()
        except (ValueError, TypeError):
            ts = 0.0
        return (-weight, -ts)

    return sorted(candidates, key=sort_key)


def main() -> int:
    """メインエントリポイント。"""
    try:
        candidates = load_candidates()
    except Exception as exc:
        print(f"[select] FATAL: 候補読み込み失敗: {exc}")
        return 1

    if not candidates:
        print("[select] FATAL: 候補が 0 件")
        return 2

    posted = load_posted_video_ids()
    fresh = exclude_posted(candidates, posted)
    if not fresh:
        print("[select] FATAL: 全候補が既動画化済み。新規候補なし。")
        return 3

    sorted_list = sort_candidates(fresh)
    selected = sorted_list[0]

    print("[select] 選定:")
    print(f"  title       : {selected.get('title')}")
    print(f"  video_id    : {selected.get('video_id')}")
    print(f"  source_name : {selected.get('source_name')}")
    print(f"  weight      : {selected.get('weight')}")
    print(f"  published_at: {selected.get('published_at')}")
    print(f"  url         : {selected.get('url')}")

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    with SELECTED_PATH.open("w", encoding="utf-8") as f:
        json.dump(selected, f, ensure_ascii=False, indent=2)
    print(f"[select] 出力: {SELECTED_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
