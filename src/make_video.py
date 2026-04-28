"""動画を合成する (フルスクリーン動画 + 字幕帯のみ、立ち絵廃止)。

work/script.json (generate_script.py 出力) を入力に:
1. media_url (各 API の MP4 直リンク) を requests ストリームでダウンロード
2. clip 区間 (start_sec, end_sec) を 1080x1920 (フルスクリーン縦) で切り出し
   元音声はカット。実動画長より end_sec が大きければ自動で丸める。
   start_sec≈0 で動画が要求の 2.5 倍以上長い場合はランダム中盤切出し。
3. シーンごとに VOICEVOX で音声合成 (nyanko=青山龍星 / zundamon)
4. シーンごとに 1080x1920 の完成動画 (clip ループ + 字幕帯 + 字幕 + 音声) を生成
   - 字幕帯: y=1430..1850 半透明黒 (alpha 0.6)
   - 字幕文字: 56pt 白 + 黒縁 4px、帯上端 + 40px から top-anchored、
              長文時は下端 70px 安全マージンでクランプ
5. 全シーンを concat → presentation.mp4
6. assets/bgm.mp3 を -12dB で mix → output.mp4

立ち絵 (キャラ画像) は元動画の被写体被覆を避けるため廃止。
キャラの識別は音声 (VOICEVOX 声分け) のみで行う。
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
FPS = 30

# レイアウト定数
# - 立ち絵は廃止。元動画の被写体被覆を最小化するため video + 字幕帯のみで構成。
#   音声は VOICEVOX で nyanko / zundamon の声分けを継続。
# - 字幕帯: y=SUBTITLE_BAND_Y..(SUBTITLE_BAND_Y+SUBTITLE_BAND_H)、半透明黒
# - 字幕文字: 帯の上端 + TOP_PAD から配置 (top-anchored)、長文時は
#             下端から BOTTOM_SAFE 余白を確保するようクランプ。白文字 + 黒縁。
SUBTITLE_BAND_Y = 1430
SUBTITLE_BAND_H = 420
SUBTITLE_BAND_ALPHA = 0.6
SUBTITLE_FONTSIZE = 56
SUBTITLE_WRAP_CHARS = 18  # 1 行あたり最大文字数 (fontsize 56 で 1080px に収まる)
SUBTITLE_LINE_SPACING = 14
SUBTITLE_BORDERW = 4
SUBTITLE_TOP_PAD = 40       # 字幕帯の上端から文字までの余白
SUBTITLE_BOTTOM_SAFE = 70   # フレーム下端から確保する安全マージン

# 中央前景 (元動画) の最大寸法 — bgblur 背景の上に重ねる「サムネイル風」枠サイズ
# 1080x1920 フレームに対し W=900 (83%) で左右に余白、H=1500 で字幕帯 (y=1430..) と被らない
FG_MAX_W = 900
FG_MAX_H = 1500

# クリップ尺の目標 (秒)。YouTube Shorts は 60 秒上限なので少し余裕を持たせる。
# 元素材が長いソース (NASA, IA) はこの値まで切り出す。短いソース (Pixabay/Pexels)
# は元素材の末尾までとなる。compose_scene 側でシーンごとに -ss で連続再生し、
# クリップが総尺より短い場合は -stream_loop -1 で先頭に戻ってループ継続する。
TARGET_CLIP_DURATION = 58.0

# キャラ → VOICEVOX speaker_id (音声の声分けは継続)
SPEAKER_IDS = {
    "zundamon": 3,
    "nyanko": 13,  # 青山龍星
}

# 日本語フォントの探索候補
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
    """日本語フォントの実体パスを解決する。"""
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


def ffprobe_dim(path: Path) -> str:
    """メディアファイルの解像度を 'WIDTHxHEIGHT' で返す。失敗時は '?x?'。"""
    try:
        res = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "csv=s=x:p=0", str(path),
            ],
            capture_output=True, text=True, check=True, encoding="utf-8",
        )
        return res.stdout.strip() or "?x?"
    except Exception:
        return "?x?"


def download_video(url: str, dest: Path) -> Path:
    """media_url を requests ストリームで保存する。"""
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
    print(
        f"[make] download → {dest} "
        f"({dest.stat().st_size:,} bytes, dim={ffprobe_dim(dest)})"
    )
    return dest


def cut_clip(src: Path, start_sec: float, end_sec: float, dest: Path) -> Path:
    """元動画から指定区間を 1080x1920 (フルスクリーン縦) / 元音声カットで切り出す。

    - Gemini が指定した start_sec を起点とし、end_sec は TARGET_CLIP_DURATION
      まで延長(ソース末尾を超えない範囲で)。
      これで「長いソースは Shorts 尺いっぱい流す」「短いソースは末尾まで」が両立。
      シーン間のクリップ連続性は compose_scene の -ss cumulative offset で担保。
    - 結果クリップが極端に短い場合 (< 3 s) は start_sec=0 にリセットして再計算
    """
    try:
        actual_duration = ffprobe_duration(src)
    except Exception as exc:
        print(f"[make] WARNING: 元動画の duration 取得失敗 ({exc})。指定値で続行。")
        actual_duration = float("inf")

    # start_sec の妥当性チェック (動画末尾近すぎなら 0 に戻す)
    if actual_duration != float("inf") and start_sec >= actual_duration - 1.0:
        print(
            f"[make] start_sec={start_sec}s が動画長 {actual_duration:.2f}s に近すぎる。"
            f"start=0 にリセット。"
        )
        start_sec = 0.0

    # end_sec を TARGET_CLIP_DURATION まで延長 (ソース末尾の 0.2s 手前で頭打ち)
    target_end = start_sec + TARGET_CLIP_DURATION
    if actual_duration != float("inf"):
        max_end = actual_duration - 0.2
        end_sec = min(target_end, max_end)
    else:
        end_sec = target_end

    if start_sec >= end_sec:
        raise RuntimeError(
            f"clip 範囲が不正: start={start_sec}s end={end_sec}s "
            f"(actual_duration={actual_duration})"
        )

    duration = end_sec - start_sec
    if duration < 3.0:
        raise RuntimeError(
            f"clip 長さ {duration:.2f}s が短すぎる (元動画が短い可能性、actual={actual_duration}s)"
        )

    print(f"[make] clip 切り出し: {start_sec}s - {end_sec}s ({duration}s)")
    # 背景ぼかし + 中央配置 (TikTok/Shorts 定番、GPT 推奨):
    # - bg: 元動画を 1080x1920 にカバー (cover-crop) してガウシアンぼかし
    # - fg: 元動画を縦横比保持で FG_MAX_W x FG_MAX_H に decrease で fit
    #       (1080 幅フルではなく FG_MAX_W=900 に明示縮小、bgblur が左右にも見える)
    # - overlay: bgblur に fgfit を中央重ね
    # 黒帯のまま放置するより一体感が出て、被写体は元アスペクト比のまま全体可視。
    filter_complex = (
        f"[0:v]split=2[bg][fg];"
        f"[bg]scale={W}:{H}:force_original_aspect_ratio=increase,"
        f"crop={W}:{H},gblur=sigma=20[bgblur];"
        f"[fg]scale={FG_MAX_W}:{FG_MAX_H}:force_original_aspect_ratio=decrease[fgfit];"
        f"[bgblur][fgfit]overlay=(W-w)/2:(H-h)/2,"
        f"setsar=1,fps={FPS}[out]"
    )
    run(
        [
            "ffmpeg", "-y",
            "-ss", str(start_sec),
            "-i", str(src),
            "-t", str(duration),
            "-an",
            "-filter_complex", filter_complex,
            "-map", "[out]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p",
            str(dest),
        ],
        desc=f"clip → {dest}",
    )
    print(f"[make] clip 完了: {dest} dim={ffprobe_dim(dest)}")
    return dest


def render_bg(clip_path: Path, total_duration: float, dest: Path) -> Path:
    """clip.mp4 を total_duration 秒の bg.mp4 に整形する (必要ならループ繰り返し)。

    ffmpeg の `-stream_loop -1 -ss OFFSET` の組み合わせは挙動が不安定 (バージョン
    によって offset がループ毎に再適用される/されないが揺れる) なので、
    総尺ぶんを 1 回だけ事前レンダリングして「単純な seek 可能な bg」に変換する。
    各シーンの compose_scene はこの bg.mp4 に対して `-ss offset` だけで seek できる。
    """
    print(f"[make] bg レンダリング: clip → {dest} (total_duration={total_duration:.2f}s)")
    run(
        [
            "ffmpeg", "-y",
            "-stream_loop", "-1",       # clip を必要なだけループ
            "-i", str(clip_path),
            "-t", f"{total_duration:.3f}",  # 総尺で打ち切り
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-an",                        # 音声なし
            "-r", str(FPS),
            str(dest),
        ],
        desc=f"bg → {dest}",
    )
    return dest


def voicevox_synthesize(text: str, speaker_id: int, dest: Path) -> Path:
    """VOICEVOX HTTP API で音声合成する。"""
    print(f"[make] VOICEVOX 合成 speaker={speaker_id}: {text[:30]}")
    q = requests.post(
        f"{VOICEVOX_URL}/audio_query",
        params={"text": text, "speaker": speaker_id},
        timeout=30,
    )
    q.raise_for_status()
    query = q.json()

    s = requests.post(
        f"{VOICEVOX_URL}/synthesis",
        params={"speaker": speaker_id},
        json=query,
        timeout=120,
    )
    s.raise_for_status()
    dest.write_bytes(s.content)
    return dest


def wrap_jp_text(text: str, max_chars_per_line: int = SUBTITLE_WRAP_CHARS) -> str:
    """日本語テキストを max_chars_per_line で機械的に折り返す。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    out_lines: list[str] = []
    for line in text.split("\n"):
        if not line:
            out_lines.append("")
            continue
        for i in range(0, len(line), max_chars_per_line):
            out_lines.append(line[i:i + max_chars_per_line])
    return "\n".join(out_lines)


def escape_drawtext_text(text: str) -> str:
    """ffmpeg drawtext text= の値用にエスケープする (引用符を使わない方式)。

    改行処理に注意:
    drawtext で改行させるには text= の値に `\\n` (2 バイト: \\, n) が届く必要がある。
    だが filter parser が先に走り、`\\X` パターンを「次の文字を literal」と解釈して
    `\\` を喰うため、`\\n` (2 バイト) を渡すと drawtext には `n` (1 バイト) しか届かず
    1 行のまま 'n' という文字として描画されてしまう。
    drawtext に `\\n` を届けるには、filter parser に `\\\\n` (3 バイト: \\, \\, n) を
    渡す必要がある。Python 文字列リテラル `"\\\\\\\\n"` は 3 文字 `\\\\n` を表す。
    """
    text = text.replace("\\", "\\\\")
    text = text.replace(":", "\\:")
    text = text.replace(",", "\\,")
    text = text.replace(";", "\\;")
    text = text.replace("'", "\\'")
    text = text.replace("%", "\\%")
    text = text.replace("[", "\\[")
    text = text.replace("]", "\\]")
    text = text.replace("=", "\\=")
    text = text.replace("\n", "\\\\n")
    return text


def compose_scene(
    clip_path: Path,
    speaker: str,
    text: str,
    audio_path: Path,
    duration: float,
    dest: Path,
    font_path: str,
    text_dir: Path,
    scene_index: int,
    clip_offset: float = 0.0,
) -> Path:
    """シーン 1 本分の完成動画 (1080x1920) を生成する。

    レイアウト (立ち絵廃止後):
    - 背景: clip_path を clip_offset 秒目から読み (-ss)、必要なら -stream_loop -1
            でループしながら scene 尺を埋める
    - 字幕帯: drawbox y=SUBTITLE_BAND_Y h=SUBTITLE_BAND_H 半透明黒
    - 字幕: drawtext textfile= 経由で実改行を保持
    - 音声: VOICEVOX wav (BGM はこの段階では混ぜない)

    clip_offset を main() がシーン累積で渡すことで、シーン間でクリップが
    連続して再生される (シーン毎に clip[0] へリセットされる問題を解消)。
    """
    # text は main() 側で改行クリーン済み (VOICEVOX と drawtext で同一の文字列)。
    # ここで wrap_jp_text() による機械的な折返しを適用し、
    # 結果をテキストファイルに書いて drawtext の textfile= 経由で渡す。
    #
    # 重要: drawtext text= 直書きでは "\n" が改行として解釈されず literal "n"
    # 文字として描画されてしまう (filter parser を通り抜けた後の drawtext の
    # escape ルールが \X → X(literal) 扱いのため)。
    # textfile= 方式ならファイル内の実改行を素直に改行として描画する。
    wrapped = wrap_jp_text(text)
    text_file = text_dir / f"scene_{scene_index:02d}_text.txt"
    text_file.write_text(wrapped, encoding="utf-8")
    print(f"[make] scene[{scene_index}] speaker={speaker} text={wrapped!r}")

    fontfile_arg = font_path.replace("\\", "/").replace(":", "\\:")
    textfile_arg = str(text_file.resolve()).replace("\\", "/").replace(":", "\\:")

    # 字幕の y 位置: 基本は帯上端 + TOP_PAD (top-anchored)。
    # 長文で text_h が大きい場合に下端からはみ出さないよう、
    # フレーム下端 - BOTTOM_SAFE - text_h を上限としてクランプ。
    #
    # 注意 1: ffmpeg drawtext の y= は filter parser が先に解釈するため、
    #   Python 側の算術式 (例: "1430+40") をそのまま渡すと
    #   "No such filter: '1430+40'" でパースエラーになる。
    #   定数演算は Python 側で事前評価し、リテラル数値だけ渡す。
    #   text_h は drawtext 自身が解決する変数なので残してよい。
    # 注意 2: min(a,b) の中のカンマは filter chain の区切り (",") として
    #   ffmpeg parser が誤認するため "No such filter: '1470'" でエラーになる。
    #   シングルクォートで全体を囲むと literal として扱われる
    #   (同ファイルの fontfile='...' と同じ機構)。
    subtitle_y_top = SUBTITLE_BAND_Y + SUBTITLE_TOP_PAD          # 1470
    subtitle_y_max = H - SUBTITLE_BOTTOM_SAFE                    # 1850
    subtitle_y_expr = f"'min({subtitle_y_max}-text_h,{subtitle_y_top})'"

    filter_complex = (
        # ベース動画 (1080x1920) を yuv420p に正規化
        f"[0:v]format=yuv420p,setsar=1[v0];"
        # 字幕帯 (半透明黒): y=SUBTITLE_BAND_Y から H まで
        f"[v0]drawbox=x=0:y={SUBTITLE_BAND_Y}:w={W}:h={SUBTITLE_BAND_H}:"
        f"color=black@{SUBTITLE_BAND_ALPHA}:t=fill[withbox];"
        # 字幕テキスト (textfile= 経由で多行改行を保持)
        f"[withbox]drawtext="
        f"fontfile='{fontfile_arg}':"
        f"textfile='{textfile_arg}':"
        f"fontcolor=white:"
        f"fontsize={SUBTITLE_FONTSIZE}:"
        f"line_spacing={SUBTITLE_LINE_SPACING}:"
        f"bordercolor=black:borderw={SUBTITLE_BORDERW}:"
        f"x=(w-text_w)/2:"
        f"y={subtitle_y_expr}"
        f"[v]"
    )

    run(
        [
            "ffmpeg", "-y",
            # input 0: 事前レンダリング済み bg.mp4 (総尺ぶん既にループ済み)。
            # ここでは -ss で offset 秒目に seek するだけ。-stream_loop は使わない
            # (ffmpeg の `-stream_loop -1 -ss N` は挙動不安定なため避ける)。
            "-ss", f"{clip_offset:.3f}",
            "-i", str(clip_path),
            # input 1: VOICEVOX 音声
            "-i", str(audio_path),
            "-filter_complex", filter_complex,
            "-map", "[v]", "-map", "1:a",
            "-t", str(duration),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",  # YouTube Shorts 推奨
            "-r", str(FPS),
            str(dest),
        ],
        desc=f"compose scene[{scene_index}] @offset={clip_offset:.2f}s → {dest}",
    )
    return dest


def concat_scenes(scene_paths: list[Path], dest: Path) -> Path:
    """シーン動画群を concat する (codec 一致済みなので -c copy)。"""
    list_file = WORK_DIR / "concat_list.txt"
    with list_file.open("w", encoding="utf-8") as f:
        for p in scene_paths:
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


def mix_bgm(input_video: Path, dest: Path, total_duration: float) -> Path:
    """完成動画に BGM (-12dB ループ) を mix する。

    BGM が無い場合は voice 音声のみのまま input_video をコピーする。
    """
    if not BGM_PATH.exists():
        print(f"[make] WARNING: BGM が見つからない ({BGM_PATH})。BGMなしで出力。")
        shutil.copy(input_video, dest)
        return dest

    filter_complex = (
        f"[1:a]volume={BGM_VOLUME_DB}dB[bgm];"
        f"[0:a][bgm]amix=inputs=2:duration=first:dropout_transition=0[a]"
    )
    run(
        [
            "ffmpeg", "-y",
            "-i", str(input_video),
            "-stream_loop", "-1", "-i", str(BGM_PATH),
            "-filter_complex", filter_complex,
            "-map", "0:v", "-map", "[a]",
            "-t", str(total_duration),
            # video は再エンコード不要 (concat 済みの presentation.mp4 をそのまま)
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",  # YouTube Shorts 推奨
            str(dest),
        ],
        desc=f"BGM mix → {dest}",
    )
    return dest


def main() -> int:
    """メインエントリポイント。"""
    if WORK_DIR.exists():
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

    # 立ち絵は廃止したのでアセット存在チェックは不要 (script.json の _meta は維持)。

    meta = script["_meta"]
    media_url = meta.get("media_url") or ""
    if not media_url:
        print("[make] FATAL: _meta.media_url が空。fetch_videos.py の出力を確認。")
        return 2

    # 1. 元動画ダウンロード
    source_path = WORK_DIR / "source.mp4"
    try:
        download_video(media_url, source_path)
    except Exception as exc:
        print(f"[make] FATAL: ダウンロード失敗: {exc}")
        return 2

    # 2. clip 切り出し (1080x1920 フルスクリーン)
    clip = script["clip"]
    clip_path = WORK_DIR / "clip.mp4"
    try:
        cut_clip(source_path, float(clip["start_sec"]), float(clip["end_sec"]), clip_path)
    except Exception as exc:
        print(f"[make] FATAL: clip 切り出し失敗: {exc}")
        return 3

    # 3. 全シーンの VOICEVOX 合成 (第1ループ)
    # 各シーンの音声 wav と継続時間を先に全部確定させ、合計 = 総尺を計算する。
    scene_data: list[dict[str, Any]] = []
    for i, sc in enumerate(script["scenes"]):
        speaker = sc["speaker"]
        # Gemini が稀に \n / 実改行 / \r を text に混入することがある。
        # VOICEVOX (発音される) と drawtext (filter parser エラーになる) の両方を
        # 壊すので、ここで一括除去してから両者に同じ文字列を渡す。
        raw_text = sc["text"]
        text = (
            raw_text.replace("\\n", "")
            .replace("\n", "")
            .replace("\r", "")
        )
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
        duration += 0.3  # 末尾余韻

        scene_data.append({
            "speaker": speaker,
            "text": text,
            "audio_path": audio_path,
            "duration": duration,
        })

    total_scene_duration = sum(s["duration"] for s in scene_data)
    print(f"[make] 全シーン synth 完了: {len(scene_data)} シーン、合計 {total_scene_duration:.2f}s")

    # 4. bg.mp4 を総尺ぶん事前レンダリング (clip がループしてもこれで一発化)
    bg_path = WORK_DIR / "bg.mp4"
    try:
        render_bg(clip_path, total_scene_duration, bg_path)
    except Exception as exc:
        print(f"[make] FATAL: bg レンダリング失敗: {exc}")
        return 7
    print(f"[make] bg dim={ffprobe_dim(bg_path)}")

    # 5. 各シーンを compose (第2ループ、bg.mp4 を offset で seek)
    cumulative_offset = 0.0
    scene_videos: list[Path] = []
    for i, s in enumerate(scene_data):
        scene_video = scenes_dir / f"scene_{i:02d}.mp4"
        try:
            compose_scene(
                clip_path=bg_path,         # bg.mp4 を渡す (clip ではなく)
                speaker=s["speaker"],
                text=s["text"],
                audio_path=s["audio_path"],
                duration=s["duration"],
                dest=scene_video,
                font_path=font_path,
                text_dir=scenes_dir,
                scene_index=i,
                clip_offset=cumulative_offset,
            )
        except Exception as exc:
            print(f"[make] FATAL: scene[{i}] 描画失敗: {exc}")
            return 8
        scene_videos.append(scene_video)
        cumulative_offset += s["duration"]

    # 6. concat
    presentation_path = WORK_DIR / "presentation.mp4"
    try:
        concat_scenes(scene_videos, presentation_path)
    except Exception as exc:
        print(f"[make] FATAL: concat 失敗: {exc}")
        return 9

    total_duration = ffprobe_duration(presentation_path)
    print(f"[make] 総再生時間: {total_duration:.2f}s")

    # 6. BGM mix
    try:
        mix_bgm(presentation_path, OUTPUT_PATH, total_duration)
    except Exception as exc:
        print(f"[make] FATAL: BGM mix 失敗: {exc}")
        return 10

    print(f"[make] 完成: {OUTPUT_PATH} ({total_duration:.2f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
