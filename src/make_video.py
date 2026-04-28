"""動画を合成する。

work/script.json (generate_script.py 出力) を入力に:
1. yt-dlp で元動画をダウンロード
2. clip 区間 (start_sec, end_sec) を 1080x960 / 元音声カットで切り出し
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
FONT_PATH = os.environ.get(
    "FONT_PATH", "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc"
)
BGM_PATH = ASSETS_DIR / "bgm.mp3"
BGM_VOLUME_DB = -12.0

# 出力動画スペック
W = 1080
H = 1920
HALF_H = H // 2  # 960
FPS = 30

# キャラ → VOICEVOX speaker_id
SPEAKER_IDS = {
    "zundamon": 3,
    "nyanko": 13,  # 青山龍星
}


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
    """yt-dlp で元動画をダウンロードする。

    Returns:
        ダウンロード済みファイルのパス
    """
    print(f"[make] yt-dlp ダウンロード: {url}")
    out_template = str(dest)
    run(
        [
            "yt-dlp", "-f", "bv*+ba/b",
            "--merge-output-format", "mp4",
            "-o", out_template,
            "--no-playlist", "--no-warnings",
            url,
        ],
        desc=f"download → {dest}",
    )
    if not dest.exists():
        # yt-dlp が拡張子を変えることがあるので work_dir を探索
        for cand in WORK_DIR.glob("source.*"):
            if cand.is_file():
                cand.rename(dest)
                break
    if not dest.exists():
        raise RuntimeError(f"ダウンロード結果が見つからない: {dest}")
    return dest


def cut_clip(src: Path, start_sec: float, end_sec: float, dest: Path) -> Path:
    """元動画から指定区間を 1080x960 / 元音声カットで切り出す。"""
    duration = end_sec - start_sec
    print(f"[make] clip 切り出し: {start_sec}s - {end_sec}s ({duration}s)")
    # 上半分の解像度に揃える: 1080x960、アスペクト比保持で fit + pad
    vf = (
        f"scale={W}:{HALF_H}:force_original_aspect_ratio=decrease,"
        f"pad={W}:{HALF_H}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"fps={FPS}"
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


def escape_drawtext(text: str) -> str:
    """ffmpeg drawtext 用にテキストをエスケープする。"""
    # バックスラッシュ → コロン → シングルクォート → カンマ → セミコロン → パーセント
    return (
        text.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "’")  # シングルクォートは右シングル引用符に置換
        .replace(",", "，")
        .replace(";", "；")
        .replace("%", "％")
    )


def render_scene_bottom(
    speaker: str,
    emotion: str,
    text: str,
    audio_path: Path,
    duration: float,
    dest: Path,
) -> Path:
    """シーンごとの下半分動画 (立ち絵 + 字幕 + 音声) を生成する。

    出力: 1080x960 / 30fps / aac 音声付き
    """
    char_img = character_image_path(speaker, emotion)
    safe_text = escape_drawtext(text)

    # 立ち絵: 中央配置、高さ 700px にリサイズ
    # 字幕: 画面下部中央、白文字 + 黒縁
    # font size 56
    filter_complex = (
        f"color=c=#1a1a2e:s={W}x{HALF_H}:d={duration}:r={FPS}[bg];"
        f"[1:v]scale=-1:700[char];"
        f"[bg][char]overlay=(W-w)/2:30[withchar];"
        f"[withchar]drawtext=fontfile='{FONT_PATH}':text='{safe_text}':"
        f"fontcolor=white:fontsize=56:bordercolor=black:borderw=4:"
        f"x=(w-text_w)/2:y=h-text_h-40[v]"
    )

    run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"color=c=#1a1a2e:s={W}x{HALF_H}:d={duration}",
            "-loop", "1", "-i", str(char_img),
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
        # 入力: top, bottom, bgm
        # 音声: bottom音声 + bgm (-12dB) を amix
        bgm_volume_factor = 10 ** (BGM_VOLUME_DB / 20.0)  # ≈ 0.251
        filter_complex = (
            f"[0:v][1:v]vstack=inputs=2[v];"
            f"[2:a]volume={bgm_volume_factor:.4f},aloop=loop=-1:size=2e9[bgm];"
            f"[1:a][bgm]amix=inputs=2:duration=first:dropout_transition=0[a]"
        )
        run(
            [
                "ffmpeg", "-y",
                "-i", str(top),
                "-i", str(bottom),
                "-i", str(BGM_PATH),
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
    (WORK_DIR / "scenes").mkdir(parents=True, exist_ok=True)

    try:
        script = load_script()
    except Exception as exc:
        print(f"[make] FATAL: script 読み込み失敗: {exc}")
        return 1

    meta = script["_meta"]
    video_url = meta["video_url"]

    # 1. 元動画ダウンロード
    source_path = WORK_DIR / "source.mp4"
    try:
        download_video(video_url, source_path)
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

        audio_path = WORK_DIR / "scenes" / f"scene_{i:02d}.wav"
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

        scene_video = WORK_DIR / "scenes" / f"scene_{i:02d}.mp4"
        try:
            render_scene_bottom(speaker, emotion, text, audio_path, duration, scene_video)
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
