"""Gemini で台本・タイトル・クリップ秒数を生成する。

work/selected.json (select_video.py 出力) を入力に:
- yt-dlp で対象動画のメタデータ (タイトル / 説明 / 長さ) を取得
- 5 種類の構成パターン × 5 種類のずんだもん役割からランダム選択
- Gemini API で:
    - 動画タイトル (55 字以内)
    - 解説台本 JSON (7-9 シーン、各 40 字以内、ニャンコンサル+ずんだもん掛け合い)
    - クリップ抽出指示 (元動画の何秒〜何秒を 10〜20 秒切り出すか)
    - 出典権威紹介を冒頭 1-2 シーンに必ず含める
- 出力: work/script.json
"""

from __future__ import annotations

import json
import os
import random
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from google import genai

ROOT = Path(__file__).resolve().parent.parent
WORK_DIR = ROOT / "work"
SELECTED_PATH = WORK_DIR / "selected.json"
SCRIPT_PATH = WORK_DIR / "script.json"

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

# yt-dlp の YouTube bot 検知を回避するためのオプション。
# GitHub Actions の IP 帯では bot 検知が強くなっており、これだけでは抜けないことも多い。
# その場合は YOUTUBE_COOKIES (Netscape cookies.txt) を Secrets に登録する。
#
# player_client はリクエストに使うクライアント種別。
# ios / tv_simply / web_safari / web_creator は cookieless でもしばしば抜ける。
# user-agent 上書きは player_client ごとの自動 UA を阻害するので付けない。
YT_DLP_BYPASS_ARGS = [
    "--no-playlist",
    "--no-warnings",
    "--extractor-args",
    "youtube:player_client=ios,tv_simply,web_safari,web_creator,mweb,tv,android",
    "--sleep-requests", "1",
    "--retries", "3",
    "--retry-sleep", "fragment:5",
]


def yt_dlp_extra_args() -> list[str]:
    """環境変数 YOUTUBE_COOKIES が設定されていれば --cookies を追加する。

    YOUTUBE_COOKIES には Netscape 形式の cookies.txt 全文を入れる想定。
    """
    args: list[str] = []
    cookies = os.environ.get("YOUTUBE_COOKIES", "").strip()
    if cookies:
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        cookies_file = WORK_DIR / "cookies.txt"
        cookies_file.write_text(cookies, encoding="utf-8")
        args.extend(["--cookies", str(cookies_file)])
        print("[script] YOUTUBE_COOKIES を検出 → --cookies を有効化")
    return args

# 構成パターン (5 種類からランダム選択)
COMPOSITION_PATTERNS = [
    {
        "name": "速報リアクション型",
        "instruction": (
            "速報を聞いた直後のような驚きと臨場感で構成する。"
            "冒頭で『え、これ本当?』『今、世界で何が起きてる?』のような掴みを入れる。"
        ),
    },
    {
        "name": "誤解→訂正型",
        "instruction": (
            "視聴者が誤解しそうなポイントを最初に提示し、"
            "中盤でニャンコンサルが『実はそうではない』と訂正・解説する。"
        ),
    },
    {
        "name": "陰謀・裏読み型",
        "instruction": (
            "表向きの説明だけでなく『裏に何があるのか』『なぜ今これが公開されたのか』"
            "といった視点を交えて深掘りする。ただし陰謀論には踏み込まない。"
        ),
    },
    {
        "name": "未来予測型",
        "instruction": (
            "この映像から『今後何が起きるか』『次に注目すべきは何か』を予測する構成にする。"
            "最後にニャンコンサルが見るべき次のシグナルを提示する。"
        ),
    },
    {
        "name": "対立構造型",
        "instruction": (
            "ニャンコンサルとずんだもんで意見を対立させ、"
            "ずんだもんが素朴な疑問でツッコミ、ニャンコンサルが冷静に解説する構成にする。"
        ),
    },
]

# ずんだもん役割パターン (5 種類からランダム選択)
ZUNDAMON_ROLES = [
    "驚き役 (『すごいのだ!』『信じられないのだ!』など素直に驚く)",
    "質問役 (『これってどういうことなのだ?』とニャンコンサルに聞く)",
    "ツッコミ役 (『いやいや、それおかしいのだ!』と突っ込む)",
    "心配役 (『大丈夫なのだ? 怖いのだ』と視聴者目線で不安を口にする)",
    "好奇心役 (『もっと知りたいのだ!』『次はどうなるのだ?』と話を広げる)",
]


def load_selected() -> dict[str, Any]:
    """work/selected.json を読み込む。"""
    if not SELECTED_PATH.exists():
        print(f"[script] FATAL: {SELECTED_PATH} が存在しない。select_video.py を先に実行する。")
        sys.exit(1)
    with SELECTED_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def fetch_video_metadata(video_url: str) -> dict[str, Any]:
    """yt-dlp で対象動画のメタデータを取得する。

    GitHub Actions の IP 帯は YouTube に bot として検知されやすいので、
    player_client=tv,web_safari,mweb,android の順に試行する。
    YOUTUBE_COOKIES env が設定されている場合はさらに cookies を併用する。

    Args:
        video_url: 対象動画 URL

    Returns:
        title / description / duration を含む辞書
    """
    print(f"[script] yt-dlp でメタデータ取得: {video_url}")
    cmd = [
        "yt-dlp", "--dump-json", "--skip-download",
        *YT_DLP_BYPASS_ARGS,
        *yt_dlp_extra_args(),
        video_url,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=90,
            check=True,
            encoding="utf-8",
        )
    except subprocess.CalledProcessError as exc:
        print(f"[script] FATAL: yt-dlp 失敗: {exc.stderr[-2000:]}")
        if "Sign in to confirm" in (exc.stderr or "") or "not a bot" in (exc.stderr or ""):
            print(
                "[script] HINT: YouTube が bot 検知。Secrets に YOUTUBE_COOKIES を設定 "
                "(Netscape 形式 cookies.txt の全文) してください。"
            )
        raise
    except subprocess.TimeoutExpired:
        print("[script] FATAL: yt-dlp タイムアウト")
        raise

    info = json.loads(result.stdout)
    meta = {
        "title": info.get("title", ""),
        "description": info.get("description", "")[:1500],
        "duration": int(info.get("duration", 0)),
        "uploader": info.get("uploader", ""),
    }
    print(f"[script] メタデータ取得: title='{meta['title']}' duration={meta['duration']}s")
    return meta


def build_prompt(
    selected: dict[str, Any],
    meta: dict[str, Any],
    composition: dict[str, str],
    zundamon_role: str,
) -> str:
    """Gemini に投げるプロンプトを構築する。"""
    return f"""あなたは YouTube Shorts の台本作家です。
以下の元動画について、執事猫キャラ「ニャンコンサル」と「ずんだもん」の掛け合いで
解説する縦動画 Shorts (45-60 秒) の台本を JSON 形式で出力してください。

# 元動画情報
- 元タイトル: {meta['title']}
- 元投稿者: {meta['uploader']}
- 元動画長さ: {meta['duration']} 秒
- ソース名: {selected.get('source_name')} (公的機関 or CC BY)
- ライセンス: {selected.get('license')}
- 元動画 URL: {selected.get('url')}
- 元動画概要 (一部抜粋):
{meta['description'][:800]}

# キャラ設定
- ニャンコンサル: 執事猫。落ち着いた敬語で解説する。一人称は「私」。CV: 青山龍星。
- ずんだもん: 元気な子供っぽい口調。語尾「〜なのだ」。今回は「{zundamon_role}」として振る舞う。

# 構成指定
- 構成パターン: {composition['name']}
- 構成指示: {composition['instruction']}

# 厳守ルール
1. シーン数: 7〜9 シーン
2. 各シーンのセリフは **40 字以内** (字幕として読みやすくするため)
3. **冒頭 1〜2 シーン目で必ずソース権威紹介を入れる**
   例: 「米国航空宇宙局NASAが公開した映像です」「米国地質調査所USGSの公式映像です」
4. 出典 (ソース名) を本文中で必ず一度は言及する
5. 動画タイトルは **55 字以内** で視聴者が惹かれるキャッチーな日本語
6. クリップ抽出は元動画の **10〜20 秒の連続区間** を1つだけ指定 (start_sec, end_sec)
   - end_sec は元動画の duration ({meta['duration']} s) を超えてはならない
   - 0 秒〜 序盤の視覚的にインパクトある区間を選ぶ
7. 立ち絵表情は normal / happy / surprised / thinking から選ぶ
8. 話者は "nyanko" または "zundamon" のみ

# 出力形式 (JSON のみ。前後に説明文を付けない)
```json
{{
  "title": "動画タイトル (55字以内)",
  "clip": {{"start_sec": 0, "end_sec": 15}},
  "scenes": [
    {{
      "speaker": "nyanko",
      "text": "セリフ (40字以内)",
      "emotion": "normal"
    }}
  ]
}}
```

JSON のみを出力してください。
"""


def call_gemini(prompt: str) -> str:
    """Gemini API を呼び出してレスポンステキストを返す。

    新 SDK (google-genai) を使用。指定モデルが見つからない / 廃止されている
    場合は段階的にフォールバックする。
    """
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY が未設定")
    client = genai.Client(api_key=GEMINI_API_KEY)

    candidates = [GEMINI_MODEL, "gemini-2.0-flash", "gemini-1.5-flash"]
    tried: list[str] = []
    for m in candidates:
        if m and m not in tried:
            tried.append(m)

    last_exc: Exception | None = None
    for model_name in tried:
        try:
            print(f"[script] Gemini ({model_name}) 呼び出し ...")
            resp = client.models.generate_content(
                model=model_name,
                contents=prompt,
            )
            text = (resp.text or "").strip()
            if text:
                return text
            print(f"[script] WARNING: {model_name} が空応答。次候補へ。")
        except Exception as exc:
            print(f"[script] WARNING: {model_name} 呼び出し失敗: {exc}")
            last_exc = exc
            continue
    if last_exc:
        raise last_exc
    raise RuntimeError("Gemini が全候補モデルで応答なし")


def extract_json(text: str) -> dict[str, Any]:
    """Gemini レスポンスから JSON 部分を抽出してパースする。"""
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        payload = fence.group(1)
    else:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            raise ValueError(f"JSON が見つからない: {text[:200]}")
        payload = m.group(0)
    return json.loads(payload)


def validate_script(script: dict[str, Any], video_duration: int) -> None:
    """生成された台本のバリデーション。

    Raises:
        ValueError: 仕様違反があれば例外送出
    """
    if not script.get("title"):
        raise ValueError("title が空")
    if len(script["title"]) > 55:
        print(f"[script] WARNING: title が 55 字超過 ({len(script['title'])} 字)。切り詰める。")
        script["title"] = script["title"][:55]

    clip = script.get("clip", {})
    start = int(clip.get("start_sec", 0))
    end = int(clip.get("end_sec", 0))
    if end <= start:
        raise ValueError(f"clip end_sec({end}) <= start_sec({start})")
    if video_duration > 0 and end > video_duration:
        print(f"[script] WARNING: clip end_sec が動画長を超過。{end} → {video_duration}")
        end = video_duration
        script["clip"]["end_sec"] = end
    duration = end - start
    if duration < 5 or duration > 25:
        print(f"[script] WARNING: clip 長 {duration}s が想定外 (5-25s)。許容して続行。")

    scenes = script.get("scenes", [])
    if len(scenes) < 5 or len(scenes) > 12:
        raise ValueError(f"シーン数 {len(scenes)} が想定外 (5-12)")

    for i, sc in enumerate(scenes):
        spk = sc.get("speaker")
        if spk not in ("nyanko", "zundamon"):
            raise ValueError(f"scene[{i}] speaker 不正: {spk}")
        emo = sc.get("emotion", "normal")
        if emo not in ("normal", "happy", "surprised", "thinking"):
            print(f"[script] WARNING: scene[{i}] emotion={emo} → normal にフォールバック")
            sc["emotion"] = "normal"
        if not sc.get("text"):
            raise ValueError(f"scene[{i}] text が空")
        if len(sc["text"]) > 50:
            print(
                f"[script] WARNING: scene[{i}] text {len(sc['text'])} 字。"
                "字幕読みづらいが続行"
            )


def main() -> int:
    """メインエントリポイント。"""
    try:
        selected = load_selected()
    except Exception as exc:
        print(f"[script] FATAL: selected 読み込み失敗: {exc}")
        return 1

    try:
        meta = fetch_video_metadata(selected["url"])
    except Exception as exc:
        print(f"[script] FATAL: メタデータ取得失敗: {exc}")
        return 2

    composition = random.choice(COMPOSITION_PATTERNS)
    zundamon_role = random.choice(ZUNDAMON_ROLES)
    print(f"[script] 構成: {composition['name']}")
    print(f"[script] ずんだもん役割: {zundamon_role}")

    prompt = build_prompt(selected, meta, composition, zundamon_role)

    try:
        raw = call_gemini(prompt)
        script = extract_json(raw)
    except Exception as exc:
        print(f"[script] FATAL: Gemini 呼び出し / パース失敗: {exc}")
        return 3

    try:
        validate_script(script, meta["duration"])
    except ValueError as exc:
        print(f"[script] FATAL: 台本バリデーション失敗: {exc}")
        return 4

    # メタ情報を script に同梱
    script["_meta"] = {
        "video_id": selected.get("video_id"),
        "video_url": selected.get("url"),
        "source_name": selected.get("source_name"),
        "source_license": selected.get("license"),
        "original_title": meta["title"],
        "original_uploader": meta["uploader"],
        "original_duration": meta["duration"],
        "composition": composition["name"],
        "zundamon_role": zundamon_role,
    }

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    with SCRIPT_PATH.open("w", encoding="utf-8") as f:
        json.dump(script, f, ensure_ascii=False, indent=2)

    print(f"[script] 出力: {SCRIPT_PATH}")
    print(f"[script] title       : {script['title']}")
    print(f"[script] clip        : {script['clip']}")
    print(f"[script] scenes      : {len(script['scenes'])} 件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
