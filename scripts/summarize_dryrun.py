"""dry-run 用: fetch_videos.py の結果を中国語キーワード別に Markdown サマリ。

work/candidates.json を読み、_query フィールドが対象 5 中国語キーワードに
一致するものを抽出してクエリ別に上位 N 件をテーブル化する。

出力: logs/dryrun_cn_keywords.md

注意: fetch_videos.py が _query を候補 dict に含めている前提
(2026-05 のブランチ feat/dryrun-cn-keywords で導入)。
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CANDIDATES_PATH = ROOT / "work" / "candidates.json"
OUTPUT_PATH = ROOT / "logs" / "dryrun_cn_keywords.md"

# 観察対象の 5 中国語キーワード
CN_QUERIES = [
    "动物搞笑视频",
    "爆笑动物",
    "仓鼠搞笑",
    "搞笑动物合集",
    "野生动物 猎杀集锦",
]

TOP_N = 10


def main() -> int:
    if not CANDIDATES_PATH.exists():
        print(f"[summarize] FATAL: {CANDIDATES_PATH} が無い。fetch_videos.py を先に実行してください。")
        return 1

    with CANDIDATES_PATH.open("r", encoding="utf-8") as f:
        candidates = json.load(f)

    by_query: dict[str, list[dict]] = defaultdict(list)
    for c in candidates:
        q = c.get("_query")
        if q in CN_QUERIES:
            by_query[q].append(c)

    total_cn = sum(len(v) for v in by_query.values())

    lines: list[str] = []
    lines.append("# Pexels CN keywords dry-run summary")
    lines.append("")
    lines.append(
        f"`fetch_videos.py` 出力 (work/candidates.json) を 中国語クエリ別に集計。"
    )
    lines.append(
        f"全候補 **{len(candidates)}** 件中、CN キーワード分は **{total_cn}** 件。"
    )
    lines.append("")
    lines.append(
        "ヒット 0 のクエリは Pexels の内部 alias DB に該当 mapping が無い、"
        "または min_duration / min_width フィルタで全部落ちた可能性。"
    )
    lines.append("")

    for q in CN_QUERIES:
        hits = by_query.get(q, [])
        lines.append(f"## `{q}` — {len(hits)} hits (raw, before dedup/filter)")
        lines.append("")
        if not hits:
            lines.append("**ヒットなし。**")
            lines.append("")
            continue
        lines.append("| # | Title | Duration | Width | Uploader | Page URL |")
        lines.append("|---|---|---|---|---|---|")
        for i, h in enumerate(hits[:TOP_N], 1):
            title = (h.get("title") or "").replace("|", " ")[:80]
            duration = h.get("duration", 0)
            width = h.get("width", 0)
            uploader = (h.get("uploader") or "").replace("|", " ")[:30]
            page_url = h.get("page_url", "")
            lines.append(
                f"| {i} | {title} | {duration}s | {width} | {uploader} | {page_url} |"
            )
        lines.append("")

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"[summarize] wrote {OUTPUT_PATH} ({total_cn} CN candidates)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
