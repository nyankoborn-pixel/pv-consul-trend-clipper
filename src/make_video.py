"""動画を合成する (フルスクリーン動画 + 立ち絵オーバーレイ方式)。

work/script.json (generate_script.py 出力) を入力に:
1. media_url (各 API の MP4 直リンク) を requests ストリームでダウンロード
2. clip 区間 (start_sec, end_sec) を 1080x1920 (フルスクリーン縦) で切り出し
   元音声はカット。実動画長より end_sec が大きければ自動で丸める。
   start_sec≈0 で動画が要求の 2.5 倍以上長い場合はランダム中盤切出し。
3. シーンごとに VOICEVOX で音声合成
4. シーンごとに 1080x1920 の完成動画 (clip ループ + 立ち絵 + 字幕帯 + 音声) を生成
   - 話者が nyanko なら立ち絵を右下、zundamon なら左下に配置
   - 立ち絵は scale=400:-1、底辺を字幕帯の上端 (y=1490) に揃える
   - 字幕帯は y=1500..1920 の半透明黒 (alpha 0.6) + 中央配置 56pt 白文字
5. 全シーンを concat → presentation.mp4
6. assets/bgm.mp3 を -12dB で mix → output.mp4

立ち絵 PNG が無い場合はエラーで停止する (placeholder 自動生成は廃止)。
"""

from __future__ import annotations

import json
import os
import random
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
# - 立ち絵: scale=CHAR_W:-1 (アスペクト比維持)、画面端から CHAR_MARGIN_X
#           底辺 y=CHAR_BOTTOM_Y (字幕帯の上端 1430 - 余白 10)
# - 字幕帯: y=SUBTITLE_BAND_Y..(SUBTITLE_BAND_Y+SUBTITLE_BAND_H)、半透明黒
# - 字幕文字: 帯の上端 + TOP_PAD から配置 (top-anchored)、長文時は
#             下端から BOTTOM_SAFE 余白を確保するようクランプ。白文字 + 黒縁。
CHAR_W = 400
CHAR_MARGIN_X = 40
CHAR_BOTTOM_Y = 1420  # 立ち絵の底辺の y 座標 (字幕帯上端 1430 より 10px 上)
SUBTITLE_BAND_Y = 1430
SUBTITLE_BAND_H = 420
SUBTITLE_BAND_ALPHA = 0.6
SUBTITLE_FONTSIZE = 56
SUBTITLE_WRAP_CHARS = 18  # 1 行あたり最大文字数 (fontsize 56 で 1080px に収まる)
SUBTITLE_LINE_SPACING = 14
SUBTITLE_BORDERW = 4
SUBTITLE_TOP_PAD = 40       # 字幕帯の上端から文字までの余白
SUBTITLE_BOTTOM_SAFE = 70   # フレーム下端から確保する安全マージン

# キャラ → VOICEVOX speaker_id
SPEAKER_IDS = {
    "zundamon": 3,
    "nyanko": 13,  # 青山龍星
}

# 話者ごとの立ち絵配置 (right=右下、left=左下)
SPEAKER_POSITIONS = {
    "nyanko": "right",
    "zundamon": "left",
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

    - end_sec が動画長を超えていれば丸める
    - start_sec≈0 で動画が要求の 2.5 倍以上長い場合は中盤からランダム切出し
    - 結果クリップが極端に短い場合 (< 3 s) はエラー
    - 横長ソースは center crop で 1080x1920 に fit
    """
    try:
        actual_duration = ffprobe_duration(src)
    except Exception as exc:
        print(f"[make] WARNING: 元動画の duration 取得失敗 ({exc})。指定値で続行。")
        actual_duration = float("inf")

    requested_duration = end_sec - start_sec

    if (
        actual_duration != float("inf")
        and start_sec < 0.5
        and actual_duration > requested_duration * 2.5
        and requested_duration > 0
    ):
        lower = max(2.0, actual_duration * 0.10)
        upper = max(lower + 1.0, actual_duration * 0.70 - requested_duration)
        if upper > lower:
            new_start = random.uniform(lower, upper)
            new_end = new_start + requested_duration
            if new_end <= actual_duration - 0.5:
                print(
                    f"[make] 元動画 {actual_duration:.1f}s は要求の {requested_duration:.1f}s "
                    f"より十分長い → 静止画化対策で start を 0s → {new_start:.1f}s に変更"
                )
                start_sec = new_start
                end_sec = new_end

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
    # ぼかし背景 + 中央フィット (TikTok/Shorts 定番):
    # 1. split で元動画を 2 系統に分岐
    # 2. bg: cover-crop で 1080x1920 に拡大 + gblur (背景レイヤー)
    # 3. fg: 元アスペクト比のまま 1080x1920 に decrease で fit
    # 4. bgblur に fgfit を中央 overlay
    # → 画面全体がぼかし背景で埋まり、元動画は全体が見切れず中央に表示される
    filter_complex = (
        f"[0:v]split=2[bg][fg];"
        f"[bg]scale={W}:{H}:force_original_aspect_ratio=increase,"
        f"crop={W}:{H},gblur=sigma=20[bgblur];"
        f"[fg]scale={W}:{H}:force_original_aspect_ratio=decrease[fgfit];"
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


def character_image_path(speaker: str, emotion: str) -> Path:
    """話者+表情から立ち絵パスを返す。

    指定 emotion の PNG が無ければ {speaker}_normal.png にフォールバック。
    normal すら無ければ FileNotFoundError (placeholder 自動生成はしない)。
    """
    base = ASSETS_DIR / speaker / f"{speaker}_{emotion}.png"
    if base.exists():
        return base
    fallback = ASSETS_DIR / speaker / f"{speaker}_normal.png"
    if fallback.exists():
        print(f"[make] WARNING: {base.name} が無いので {fallback.name} を使用")
        return fallback
    # emotion=="normal" だと base と fallback が同一パスを指すので順序維持で重複排除
    search_paths = list(dict.fromkeys([str(base), str(fallback)]))
    paths_block = "\n".join(f"  - {p}" for p in search_paths)
    raise FileNotFoundError(
        "立ち絵 PNG が配置されていません。次のパスに PNG を配置してください:\n"
        f"{paths_block}\n"
        "(プレースホルダ自動生成は廃止されました。本番アセットを必ず配置してください)"
    )


def verify_required_assets(script: dict[str, Any]) -> int:
    """script.json の全シーンが要求する立ち絵 PNG が揃っているか早期チェック。

    各シーンで primary ({speaker}_{emotion}.png) と fallback ({speaker}_normal.png)
    のどちらも無いものを集めて一括レポート。VOICEVOX 合成や ffmpeg 起動前に止める。

    Returns:
        0  : 全シーン解決可能
        11 : 1 件でも解決不能あり (main() がそのまま return code として使う)
    """
    scenes = script.get("scenes", [])
    issues: list[tuple[int, str, str, Path]] = []
    for i, sc in enumerate(scenes):
        speaker = sc.get("speaker") or ""
        emotion = sc.get("emotion") or "normal"
        primary = ASSETS_DIR / speaker / f"{speaker}_{emotion}.png"
        fallback = ASSETS_DIR / speaker / f"{speaker}_normal.png"
        if primary.exists() or fallback.exists():
            continue
        issues.append((i, speaker, emotion, primary))

    if not issues:
        print(f"[make] 立ち絵チェック OK: 全 {len(scenes)} シーンで PNG 解決可能")
        return 0

    # 配置すべきユニークなパス (順序維持)
    missing_unique = list(dict.fromkeys(p for _, _, _, p in issues))
    affected = ", ".join(f"scene[{i}]({sp},{em})" for i, sp, em, _ in issues)
    print(
        f"[make] FATAL: 立ち絵 PNG 不足。{len(missing_unique)} ファイル要配置、"
        f"{len(issues)} シーンが解決不能:"
    )
    for p in missing_unique:
        print(f"  - {p}")
    print(f"[make]   該当シーン: {affected}")
    print("[make] (VOICEVOX 合成前に検出。assets/ に PNG を配置してください)")
    return 11


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
    """ffmpeg drawtext text= の値用にエスケープする (引用符を使わない方式)。"""
    text = text.replace("\\", "\\\\")
    text = text.replace(":", "\\:")
    text = text.replace(",", "\\,")
    text = text.replace(";", "\\;")
    text = text.replace("'", "\\'")
    text = text.replace("%", "\\%")
    text = text.replace("[", "\\[")
    text = text.replace("]", "\\]")
    text = text.replace("=", "\\=")
    text = text.replace("\n", "\\n")
    return text


def compose_scene(
    clip_path: Path,
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
    """シーン 1 本分の完成動画 (1080x1920) を生成する。

    レイアウト:
    - 背景: clip_path をループ (-stream_loop -1) でフルスクリーン
    - 立ち絵: scale=CHAR_W:-1、底辺 y=CHAR_BOTTOM_Y、話者に応じて左/右配置
    - 字幕帯: drawbox y=SUBTITLE_BAND_Y h=SUBTITLE_BAND_H 半透明黒
    - 字幕: drawtext text= 直書き (textfile= 不使用、エスケープ方式)
    - 音声: VOICEVOX wav (BGM はこの段階では混ぜない)
    """
    char_img = character_image_path(speaker, emotion)
    position = SPEAKER_POSITIONS.get(speaker, "right")
    if position == "right":
        char_x_expr = f"main_w-overlay_w-{CHAR_MARGIN_X}"
    else:
        char_x_expr = f"{CHAR_MARGIN_X}"
    # 立ち絵の底辺を CHAR_BOTTOM_Y に揃える (画像の高さに依存しない)
    char_y_expr = f"{CHAR_BOTTOM_Y}-overlay_h"

    # text は main() 側で改行クリーン済み (VOICEVOX と drawtext で同一の文字列)。
    # ここで wrap_jp_text() による機械的な折返しのみ適用する。
    wrapped = wrap_jp_text(text)
    escaped_text = escape_drawtext_text(wrapped)
    # デバッグ用: シーンごとのテキストを残す
    text_file = text_dir / f"scene_{scene_index:02d}_text.txt"
    text_file.write_text(wrapped, encoding="utf-8")
    print(f"[make] scene[{scene_index}] speaker={speaker} pos={position} text={wrapped!r}")

    fontfile_arg = font_path.replace("\\", "/").replace(":", "\\:")

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
        # 立ち絵を CHAR_W に幅固定でスケール (高さは比率維持)
        f"[1:v]scale={CHAR_W}:-1[char];"
        # ベースに立ち絵を overlay (右下 or 左下、底辺 y=CHAR_BOTTOM_Y)
        f"[v0][char]overlay=x={char_x_expr}:y={char_y_expr}[withchar];"
        # 字幕帯 (半透明黒): y=SUBTITLE_BAND_Y から H まで
        f"[withchar]drawbox=x=0:y={SUBTITLE_BAND_Y}:w={W}:h={SUBTITLE_BAND_H}:"
        f"color=black@{SUBTITLE_BAND_ALPHA}:t=fill[withbox];"
        # 字幕テキスト
        f"[withbox]drawtext="
        f"fontfile='{fontfile_arg}':"
        f"text={escaped_text}:"
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
            # input 0: ベース動画 (clip をループ)
            "-stream_loop", "-1",
            "-i", str(clip_path),
            # input 1: 立ち絵 PNG (静止画なので -loop 1 + -t)
            "-loop", "1", "-t", str(duration), "-i", str(char_img),
            # input 2: VOICEVOX 音声
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
        desc=f"compose scene[{scene_index}] → {dest}",
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
            "-c:a", "aac", "-b:a", "192k",
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

    # 立ち絵 PNG 不足を VOICEVOX 合成や clip ダウンロード前に検出
    rc = verify_required_assets(script)
    if rc != 0:
        return rc

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

    # 3, 4. シーンごとに VOICEVOX 合成 + 完成シーン動画生成
    scene_videos: list[Path] = []
    for i, sc in enumerate(script["scenes"]):
        speaker = sc["speaker"]
        emotion = sc.get("emotion", "normal")
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

        scene_video = scenes_dir / f"scene_{i:02d}.mp4"
        try:
            compose_scene(
                clip_path=clip_path,
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
    presentation_path = WORK_DIR / "presentation.mp4"
    try:
        concat_scenes(scene_videos, presentation_path)
    except Exception as exc:
        print(f"[make] FATAL: concat 失敗: {exc}")
        return 8

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
