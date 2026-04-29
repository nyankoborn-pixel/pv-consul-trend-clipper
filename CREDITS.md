# CREDITS

このリポジトリは MIT License で配布されますが、`assets/` 配下のサードパーティ素材
および利用しているライブラリ/API には個別のライセンスが適用されます。
本ドキュメントは出典・著作権者・利用条件を明示するためのものです。

各動画素材の出典は **生成された YouTube Shorts の動画概要欄** にも自動的に明記されます
(`src/upload_youtube.py` のテンプレ参照)。

---

## キャラクター画像

### ニャンコンサル (`assets/nyanko/*.png`, `assets/bg_default.png`)

- **著作者**: © 2026 nyankoborn-pixel
- **ライセンス**: 本リポジトリの一部として MIT License で配布
- **備考**: オリジナルキャラクター。表情差分(normal / happy / surprised / thinking)を含む

### ずんだもん (`assets/zundamon/*.png`)

- **著作者**: 東北ずん子・ずんだもんプロジェクト(SSS合同会社)
- **ライセンス**: 東北ずん子・ずんだもん利用規約 に準拠
- **規約 URL**: <https://zunko.jp/con_ongen_kiyaku.html>
- **本リポジトリでの利用**: 立ち絵画像 4 種(normal / happy / surprised / thinking)を
  解説動画の話者表現として使用。商用利用に該当する場合は規約に従う

---

## 音声合成

### VOICEVOX

- **提供元**: © Hiroshiba Kazuyuki
- **公式サイト**: <https://voicevox.hiroshiba.jp/>
- **利用規約**: <https://voicevox.hiroshiba.jp/term/>
- **本リポジトリでの利用**: GitHub Actions のサービスコンテナ
  (`voicevox/voicevox_engine:cpu-latest`) として呼び出し、シーンごとの音声を生成

#### 使用キャラクター(speaker_id)

| キャラクター | speaker_id | 役割 |
|---|---:|---|
| ずんだもん(ノーマル) | 3 | ずんだもん話者 |
| 青山龍星(ノーマル) | 13 | ニャンコンサル話者 |

各キャラクター個別の利用規約は VOICEVOX 公式の各キャラクターページに従います。

---

## BGM

### 「3:03 PM」 by しゃろう

- **作曲者**: しゃろう氏
- **入手元**: 商用利用・帰属表示で許諾されているフリー音源として入手
- **利用条件**: クレジット表記
- **本リポジトリでの利用**: `assets/bgm.mp3` として配置、生成動画の背景 BGM
  (-12 dB ducking 適用) として使用

---

## 動画素材

生成される動画の上半分(背景動画)は、以下の API / アーカイブから取得した素材を使用します。
個別の動画には各々のライセンスが適用され、出典は YouTube 概要欄に記載されます。

| ソース | API/URL | ライセンス |
|---|---|---|
| Pixabay | <https://pixabay.com/api/docs/> | Pixabay License |
| Pexels | <https://www.pexels.com/api/> | Pexels License |
| NASA Image and Video Library | <https://images.nasa.gov/> | Public Domain (NASA media usage guidelines) |
| USGS Volcano Hazards | <https://www.usgs.gov/programs/VHP> | Public Domain (U.S. Government work) |
| Internet Archive | <https://archive.org/> | per-item(動画ごとに異なる) |

> Internet Archive 由来の動画は、各 item ページに表示されているライセンスに従います。
> Public Domain / CC0 / CC BY 等が混在するため、`config/sources.yml` のクエリ調整時は
> 確認推奨。

---

## ライブラリ / 依存

主要な Python 依存(詳細は `requirements.txt`):

| 名称 | ライセンス | 用途 |
|---|---|---|
| [google-genai](https://pypi.org/project/google-genai/) | Apache 2.0 | Gemini API クライアント |
| [google-api-python-client](https://github.com/googleapis/google-api-python-client) | Apache 2.0 | YouTube Data API v3 |
| [google-auth-oauthlib](https://pypi.org/project/google-auth-oauthlib/) | Apache 2.0 | YouTube OAuth |
| [requests](https://pypi.org/project/requests/) | Apache 2.0 | HTTP クライアント |
| [tenacity](https://pypi.org/project/tenacity/) | Apache 2.0 | リトライ制御 |
| [beautifulsoup4](https://pypi.org/project/beautifulsoup4/) | MIT | HTML パース(USGS スクレイピング用) |
| [pyyaml](https://pypi.org/project/PyYAML/) | MIT | sources.yml 読込 |
| [python-dateutil](https://pypi.org/project/python-dateutil/) | Apache 2.0 / BSD 3-Clause | 日時パース |

外部ツール:

| 名称 | ライセンス | 用途 |
|---|---|---|
| [ffmpeg](https://ffmpeg.org/) | LGPL 2.1+ / GPL 2.0+ | 動画/音声合成、エンコード |
| [Noto Sans CJK](https://github.com/notofonts/noto-cjk) | SIL Open Font License 1.1 | 字幕の日本語フォント(`apt install fonts-noto-cjk` で取得) |

---

## API キー / 認証情報

本リポジトリには **API キー、OAuth クライアントシークレット、リフレッシュトークン
を一切含めていません**。これらは GitHub Actions の Secrets で管理し、
ワークフロー実行時に環境変数として注入されます。

詳細は `README.md` のセットアップ節を参照してください。

---

## クレジット表記の追加・修正について

新しい素材を `assets/` に追加する、または既存素材の入手元/ライセンスが変更された場合は、
本ファイルを更新してから commit してください。
