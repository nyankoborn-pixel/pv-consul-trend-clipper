"""Gemini で台本・タイトル・クリップ秒数を生成する。

work/selected.json (select_video.py 出力) を入力に:
- selected.json のメタ (タイトル / 説明 / duration / authority_intro) をそのまま利用
  (旧版の yt-dlp 呼び出しは廃止)
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
import sys
from pathlib import Path
from typing import Any

from google import genai
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

ROOT = Path(__file__).resolve().parent.parent
WORK_DIR = ROOT / "work"
SELECTED_PATH = WORK_DIR / "selected.json"
SCRIPT_PATH = WORK_DIR / "script.json"

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

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


def metadata_from_selected(selected: dict[str, Any]) -> dict[str, Any]:
    """selected.json の中身からそのまま Gemini プロンプト用メタを構築する。

    yt-dlp 呼び出しは不要になった (各ソースの API がメタを返してくるため)。
    """
    meta = {
        "title": selected.get("title", "") or "",
        "description": (selected.get("description") or "")[:1500],
        "duration": int(selected.get("duration") or 0),
        "uploader": selected.get("uploader", "") or "",
    }
    print(
        f"[script] メタデータ: title='{meta['title']}' duration={meta['duration']}s "
        f"uploader='{meta['uploader']}'"
    )
    return meta


def build_prompt(
    selected: dict[str, Any],
    meta: dict[str, Any],
    composition: dict[str, str],
    zundamon_role: str,
) -> str:
    """Gemini に投げるプロンプトを構築する。"""
    duration = int(meta.get("duration") or 0)
    if duration > 0:
        clip_rule = (
            f"8. クリップ抽出は元動画の **15〜25 秒の連続区間** を1つだけ指定 "
            f"(start_sec, end_sec)\n"
            f"   - end_sec は元動画の duration ({duration} s) を超えてはならない\n"
            f"   - 序盤〜中盤の視覚的にインパクトある区間を選ぶ"
        )
    else:
        # NASA / USGS / IA など duration 不明ソース。
        # make_video.py 側で実 duration を取得して安全側に丸める。
        clip_rule = (
            "8. 元動画長さが不明なため、クリップは start_sec=0, end_sec=20 (20秒) で固定すること"
        )

    authority_intro = (selected.get("authority_intro") or "").strip()
    if not authority_intro:
        authority_intro = f"{selected.get('source_name', 'ソース')}が公開した映像"

    return f"""あなたは YouTube Shorts の台本作家です。
以下の元動画について、執事猫キャラ「ニャンコンサル」と「ずんだもん」の掛け合いで
解説する縦動画 Shorts (45-60 秒) の台本を JSON 形式で出力してください。

# 元動画情報
- 元タイトル: {meta['title']}
- 元投稿者: {meta['uploader']}
- 元動画長さ: {duration if duration > 0 else "不明"} 秒
- ソース名: {selected.get('source_name')}
- ソース紹介句のヒント: 「{authority_intro}」 (これを冒頭で必ず言及する)
- ライセンス: {selected.get('license')}
- 元動画ページ URL: {selected.get('page_url')}
- 元動画概要 (一部抜粋):
{meta['description'][:800]}

# キャラ設定
- ニャンコンサル: 執事猫。落ち着いた敬語で解説する。一人称は「私」。CV: 青山龍星。
- ずんだもん: 元気な子供っぽい口調。語尾「〜なのだ」。今回は「{zundamon_role}」として振る舞う。

# 構成指定
- 構成パターン: {composition['name']}
- 構成指示: {composition['instruction']}

# 厳守ルール
1. **動画の総尺は 45〜60 秒**になるよう各シーンの長さを調整する
   (VOICEVOX で読み上げた合計が 45〜60 秒になる文字数。日本語 約 6 字/秒 が目安)
2. シーン数: **7〜9 シーン**
3. 各シーンのセリフは **20〜40 字を目安、最長 60 字まで**
   (短すぎると視聴体験が物足りない、長すぎると字幕が画面に収まらない)
4. **冒頭 1〜2 シーン目で必ずソース紹介を入れる**
   ヒント文「{authority_intro}」を自然な日本語に組み込んで読み上げる。
   例: 「{authority_intro}を解説します」「これは{authority_intro}です」
5. **末尾 1 シーンで自然な締めを入れる**(問いかけ or 余韻、強い CTA は不要)
6. 出典 (ソース名) を本文中で必ず一度は言及する
7. 動画タイトルは **55 字以内** で視聴者が惹かれるキャッチーな日本語
{clip_rule}
9. 立ち絵表情は normal / happy / surprised / thinking から選ぶ
10. 話者は "nyanko" または "zundamon" のみ

# 出力形式 (JSON のみ。前後に説明文を付けない)
```json
{{
  "title": "動画タイトル (55字以内)",
  "clip": {{"start_sec": 0, "end_sec": 20}},
  "scenes": [
    {{
      "speaker": "nyanko",
      "text": "セリフ (20〜40字目安、最長60字)",
      "emotion": "normal"
    }}
  ]
}}
```

JSON のみを出力してください。
"""


def _is_transient_error(exc: BaseException) -> bool:
    """503 UNAVAILABLE / 429 RESOURCE_EXHAUSTED など一時的なエラーを判定する。

    404 / NOT_FOUND は transient ではない (モデルそのものが無いのでリトライ無意味)。
    """
    s = str(exc)
    if any(m in s for m in ("404", "NOT_FOUND", "no longer available")):
        return False
    return any(
        marker in s
        for marker in ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "DEADLINE_EXCEEDED")
    )


def _is_model_gone(exc: BaseException) -> bool:
    """404 NOT_FOUND など『そのモデルは使えない』系エラーを判定する。"""
    s = str(exc)
    return ("404" in s and "NOT_FOUND" in s) or "no longer available" in s


def _gemini_retry_log(retry_state):
    """tenacity の before_sleep ハンドラ ([script] プレフィックスで warn)。"""
    fn = retry_state.fn.__name__ if retry_state.fn else "?"
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    next_wait = retry_state.next_action.sleep if retry_state.next_action else 0
    print(
        f"[script] WARNING: {fn}() attempt {retry_state.attempt_number} 失敗 "
        f"({exc.__class__.__name__}: {exc}) → {next_wait:.1f}s 待って再試行"
    )


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=10),
    retry=retry_if_exception(_is_transient_error),
    before_sleep=_gemini_retry_log,
    reraise=True,
)
def _generate_with_model(client, model_name: str, prompt: str) -> str:
    """単一モデルでの生成 (transient error は tenacity でリトライ、404 は即 raise)。"""
    resp = client.models.generate_content(
        model=model_name,
        contents=prompt,
    )
    return (resp.text or "").strip()


def call_gemini(prompt: str) -> str:
    """Gemini API を呼び出してレスポンステキストを返す。

    新 SDK (google-genai) を使用。
    - 503 / 429 / DEADLINE_EXCEEDED は tenacity が同一モデルで最大 3 回リトライ
    - 404 NOT_FOUND は即座に次のフォールバックモデルへ移る (本関数の外側ループ)
    - フォールバック順: env GEMINI_MODEL → 2.5-flash → 2.5-flash-lite → 2.5-pro
      旧 1.5-flash / 2.0-flash は新規ユーザーに非提供になっているため除外。
    """
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY が未設定")
    client = genai.Client(api_key=GEMINI_API_KEY)

    candidates = [
        GEMINI_MODEL,
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-2.5-pro",
    ]
    tried: list[str] = []
    for m in candidates:
        if m and m not in tried:
            tried.append(m)

    last_exc: Exception | None = None
    for model_name in tried:
        try:
            print(f"[script] Gemini ({model_name}) 呼び出し ...")
            text = _generate_with_model(client, model_name, prompt)
            if text:
                return text
            print(f"[script] WARNING: {model_name} 空応答、次候補へ。")
            last_exc = RuntimeError(f"{model_name} returned empty text")
        except Exception as exc:
            last_exc = exc
            if _is_model_gone(exc):
                print(f"[script] {model_name} は使用不可 (404)、次候補へ。")
            else:
                # tenacity の retry を使い切った後の最終失敗
                print(f"[script] WARNING: {model_name} 最終失敗: {exc}")
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
        if len(sc["text"]) < 15:
            print(
                f"[script] WARNING: scene[{i}] text {len(sc['text'])} 字。"
                "短すぎて総尺が不足する可能性 (Gemini が 20-40 字目安に従っていない)"
            )
        if len(sc["text"]) > 50:
            print(
                f"[script] WARNING: scene[{i}] text {len(sc['text'])} 字。"
                "字幕読みづらいが続行 (60 字超は要注意)"
            )


def main() -> int:
    """メインエントリポイント。"""
    try:
        selected = load_selected()
    except Exception as exc:
        print(f"[script] FATAL: selected 読み込み失敗: {exc}")
        return 1

    meta = metadata_from_selected(selected)

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

    # メタ情報を script に同梱 (make_video / upload_youtube が参照する)
    script["_meta"] = {
        "video_id": selected.get("video_id"),
        "page_url": selected.get("page_url"),
        "media_url": selected.get("media_url"),
        "source_name": selected.get("source_name"),
        "source_type": selected.get("source_type"),
        "source_license": selected.get("license"),
        "authority_intro": selected.get("authority_intro"),
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
