"""動画ソースから候補を収集する (YouTube 完全廃止版)。

config/sources.yml の type に応じて以下から MP4 直リンク付きで候補を収集:
  pixabay          : Pixabay Video API
  pexels           : Pexels Video API
  nasa             : NASA Image and Video Library
  usgs_volcano     : USGS Volcano Hazards multimedia (BS4 スクレイピング、best-effort)
  internet_archive : Internet Archive advancedsearch + metadata API

各候補は media_url (直 MP4) と page_url (出典ページ) を持つ。
出力: work/candidates.json
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "sources.yml"
WORK_DIR = ROOT / "work"
OUTPUT_PATH = WORK_DIR / "candidates.json"

PIXABAY_API_KEY = os.environ.get("PIXABAY_API_KEY", "")
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "")
NASA_API_KEY = os.environ.get("NASA_API_KEY", "DEMO_KEY")  # NASA は鍵なしでも DEMO_KEY で動く

DEFAULT_HEADERS = {
    "User-Agent": "pv-consul-trend-clipper/1.0 (+https://github.com/)"
}


def load_config() -> dict[str, Any]:
    """config/sources.yml を読み込んで辞書として返す。"""
    print(f"[fetch] config を読み込み: {CONFIG_PATH}")
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ----- Pixabay ---------------------------------------------------------------


def fetch_pixabay(
    queries: list[str],
    weight: int,
    authority: str,
    license_name: str,
    source_name: str,
    per_query: int,
    timeout: int,
    min_duration: int,
    min_width: int,
) -> list[dict[str, Any]]:
    """Pixabay Video API から候補を取得する。

    Pixabay API は min_width/min_height パラメータをサポートしているので
    API 段階で低解像度素材を弾く。さらに取得後にも duration / 各 size の
    実 width をチェックして 1080x1920 縦化に耐える素材のみ残す。
    """
    if not PIXABAY_API_KEY:
        print(f"[fetch] WARNING: PIXABAY_API_KEY 未設定。{source_name} スキップ")
        return []

    results: list[dict[str, Any]] = []
    for q in queries:
        params = {
            "key": PIXABAY_API_KEY,
            "q": q,
            "video_type": "film",
            "per_page": per_query,
            "safesearch": "true",
            # API 直接フィルタ: 縦動画 1080 幅に耐える素材のみ
            "min_width": min_width,
        }
        try:
            r = requests.get(
                "https://pixabay.com/api/videos/",
                params=params,
                headers=DEFAULT_HEADERS,
                timeout=timeout,
            )
            r.raise_for_status()
        except requests.RequestException as exc:
            print(f"[fetch] ERROR: Pixabay q='{q}' 失敗: {exc}")
            continue

        for hit in r.json().get("hits", []):
            duration = int(hit.get("duration", 0))
            if duration < min_duration:
                continue
            videos = hit.get("videos", {}) or {}
            # 優先順: large > medium > small > tiny
            # 各 size には width/height が含まれるので、min_width を満たす最小 size を選ぶ
            media_url = ""
            chosen_w = 0
            for size in ("large", "medium", "small", "tiny"):
                v = videos.get(size, {})
                if not isinstance(v, dict):
                    continue
                v_url = v.get("url")
                v_w = int(v.get("width") or 0)
                if v_url and v_w >= min_width:
                    media_url = v_url
                    chosen_w = v_w
                    break
            if not media_url:
                continue
            vid = hit.get("id")
            results.append({
                "video_id": f"pixabay_{vid}",
                "title": (hit.get("tags") or "").strip() or f"Pixabay video {vid}",
                "description": (hit.get("tags") or "")[:500],
                "page_url": hit.get("pageURL", f"https://pixabay.com/videos/id-{vid}/"),
                "media_url": media_url,
                "duration": duration,
                "width": chosen_w,
                "published_at": None,
                "source_name": source_name,
                "source_type": "pixabay",
                "authority_intro": authority,
                "license": license_name,
                "weight": weight,
                "uploader": hit.get("user", "Pixabay user"),
            })
        print(f"[fetch] Pixabay q='{q}': 累計 {len(results)} 件")
    return results


# ----- Pexels ----------------------------------------------------------------


def fetch_pexels(
    queries: list[str],
    weight: int,
    authority: str,
    license_name: str,
    source_name: str,
    per_query: int,
    timeout: int,
    min_duration: int,
    min_width: int,
) -> list[dict[str, Any]]:
    """Pexels Video API から候補を取得する。

    Pexels API は size パラメータで `large` (4K)/`medium` (FullHD)/`small` (HD)
    を切替可能。size=large を指定して取得し、video_files の中から
    min_width 以上 & 1440p 以下を選ぶ (極端な 4K は避ける、CDN 帯域節約)。
    duration / width 両方を取得後にも検証して品質を担保。
    """
    if not PEXELS_API_KEY:
        print(f"[fetch] WARNING: PEXELS_API_KEY 未設定。{source_name} スキップ")
        return []

    headers = {**DEFAULT_HEADERS, "Authorization": PEXELS_API_KEY}
    results: list[dict[str, Any]] = []
    for q in queries:
        params = {
            "query": q,
            "per_page": per_query,
            # size=large は 4K 含む高解像度を返す。FullHD 以上が欲しいので large を指定し、
            # video_files の中から min_width 以上 & 1440p 以下を pick する
            "size": "large",
        }
        try:
            r = requests.get(
                "https://api.pexels.com/videos/search",
                params=params,
                headers=headers,
                timeout=timeout,
            )
            r.raise_for_status()
        except requests.RequestException as exc:
            print(f"[fetch] ERROR: Pexels q='{q}' 失敗: {exc}")
            continue

        for vid in r.json().get("videos", []):
            duration = int(vid.get("duration", 0))
            if duration < min_duration:
                continue
            # video_files から min_width を満たす最高品質 (ただし 1440p 以下) を選ぶ
            files = vid.get("video_files", []) or []

            def quality_key(f: dict[str, Any]) -> tuple[int, int]:
                # 4K 等は最後に回し、min_width〜1440p を優先
                h = int(f.get("height") or 0)
                if h > 1440:
                    return (0, h)
                return (1, h)

            mp4_files = [
                f for f in files
                if (f.get("file_type") == "video/mp4")
                and f.get("link")
                and int(f.get("width") or 0) >= min_width
            ]
            mp4_files.sort(key=quality_key, reverse=True)
            if not mp4_files:
                # min_width を満たすファイルが video_files に無い → スキップ
                continue
            chosen = mp4_files[0]
            media_url = chosen["link"]
            chosen_w = int(chosen.get("width") or 0)
            user = (vid.get("user") or {}).get("name", "Pexels user")
            results.append({
                "video_id": f"pexels_{vid.get('id')}",
                "title": (vid.get("url") or "").rstrip("/").split("/")[-1].replace("-", " ")
                         or f"Pexels video {vid.get('id')}",
                "description": "",
                "page_url": vid.get("url", ""),
                "media_url": media_url,
                "duration": duration,
                "width": chosen_w,
                "published_at": None,
                "source_name": source_name,
                "source_type": "pexels",
                "authority_intro": authority,
                "license": license_name,
                "weight": weight,
                "uploader": user,
            })
        print(f"[fetch] Pexels q='{q}': 累計 {len(results)} 件")
    return results


# ----- NASA Image and Video Library -----------------------------------------


def fetch_nasa(
    queries: list[str],
    weight: int,
    authority: str,
    license_name: str,
    source_name: str,
    per_query: int,
    timeout: int,
    min_duration: int,
) -> list[dict[str, Any]]:
    """NASA Image and Video Library から候補を取得する。

    検索 API → アイテムごとの collection.json を取りに行って .mp4 を探す 2 段構え。
    """
    results: list[dict[str, Any]] = []
    for q in queries:
        params = {"q": q, "media_type": "video", "page_size": per_query}
        try:
            r = requests.get(
                "https://images-api.nasa.gov/search",
                params=params,
                headers=DEFAULT_HEADERS,
                timeout=timeout,
            )
            r.raise_for_status()
        except requests.RequestException as exc:
            print(f"[fetch] ERROR: NASA q='{q}' 失敗: {exc}")
            continue

        items = r.json().get("collection", {}).get("items", [])
        for item in items:
            href = item.get("href")
            data = (item.get("data") or [{}])[0]
            nasa_id = data.get("nasa_id")
            if not href or not nasa_id:
                continue
            # アイテムの collection.json を取得して mp4 を探す
            try:
                cr = requests.get(href, headers=DEFAULT_HEADERS, timeout=timeout)
                cr.raise_for_status()
                files = cr.json()
            except (requests.RequestException, ValueError) as exc:
                print(f"[fetch] WARNING: NASA item {nasa_id} の files 取得失敗: {exc}")
                continue
            if not isinstance(files, list):
                continue

            # 優先順: large > medium > orig > small
            mp4_url = ""
            for tag in ("~large.mp4", "~medium.mp4", "~orig.mp4", "~small.mp4"):
                for f in files:
                    if isinstance(f, str) and f.endswith(tag):
                        mp4_url = f
                        break
                if mp4_url:
                    break
            if not mp4_url:
                # フォールバック: 最初の .mp4
                for f in files:
                    if isinstance(f, str) and f.endswith(".mp4"):
                        mp4_url = f
                        break
            if not mp4_url:
                continue

            results.append({
                "video_id": f"nasa_{nasa_id}",
                "title": data.get("title", "") or f"NASA video {nasa_id}",
                "description": (data.get("description") or "")[:1500],
                "page_url": f"https://images.nasa.gov/details/{nasa_id}",
                "media_url": mp4_url,
                "duration": 0,  # NASA メタは duration を持たないことが多い
                "published_at": data.get("date_created"),
                "source_name": source_name,
                "source_type": "nasa",
                "authority_intro": authority,
                "license": license_name,
                "weight": weight,
                "uploader": data.get("center", "NASA"),
            })
        print(f"[fetch] NASA q='{q}': 累計 {len(results)} 件")
    return results


# ----- USGS Volcano Hazards (best-effort scraping) --------------------------


def fetch_usgs_volcano(
    weight: int,
    authority: str,
    license_name: str,
    source_name: str,
    timeout: int,
    min_duration: int,
) -> list[dict[str, Any]]:
    """USGS の火山関連ページから .mp4 リンクを best-effort でスクレイピングする。

    USGS は公式 API を持たないので、知られた多媒体ページを巡回し
    a[href$=".mp4"] / source[src$=".mp4"] / video[src$=".mp4"] を抽出する。
    1 件も見つからなければ空配列を返す (パイプラインは落とさない)。
    """
    seed_urls = [
        "https://www.usgs.gov/programs/VHP/volcano-multimedia",
        "https://www.usgs.gov/media/videos",
        "https://volcanoes.usgs.gov/vhp/multimedia.html",
    ]
    found: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for seed in seed_urls:
        try:
            r = requests.get(seed, headers=DEFAULT_HEADERS, timeout=timeout)
            if not r.ok:
                continue
        except requests.RequestException:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        # 直接 mp4 リンクを探す
        for tag in soup.find_all(["a", "source", "video"]):
            href = tag.get("href") or tag.get("src") or ""
            if not href.lower().endswith(".mp4"):
                continue
            if href.startswith("/"):
                href = "https://www.usgs.gov" + href
            if href in seen_urls:
                continue
            seen_urls.add(href)
            title = (tag.get("title") or tag.get_text(strip=True)
                     or href.rsplit("/", 1)[-1])
            found.append({
                "video_id": f"usgs_{abs(hash(href)) % (10**10)}",
                "title": title or "USGS volcano video",
                "description": "",
                "page_url": seed,
                "media_url": href,
                "duration": 0,
                "published_at": None,
                "source_name": source_name,
                "source_type": "usgs_volcano",
                "authority_intro": authority,
                "license": license_name,
                "weight": weight,
                "uploader": "USGS",
            })
    print(f"[fetch] USGS Volcano: 抽出 {len(found)} 件 (best-effort)")
    return found


# ----- Internet Archive ------------------------------------------------------


def fetch_internet_archive(
    queries: list[str],
    weight: int,
    authority: str,
    license_name: str,
    source_name: str,
    per_query: int,
    timeout: int,
    min_duration: int,
) -> list[dict[str, Any]]:
    """Internet Archive から動画候補を取得する。

    advancedsearch.php で mediatype:movies の identifier 一覧を取り、
    metadata API で実ファイル名を引いて download URL を組み立てる。
    """
    results: list[dict[str, Any]] = []
    for q in queries:
        params = {
            "q": f"({q}) AND mediatype:movies",
            "fl[]": ["identifier", "title", "description", "date", "downloads"],
            "sort[]": "downloads desc",
            "rows": per_query,
            "output": "json",
        }
        try:
            r = requests.get(
                "https://archive.org/advancedsearch.php",
                params=params,
                headers=DEFAULT_HEADERS,
                timeout=timeout,
            )
            r.raise_for_status()
        except requests.RequestException as exc:
            print(f"[fetch] ERROR: IA q='{q}' 失敗: {exc}")
            continue

        docs = r.json().get("response", {}).get("docs", [])
        for doc in docs:
            ident = doc.get("identifier")
            if not ident:
                continue
            try:
                mr = requests.get(
                    f"https://archive.org/metadata/{ident}",
                    headers=DEFAULT_HEADERS,
                    timeout=timeout,
                )
                mr.raise_for_status()
                meta_files = mr.json().get("files", [])
            except (requests.RequestException, ValueError) as exc:
                print(f"[fetch] WARNING: IA {ident} metadata 失敗: {exc}")
                continue

            mp4_name = ""
            for f in meta_files:
                if not isinstance(f, dict):
                    continue
                name = f.get("name", "")
                fmt = (f.get("format") or "").lower()
                if name.lower().endswith(".mp4") or "h.264" in fmt or "mp4" in fmt:
                    mp4_name = name
                    break
            if not mp4_name:
                continue

            # description は時に list (multi-paragraph) で来る
            desc = doc.get("description") or ""
            if isinstance(desc, list):
                desc = " ".join(str(x) for x in desc)
            desc = str(desc)[:1500]

            results.append({
                "video_id": f"ia_{ident}",
                "title": str(doc.get("title", "") or f"Internet Archive {ident}"),
                "description": desc,
                "page_url": f"https://archive.org/details/{ident}",
                "media_url": f"https://archive.org/download/{ident}/{mp4_name}",
                "duration": 0,
                "published_at": doc.get("date"),
                "source_name": source_name,
                "source_type": "internet_archive",
                "authority_intro": authority,
                "license": license_name,
                "weight": weight,
                "uploader": "Internet Archive",
            })
        print(f"[fetch] Internet Archive q='{q}': 累計 {len(results)} 件")
    return results


# ----- 共通 ------------------------------------------------------------------


def deduplicate(videos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一 video_id を排除する。"""
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

    fetch_cfg = config.get("fetch", {}) or {}
    per_query: int = int(fetch_cfg.get("per_query_results", 6))
    timeout: int = int(fetch_cfg.get("request_timeout", 30))
    min_duration: int = int(fetch_cfg.get("min_duration", 15))
    min_width: int = int(fetch_cfg.get("min_width", 1080))

    all_videos: list[dict[str, Any]] = []
    for src in config.get("sources", []):
        src_type = src.get("type")
        name = src.get("name", "(unnamed)")
        weight = int(src.get("weight", 1))
        authority = src.get("authority_intro", "")
        license_name = src.get("license", "")
        queries = src.get("queries", []) or []

        print(f"[fetch] === {name} ({src_type}) ===")
        try:
            if src_type == "pixabay":
                got = fetch_pixabay(
                    queries, weight, authority, license_name, name,
                    per_query, timeout, min_duration, min_width,
                )
            elif src_type == "pexels":
                got = fetch_pexels(
                    queries, weight, authority, license_name, name,
                    per_query, timeout, min_duration, min_width,
                )
            elif src_type == "nasa":
                got = fetch_nasa(
                    queries, weight, authority, license_name, name,
                    per_query, timeout, min_duration,
                )
            elif src_type == "usgs_volcano":
                got = fetch_usgs_volcano(
                    weight, authority, license_name, name,
                    timeout, min_duration,
                )
            elif src_type == "internet_archive":
                got = fetch_internet_archive(
                    queries, weight, authority, license_name, name,
                    per_query, timeout, min_duration,
                )
            else:
                print(f"[fetch] WARNING: 未知の type={src_type} (source={name})")
                got = []
            all_videos.extend(got)
        except Exception as exc:
            print(f"[fetch] ERROR: source={name} 取得中に例外: {exc}")
            continue

    print(f"[fetch] 全ソース取得完了: 計 {len(all_videos)} 件")
    deduped = deduplicate(all_videos)

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(deduped, f, ensure_ascii=False, indent=2)

    print(f"[fetch] 出力: {OUTPUT_PATH} ({len(deduped)} 件)")
    if not deduped:
        print("[fetch] FATAL: 候補動画が 0 件。後続処理を停止する。")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
