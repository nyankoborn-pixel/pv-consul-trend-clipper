"""候補動画から1本を選定する。

work/candidates.json (fetch_videos.py 出力) を読み:
1. logs/video_posted.jsonl ∪ logs/video_selected.jsonl で既出 video_id を除外
2. weight (高い順) → published_at (新しい順) でソート
   published_at が無いソース (Pixabay/Pexels 等) は同 weight 内で random shuffle
3. トップ1件を work/selected.json に書き出す
4. 選定した video_id を logs/video_selected.jsonl に追記
   (dry_run でも記録されるので連続テスト時にソースが重複しない)

video_posted.jsonl は upload_youtube.py が投稿成功時に書く永続ログ。
video_selected.jsonl は本ファイルが「選定した時点」で書く軽いログ。
両方を OR で参照することで、本番投稿でも dry_run でも重複回避が機能する。
"""

from __future__ import annotations

import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dateutil import parser as date_parser

ROOT = Path(__file__).resolve().parent.parent
WORK_DIR = ROOT / "work"
LOGS_DIR = ROOT / "logs"
CANDIDATES_PATH = WORK_DIR / "candidates.json"
SELECTED_PATH = WORK_DIR / "selected.json"
POSTED_LOG_PATH = LOGS_DIR / "video_posted.jsonl"
SELECTED_LOG_PATH = LOGS_DIR / "video_selected.jsonl"


def load_candidates() -> list[dict[str, Any]]:
    """work/candidates.json を読み込む。"""
    if not CANDIDATES_PATH.exists():
        print(f"[select] FATAL: {CANDIDATES_PATH} が存在しない。fetch_videos.py を先に実行する。")
        sys.exit(1)
    with CANDIDATES_PATH.open("r", encoding="utf-8") as f:
        candidates = json.load(f)
    print(f"[select] 候補動画 {len(candidates)} 件を読み込み")
    return candidates


def _read_jsonl_video_ids(path: Path) -> set[str]:
    """jsonl ファイルから 'video_id' フィールドを集合で読み込む (空ファイル / 無ければ空集合)。"""
    ids: set[str] = set()
    if not path.exists():
        return ids
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                print(f"[select] WARNING: {path.name} jsonl パース失敗: {line[:80]}")
                continue
            vid = rec.get("video_id")
            if vid:
                ids.add(vid)
    return ids


def load_seen_video_ids() -> set[str]:
    """既出 video_id 集合 (posted ∪ selected) を読み込む。

    - posted: upload_youtube.py が投稿成功時に書く永続ログ
    - selected: select_video.py が選定時に書く軽いログ
      (dry_run でも記録され、連続テストでもソースが重複しない)

    Returns:
        既に投稿済 or 選定済の video_id 集合
    """
    posted = _read_jsonl_video_ids(POSTED_LOG_PATH)
    selected = _read_jsonl_video_ids(SELECTED_LOG_PATH)
    seen = posted | selected
    print(
        f"[select] 既出 video_id: posted={len(posted)} 件 / "
        f"selected={len(selected)} 件 / 合算 {len(seen)} 件"
    )
    return seen


def exclude_posted(
    candidates: list[dict[str, Any]], seen: set[str]
) -> list[dict[str, Any]]:
    """既出 video_id を除外する。"""
    fresh = [c for c in candidates if c.get("video_id") not in seen]
    print(f"[select] 既出除外: {len(candidates)} → {len(fresh)} 件")
    return fresh


def record_selection(selected: dict[str, Any]) -> None:
    """選定した video_id を logs/video_selected.jsonl に追記する。"""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "selected_at": datetime.now(timezone.utc).isoformat(),
        "video_id": selected.get("video_id"),
        "title": selected.get("title"),
        "source_name": selected.get("source_name"),
        "source_type": selected.get("source_type"),
        "page_url": selected.get("page_url"),
    }
    with SELECTED_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"[select] 重複防止ログ追記: {SELECTED_LOG_PATH}")


def sort_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """weight (降順) → published_at (降順) でソートする。

    published_at が None / 取得不可の場合は 0 扱い。
    最終的に同 weight & 同 published_at の中で variety を確保するため shuffle した上で
    安定ソートする (Python の sorted は安定なので、shuffle 結果のうちキー一致グループは
    元順序を維持する → 結果として同条件内のみがランダム化される)。
    """
    shuffled = list(candidates)
    random.shuffle(shuffled)

    def sort_key(c: dict[str, Any]) -> tuple[int, float]:
        weight = int(c.get("weight", 0))
        pub = c.get("published_at")
        ts = 0.0
        if pub:
            try:
                ts = date_parser.parse(str(pub)).timestamp()
            except (ValueError, TypeError):
                ts = 0.0
        return (-weight, -ts)

    return sorted(shuffled, key=sort_key)


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

    seen = load_seen_video_ids()
    fresh = exclude_posted(candidates, seen)
    if not fresh:
        print(
            "[select] FATAL: 全候補が既出 (posted ∪ selected)。新規候補なし。\n"
            "         logs/video_selected.jsonl をクリアするか、"
            "ソース queries を変えて新規候補を入れてください。"
        )
        return 3

    sorted_list = sort_candidates(fresh)
    selected = sorted_list[0]

    print("[select] 選定:")
    print(f"  title       : {selected.get('title')}")
    print(f"  video_id    : {selected.get('video_id')}")
    print(f"  source_name : {selected.get('source_name')}")
    print(f"  weight      : {selected.get('weight')}")
    print(f"  published_at: {selected.get('published_at')}")
    print(f"  page_url    : {selected.get('page_url')}")
    print(f"  media_url   : {selected.get('media_url')}")
    print(f"  duration    : {selected.get('duration')}s")

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    with SELECTED_PATH.open("w", encoding="utf-8") as f:
        json.dump(selected, f, ensure_ascii=False, indent=2)
    print(f"[select] 出力: {SELECTED_PATH}")

    # 選定ログに追記 (dry_run でも記録 → 次回以降で同一ソースを避ける)
    record_selection(selected)
    return 0


if __name__ == "__main__":
    sys.exit(main())
