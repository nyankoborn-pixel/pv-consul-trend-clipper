"""動画を合成する。

work/script.json (generate_script.py 出力) を入力に:
1. media_url (各 API の MP4 直リンク) を requests ストリームでダウンロード
2. clip 区間 (start_sec, end_sec) を 1080x960 / 元音声カットで切り出し
   実動画長より end_sec が大きければ自動で丸める
3. シーンごとに VOICEVOX で音声合成
4. シーンごとの下半分 (立ち絵 + 字幕) 動画を生成
5. 全シーンを concat → 1080x960 の下半分動画を作成
6. 上半分 (元動画クリップをループ) と下半分を vstack
7. assets/bgm.mp3 を -12dB で mix
8. output.mp4 を出力 (1080x1920 縦動画)

レイアウト:
  上半分 (y=0..960)   : 元動画クリップ
  下半分 (y=960..1920): 立ち絵中央 + 字幕 (画面下部)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent.parent
WORK_DIR = ROOT / "work"
ASSETS_DIR = ROOT / "assets"
SCRIPT_PATH = WORK_DIR / "script.json"
OUTPUT_PATH = ROOT / "output.mp4"

VOICEVOX_URL = os.environ.get("VOICEVOX_URL", "http://localhost:50021")
BGM_PATH = ASSETS_DIR / "bgm.mp3"
BGM_VOLUME_DB = -12.0

DOWNLOAD_HEADERS = {
    "User-Agent": "pv-consul-trend-clipper/1.0 (+https://github.com/)",
}
DOWNLOAD_TIMEOUT = 120
DOWNLOAD_CHUNK = 1024 * 1024  # 1 MiB

# 出力動画スペック
W = 1080
H = 1920
HALF_H = H // 2  # 960
FPS = 30

# 字幕レイアウト
SUBTITLE_FONTSIZE = 44
SUBTITLE_WRAP_CHARS = 18  # 1 行あたり最大文字数
SUBTITLE_LINE_SPACING = 12

# キャラ → VOICEVOX speaker_id
SPEAKER_IDS = {
    "zundamon": 3,
    "nyanko": 13,  # 青山龍星
}

# ffmpeg 色指定 (lavfi color source は 0xRRGGBB を確実に解釈)
BG_COLOR = "0x1a1a2e"

# 日本語フォントの探索候補 (FONT_PATH env が無効なら順に探す)
FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKjp-Bold.otf",
    "/usr/share/fonts/opentype/noto/NotoSansCJKjp-Regular.otf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",  # 最終フォールバック (日本語不可)
    "C:/Windows/Fonts/YuGothM.ttc",  # ローカル Windows 開発用
    "C:/Windows/Fonts/meiryo.ttc",
]


def resolve_font_path() -> str:
    """日本語フォントの実体パスを解決する。

    FONT_PATH env が指す実ファイルが存在すればそれを使用、
    存在しなければ FONT_CANDIDATES を順に探索する。
    """
    env_path = os.environ.get("FONT_PATH", "").strip()
    if env_path and Path(env_path).is_file():
        print(f"[make] フォント (env): {env_path}")
        return env_path
    for cand in FONT_CANDIDATES:
        if Path(cand).is_file():
            if "DejaVu" in cand:
                print(f"[make] WARNING: 日本語フォント未検出。{cand} で代用 (字化け可能性)")
            else:
                print(f"[make] フォント検出: {cand}")
            return cand
    raise FileNotFoundError(
        "日本語フォントが見つかりません。FONT_PATH env を設定するか、"
        "Ubuntu なら 'apt install fonts-noto-cjk' を実行してください。"
    )


def load_script() -> dict[str, Any]:
    """work/script.json を読み込む。"""
    if not SCRIPT_PATH.exists():
        print(f"[make] FATAL: {SCRIPT_PATH} が存在しない。")
        sys.exit(1)
    with SCRIPT_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def run(cmd: list[str], desc: str = "") -> None:
    """コマンドを実行。失敗時は例外。"""
    if desc:
        print(f"[make] {desc}")
    print(f"[make] $ {' '.join(cmd)}")
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if res.returncode != 0:
        print(f"[make] STDERR: {res.stderr[-2000:]}")
        raise RuntimeError(f"command failed (rc={res.returncode}): {' '.join(cmd[:3])}")


def ffprobe_duration(path: Path) -> float:
    """メディアファイルの再生時間 (秒) を ffprobe で取得する。"""
    res = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ],
        capture_output=True, text=True, check=True, encoding="utf-8",
    )
    return float(res.stdout.strip())


def download_video(url: str, dest: Path) -> Path:
    """media_url (公式 API の MP4 直リンク) を requests ストリームで保存する。

    Pixabay / Pexels / NASA / Internet Archive / USGS のいずれも
    cookies なし & 認証なしの直リンクを返すので yt-dlp は不要。
    """
    print(f"[make] HTTP ダウンロード: {url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with requests.get(
            url,
            stream=True,
            headers=DOWNLOAD_HEADERS,
            timeout=DOWNLOAD_TIMEOUT,
            allow_redirects=True,
        ) as r:
            r.raise_for_status()
            total = 0
            with dest.open("wb") as f:
                for chunk in r.iter_content(chunk_size=DOWNLOAD_CHUNK):
                    if not chunk:
                        continue
                    f.write(chunk)
                    total += len(chunk)
    except requests.RequestException as exc:
        raise RuntimeError(f"ダウンロード失敗: {exc}") from exc

    if not dest.exists() or dest.stat().st_size == 0:
        raise RuntimeError(f"ダウンロード結果が空: {dest}")
    print(f"[make] download → {dest} ({dest.stat().st_size:,} bytes)")
    return dest


def cut_clip(src: Path, start_sec: float, end_sec: float, dest: Path) -> Path:
    """元動画から指定区間を 1080x960 / 元音声カットで切り出す。

    実動画長を ffprobe で取得し、end_sec が動画長を超えていれば丸める。
    結果クリップが極端に短い場合 (< 3 s) はエラーにする。
    """
    try:
        actual_duration = ffprobe_duration(src)
    except Exception as exc:
        print(f"[make] WARNING: 元動画の duration 取得失敗 ({exc})。指定値で続行。")
        actual_duration = float("inf")

    if end_sec > actual_duration:
        print(
            f"[make] clip end_sec={end_sec}s > 実動画 {actual_duration:.2f}s。"
            f"末尾 0.2s 余裕を見て丸める。"
        )
        end_sec = max(start_sec + 1.0, actual_duration - 0.2)
    if start_sec >= end_sec:
        raise RuntimeError(
            f"clip 範囲が不正: start={start_sec}s end={end_sec}s "
            f"(actual_duration={actual_duration})"
        )

    duration = end_sec - start_sec
    if duration < 3.0:
        raise RuntimeError(f"clip 長さ {duration:.2f}s が短すぎる (元動画が短い可能性)")

    print(f"[make] clip 切り出し: {start_sec}s - {end_sec}s ({duration}s)")
    # 上半分の解像度に揃える: 1080x960、アスペクト比保持で fit + pad
    vf = (
        f"scale={W}:{HALF_H}:force_original_aspect_ratio=decrease,"
        f"pad={W}:{HALF_H}:(ow-iw)/2:(oh-ih)/2:color=0x000000,"
        f"setsar=1,fps={FPS}"
    )
    run(
        [
            "ffmpeg", "-y",
            "-ss", str(start_sec),
            "-i", str(src),
            "-t", str(duration),
            "-an",
            "-vf", vf,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p",
            str(dest),
        ],
        desc=f"clip → {dest}",
    )
    return dest


def voicevox_synthesize(text: str, speaker_id: int, dest: Path) -> Path:
    """VOICEVOX HTTP API で音声合成する。

    Args:
        text: 読み上げテキスト
        speaker_id: VOICEVOX speaker_id
        dest: 出力 wav パス

    Returns:
        生成された wav ファイルパス
    """
    print(f"[make] VOICEVOX 合成 speaker={speaker_id}: {text[:30]}")
    # audio_query
    q = requests.post(
        f"{VOICEVOX_URL}/audio_query",
        params={"text": text, "speaker": speaker_id},
        timeout=30,
    )
    q.raise_for_status()
    query = q.json()

    # synthesis
    s = requests.post(
        f"{VOICEVOX_URL}/synthesis",
        params={"speaker": speaker_id},
        json=query,
        timeout=120,
    )
    s.raise_for_status()
    dest.write_bytes(s.content)
    return dest


def character_image_path(speaker: str, emotion: str) -> Path:
    """話者+表情から立ち絵パスを返す。見つからなければ normal にフォールバック。"""
    base = ASSETS_DIR / speaker / f"{speaker}_{emotion}.png"
    if base.exists():
        return base
    fallback = ASSETS_DIR / speaker / f"{speaker}_normal.png"
    if fallback.exists():
        print(f"[make] WARNING: {base.name} が無いので {fallback.name} を使用")
        return fallback
    raise FileNotFoundError(f"立ち絵が見つからない: {base} / {fallback}")


def wrap_jp_text(text: str, max_chars_per_line: int = SUBTITLE_WRAP_CHARS) -> str:
    """日本語テキストを max_chars_per_line で機械的に折り返す。

    句読点や英単語境界を考慮した高度な折返しはしない (Shorts 字幕は短文前提)。
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    out_lines: list[str] = []
    for line in text.split("\n"):
        if not line:
            out_lines.append("")
            continue
        for i in range(0, len(line), max_chars_per_line):
            out_lines.append(line[i:i + max_chars_per_line])
    return "\n".join(out_lines)


def render_scene_bottom(
    speaker: str,
    emotion: str,
    text: str,
    audio_path: Path,
    duration: float,
    dest: Path,
    font_path: str,
    text_dir: Path,
    scene_index: int,
) -> Path:
    """シーンごとの下半分動画 (立ち絵 + 字幕 + 音声) を生成する。

    出力: 1080x960 / 30fps / aac 音声付き

    drawtext は textfile パラメータでテキストを渡し、フィルタ文字列内での
    クォート・コロン・カンマのエスケープ問題を回避する。
    """
    char_img = character_image_path(speaker, emotion)

    # 字幕テキストを折返し → ファイル化 (drawtext textfile 用)
    wrapped = wrap_jp_text(text)
    text_file = text_dir / f"scene_{scene_index:02d}_text.txt"
    text_file.write_text(wrapped, encoding="utf-8")

    # ffmpeg フィルタ用にパス区切りを正規化 (Windows ローカル開発も考慮)
    fontfile_arg = font_path.replace("\\", "/").replace(":", "\\:")
    textfile_arg = str(text_file.resolve()).replace("\\", "/").replace(":", "\\:")

    # 立ち絵: 高さ 700px にリサイズ (アスペクト比維持)
    # 字幕: 画面下部中央、白文字 + 黒縁、自動折返し済み
    filter_complex = (
        f"[0:v]format=yuv420p[bg];"
        f"[1:v]scale=-1:700[char];"
        f"[bg][char]overlay=(W-w)/2:30:format=auto[withchar];"
        f"[withchar]drawtext="
        f"fontfile='{fontfile_arg}':"
        f"textfile='{textfile_arg}':"
        f"fontcolor=white:"
        f"fontsize={SUBTITLE_FONTSIZE}:"
        f"line_spacing={SUBTITLE_LINE_SPACING}:"
        f"bordercolor=black:borderw=3:"
        f"x=(w-text_w)/2:"
        f"y=h-text_h-40[v]"
    )

    run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", f"color=c={BG_COLOR}:s={W}x{HALF_H}:d={duration}:r={FPS}",
            "-loop", "1", "-t", str(duration), "-i", str(char_img),
            "-i", str(audio_path),
            "-filter_complex", filter_complex,
            "-map", "[v]", "-map", "2:a",
            "-t", str(duration),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            "-r", str(FPS),
            str(dest),
        ],
        desc=f"scene bottom → {dest}",
    )
    return dest


def concat_scenes(scene_paths: list[Path], dest: Path) -> Path:
    """シーン下半分動画群を concat する。"""
    list_file = WORK_DIR / "concat_list.txt"
    with list_file.open("w", encoding="utf-8") as f:
        for p in scene_paths:
            # ffmpeg concat demuxer 用にエスケープ
            abs_path = str(p.resolve()).replace("\\", "/")
            f.write(f"file '{abs_path}'\n")

    run(
        [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(list_file),
            "-c", "copy",
            str(dest),
        ],
        desc=f"concat → {dest}",
    )
    return dest


def make_top_loop(clip_path: Path, total_duration: float, dest: Path) -> Path:
    """上半分: 元クリップを total_duration までループした映像を作る。"""
    run(
        [
            "ffmpeg", "-y",
            "-stream_loop", "-1",
            "-i", str(clip_path),
            "-t", str(total_duration),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-an",
            "-r", str(FPS),
            str(dest),
        ],
        desc=f"top loop → {dest}",
    )
    return dest


def vstack_with_bgm(
    top: Path, bottom: Path, total_duration: float, dest: Path
) -> Path:
    """上下動画を vstack し、BGM を -12dB で mix した最終動画を出力する。"""
    has_bgm = BGM_PATH.exists()
    if not has_bgm:
        print(f"[make] WARNING: BGM が見つからない ({BGM_PATH})。BGMなしで出力。")

    if has_bgm:
        # 入力: top, bottom, bgm (BGM は -stream_loop -1 で input 段階で無限ループ)
        # 音声: bottom音声 + bgm (-12dB) を amix
        filter_complex = (
            f"[0:v][1:v]vstack=inputs=2[v];"
            f"[2:a]volume={BGM_VOLUME_DB}dB[bgm];"
            f"[1:a][bgm]amix=inputs=2:duration=first:dropout_transition=0[a]"
        )
        run(
            [
                "ffmpeg", "-y",
                "-i", str(top),
                "-i", str(bottom),
                "-stream_loop", "-1", "-i", str(BGM_PATH),
                "-filter_complex", filter_complex,
                "-map", "[v]", "-map", "[a]",
                "-t", str(total_duration),
                "-c:v", "libx264", "-preset", "medium", "-crf", "21",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k",
                "-r", str(FPS),
                str(dest),
            ],
            desc=f"final → {dest}",
        )
    else:
        run(
            [
                "ffmpeg", "-y",
                "-i", str(top),
                "-i", str(bottom),
                "-filter_complex", "[0:v][1:v]vstack=inputs=2[v]",
                "-map", "[v]", "-map", "1:a",
                "-t", str(total_duration),
                "-c:v", "libx264", "-preset", "medium", "-crf", "21",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k",
                "-r", str(FPS),
                str(dest),
            ],
            desc=f"final (no bgm) → {dest}",
        )
    return dest


def main() -> int:
    """メインエントリポイント。"""
    if WORK_DIR.exists():
        # 過去の中間生成物を一旦掃除 (script.json / candidates.json などは残す)
        for sub in ("scenes",):
            shutil.rmtree(WORK_DIR / sub, ignore_errors=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    scenes_dir = WORK_DIR / "scenes"
    scenes_dir.mkdir(parents=True, exist_ok=True)

    try:
        font_path = resolve_font_path()
    except Exception as exc:
        print(f"[make] FATAL: フォント解決失敗: {exc}")
        return 99

    try:
        script = load_script()
    except Exception as exc:
        print(f"[make] FATAL: script 読み込み失敗: {exc}")
        return 1

    meta = script["_meta"]
    media_url = meta.get("media_url") or ""
    if not media_url:
        print("[make] FATAL: _meta.media_url が空。fetch_videos.py の出力を確認。")
        return 2

    # 1. 元動画ダウンロード (公式 API の MP4 直リンクから requests で取得)
    source_path = WORK_DIR / "source.mp4"
    try:
        download_video(media_url, source_path)
    except Exception as exc:
        print(f"[make] FATAL: ダウンロード失敗: {exc}")
        return 2

    # 2. clip 切り出し
    clip = script["clip"]
    clip_path = WORK_DIR / "clip.mp4"
    try:
        cut_clip(source_path, float(clip["start_sec"]), float(clip["end_sec"]), clip_path)
    except Exception as exc:
        print(f"[make] FATAL: clip 切り出し失敗: {exc}")
        return 3

    # 3, 4. シーンごとに VOICEVOX 合成 + 下半分動画生成
    scene_videos: list[Path] = []
    for i, sc in enumerate(script["scenes"]):
        speaker = sc["speaker"]
        emotion = sc.get("emotion", "normal")
        text = sc["text"]
        speaker_id = SPEAKER_IDS.get(speaker)
        if speaker_id is None:
            print(f"[make] FATAL: unknown speaker={speaker}")
            return 4

        audio_path = scenes_dir / f"scene_{i:02d}.wav"
        try:
            voicevox_synthesize(text, speaker_id, audio_path)
        except Exception as exc:
            print(f"[make] FATAL: VOICEVOX scene[{i}] 失敗: {exc}")
            return 5

        try:
            duration = ffprobe_duration(audio_path)
        except Exception as exc:
            print(f"[make] FATAL: ffprobe scene[{i}] 失敗: {exc}")
            return 6
        # 末尾に余韻 0.3s
        duration += 0.3

        scene_video = scenes_dir / f"scene_{i:02d}.mp4"
        try:
            render_scene_bottom(
                speaker=speaker,
                emotion=emotion,
                text=text,
                audio_path=audio_path,
                duration=duration,
                dest=scene_video,
                font_path=font_path,
                text_dir=scenes_dir,
                scene_index=i,
            )
        except Exception as exc:
            print(f"[make] FATAL: scene[{i}] 描画失敗: {exc}")
            return 7
        scene_videos.append(scene_video)

    # 5. concat
    bottom_path = WORK_DIR / "bottom.mp4"
    try:
        concat_scenes(scene_videos, bottom_path)
    except Exception as exc:
        print(f"[make] FATAL: concat 失敗: {exc}")
        return 8

    total_duration = ffprobe_duration(bottom_path)
    print(f"[make] 総再生時間: {total_duration:.2f}s")

    # 6. 上半分ループ
    top_path = WORK_DIR / "top.mp4"
    try:
        make_top_loop(clip_path, total_duration, top_path)
    except Exception as exc:
        print(f"[make] FATAL: top loop 失敗: {exc}")
        return 9

    # 7. vstack + BGM
    try:
        vstack_with_bgm(top_path, bottom_path, total_duration, OUTPUT_PATH)
    except Exception as exc:
        print(f"[make] FATAL: 最終合成失敗: {exc}")
        return 10

    print(f"[make] 完成: {OUTPUT_PATH} ({total_duration:.2f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
