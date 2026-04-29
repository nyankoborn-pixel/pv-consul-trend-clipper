# pv-consul-trend-clipper

公式 API・パブリックドメイン・フリー素材ライセンスの映像から衝撃映像をピックアップし、
執事猫キャラ「ニャンコンサル」(青山龍星 voice)とずんだもんが解説を被せた
YouTube Shorts を **1日4回自動投稿** するシステムです。

完全自動運用 (HOTL: Human On The Loop)。投稿前のレビューはありません。

---

## 仕組み

```
cron-job.org
    │ (HTTPS API → workflow_dispatch)
    ▼
GitHub Actions (auto_post.yml)
    │
    ├─ 1. fetch_videos.py    : 公式 API から候補動画を収集
    │                          (Pixabay / Pexels / NASA / USGS / Internet Archive)
    ├─ 2. select_video.py    : 重複除外 + weight でソート → 1本選定
    ├─ 3. generate_script.py : Gemini で台本 + クリップ秒数 + タイトル生成
    ├─ 4. make_video.py      : requests で MP4 直 DL → ffmpeg 切り出し → 元音声カット
    │                          → VOICEVOX 合成 → 立ち絵 + 字幕 + BGM mix
    └─ 5. upload_youtube.py  : YouTube Data API v3 で投稿 + 出典明記
```

---

## ソース一覧

| ソース | type | weight | 認証 | ライセンス |
| --- | --- | --- | --- | --- |
| [Pixabay Video API](https://pixabay.com/api/docs/) | `pixabay` | 8 | `PIXABAY_API_KEY` 必須 | Pixabay License |
| [Pexels Video API](https://www.pexels.com/api/) | `pexels` | 8 | `PEXELS_API_KEY` 必須 | Pexels License |
| [NASA Image and Video Library](https://images-api.nasa.gov) | `nasa` | 9 | `NASA_API_KEY` 任意 | Public Domain |
| USGS Volcano Hazards multimedia | `usgs_volcano` | 7 | 不要(スクレイピング) | Public Domain |
| [Internet Archive](https://archive.org/help/aboutapi.php) | `internet_archive` | 5 | 不要 | per-item |

> **YouTube ソースは廃止しました**。yt-dlp の bot 検知突破と cookies 更新の運用負荷を回避するため、公式 API から MP4 を直接取得する方式に統一しています。

---

## 著作権安全策

- ソースは **公式 API・パブリックドメイン・CC0/フリー素材ライセンス** のみ
- 元音声は **完全カット**(自前 BGM + 解説のみ被せる)
- 概要欄に **出典 URL とライセンスを必ず明記**
- 二次創作であることを概要欄で明示

---

## セットアップ

### 1. リポジトリを clone

```bash
git clone https://github.com/<owner>/pv-consul-trend-clipper.git
cd pv-consul-trend-clipper
```

### 2. アセットファイルを `assets/` に配置

| ファイル | 内容 |
| --- | --- |
| `assets/nyanko/nyanko_normal.png` | ニャンコンサル立ち絵 (通常) |
| `assets/nyanko/nyanko_happy.png` | ニャンコンサル立ち絵 (喜) |
| `assets/nyanko/nyanko_surprised.png` | ニャンコンサル立ち絵 (驚) |
| `assets/nyanko/nyanko_thinking.png` | ニャンコンサル立ち絵 (考) |
| `assets/zundamon/zundamon_normal.png` | ずんだもん立ち絵 (通常) |
| `assets/zundamon/zundamon_happy.png` | ずんだもん立ち絵 (喜) |
| `assets/zundamon/zundamon_surprised.png` | ずんだもん立ち絵 (驚) |
| `assets/zundamon/zundamon_thinking.png` | ずんだもん立ち絵 (考) |
| `assets/bgm.mp3` | BGM (しゃろう氏「3:03 PM」など) |

### 3. GitHub Secrets を登録

リポジトリの `Settings → Secrets and variables → Actions` で以下を登録:

| Secret 名 | 必須 | 取得先 |
| --- | --- | --- |
| `GEMINI_API_KEY` | ✓ | https://aistudio.google.com/apikey |
| `PIXABAY_API_KEY` | ✓ | https://pixabay.com/api/docs/ (無料アカウント) |
| `PEXELS_API_KEY` | ✓ | https://www.pexels.com/api/new/ (無料アカウント) |
| `NASA_API_KEY` | 任意 | https://api.nasa.gov/ (未設定なら DEMO_KEY 利用) |
| `YOUTUBE_CLIENT_ID` | ✓ | Google Cloud Console (OAuth 2.0 クライアント) |
| `YOUTUBE_CLIENT_SECRET` | ✓ | 同上 |
| `YOUTUBE_REFRESH_TOKEN` | ✓ | OAuth Playground で `youtube.upload` スコープ取得 |

> logs/ への commit & push はワークフローの `GITHUB_TOKEN` (permissions: contents: write) で行うため、追加の PAT は不要です。
> cron-job.org から workflow_dispatch を叩く用途だけ別途 GitHub PAT が必要 (リポジトリ Secrets ではなく cron-job.org 側に設定)。

#### 旧 Secrets (削除推奨、残してても無視されるだけ)

YouTube ソース廃止に伴い以下は不要になりました:

- `YOUTUBE_COOKIES` (yt-dlp の bot 検知回避用だった)
- `YOUTUBE_API_KEY` (search.list での CC BY 検索用だった)

### 4. cron-job.org にジョブ登録

`https://cron-job.org/` で以下4本のジョブを登録(JST):

| 時刻 (JST) | URL |
| --- | --- |
| 07:00 | `https://api.github.com/repos/<owner>/pv-consul-trend-clipper/actions/workflows/auto_post.yml/dispatches` |
| 12:00 | 同上 |
| 18:00 | 同上 |
| 22:00 | 同上 |

リクエスト設定:
- Method: `POST`
- Headers:
  - `Authorization: Bearer <GitHub PAT (workflow scope)>`
  - `Accept: application/vnd.github+json`
- Body: `{"ref":"main"}`

---

## 動作確認

1. GitHub Actions 画面で `Auto Post YouTube Shorts (Trend Clipper)` を手動 dispatch
2. `dry_run=true` を指定して動画生成のみ確認 (アップロードはスキップ)
3. 生成された `output.mp4` を artifact からダウンロードして確認
4. 問題なければ `dry_run=false` で本番投稿テスト
5. YouTube Studio で投稿先チャンネルを確認

---

## ディレクトリ構成

```
pv-consul-trend-clipper/
├── .github/workflows/auto_post.yml
├── assets/                  # 立ち絵 / BGM (手動配置)
├── config/sources.yml       # 動画ソース定義 (5 ソース)
├── requirements.txt
├── src/
│   ├── fetch_videos.py      # Pixabay / Pexels / NASA / USGS / IA から候補収集
│   ├── select_video.py      # weight でソート → 1本選定
│   ├── generate_script.py   # Gemini で台本生成
│   ├── make_video.py        # requests で MP4 DL → ffmpeg 合成
│   └── upload_youtube.py    # YouTube Data API v3 で投稿
├── logs/                    # 投稿履歴 (jsonl)
├── CREDITS.md               # サードパーティ素材・依存のクレジット
├── LICENSE                  # MIT License (コードに適用、assets/ は別ライセンス)
├── .gitignore
└── README.md
```

---

## Credits

本リポジトリは **MIT License**(コード本体)で配布されています。
詳細なライセンス文は [`LICENSE`](LICENSE) を参照してください。

`assets/` 配下のサードパーティ素材(キャラクター画像・BGM)、および利用している
ライブラリ・API のクレジット情報は [`CREDITS.md`](CREDITS.md) に集約しています。

主要な利用先:

- **音声合成**: [VOICEVOX](https://voicevox.hiroshiba.jp/) © Hiroshiba Kazuyuki
  (キャラクター: ずんだもん / 青山龍星)
- **キャラクター画像**: ずんだもん([東北ずん子・ずんだもんプロジェクト](https://zunko.jp/con_ongen_kiyaku.html)) / ニャンコンサル(© nyankoborn-pixel)
- **BGM**: 「3:03 PM」 by しゃろう氏(クレジット表記で許諾されているフリー音源)
- **動画素材 API**: [Pixabay](https://pixabay.com/api/docs/) / [Pexels](https://www.pexels.com/api/) / [NASA Image and Video Library](https://images.nasa.gov/) / [USGS](https://www.usgs.gov/programs/VHP) / [Internet Archive](https://archive.org/)
- **動画/音声処理**: [ffmpeg](https://ffmpeg.org/) / [Gemini API](https://ai.google.dev/)
- **字幕フォント**: [Noto Sans CJK](https://github.com/notofonts/noto-cjk) (SIL OFL 1.1、ランナー上で apt 経由インストール)

各動画素材の出典は **生成された YouTube Shorts の動画概要欄** にも自動的に明記されます。

---

## ライセンス / 注意

- **コード**: MIT License([`LICENSE`](LICENSE) 参照)
- **assets/ 配下の素材**: 個別ライセンスが適用([`CREDITS.md`](CREDITS.md) 参照)
- **生成動画の著作権**: 元映像の権利者に帰属。本プロジェクトは出典明記の上で
  Pixabay License / Pexels License / Public Domain (NASA/USGS) /
  Internet Archive のライセンスに従う
- **API キー / 認証情報**: リポジトリには含めず、GitHub Actions の Secrets として
  管理。リポジトリを fork / clone しても秘匿情報は引き継がれない
