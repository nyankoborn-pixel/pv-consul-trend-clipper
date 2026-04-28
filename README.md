# pv-consul-trend-clipper

海外の公的機関(NASA / SpaceX / USGS / NOAA / 米軍 など)や CC BY ライセンス映像から
衝撃映像をピックアップし、執事猫キャラ「ニャンコンサル」(青山龍星 voice)と
ずんだもんが解説を被せた YouTube Shorts を **1日4回自動投稿** するシステムです。

完全自動運用 (HOTL: Human On The Loop)。投稿前のレビューはありません。

---

## 仕組み

```
cron-job.org
    │ (HTTPS API → workflow_dispatch)
    ▼
GitHub Actions (auto_post.yml)
    │
    ├─ 1. fetch_videos.py    : ソースから候補動画を収集 (RSS + YouTube Search API)
    ├─ 2. select_video.py    : 重複除外 + weight & 投稿日時で1本選定
    ├─ 3. generate_script.py : Gemini で台本 + クリップ秒数 + タイトル生成
    ├─ 4. make_video.py      : yt-dlp DL → ffmpeg 切り出し → 元音声カット
    │                          → VOICEVOX 合成 → 立ち絵 + 字幕 + BGM mix
    └─ 5. upload_youtube.py  : YouTube Data API v3 で投稿 + 出典明記
```

---

## 著作権安全策

- ソースは **公的機関 (NASA 等) または CC BY ライセンス映像のみ**
- 元音声は **完全カット**(自前 BGM + 解説のみ被せる)
- 概要欄に **出典 URL を必ず明記**
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

| Secret 名 | 内容 |
| --- | --- |
| `GEMINI_API_KEY` | Google AI Studio の Gemini API キー |
| `YOUTUBE_API_KEY` | YouTube Data API v3 キー (search.list 用) |
| `YOUTUBE_CLIENT_ID` | OAuth クライアント ID |
| `YOUTUBE_CLIENT_SECRET` | OAuth クライアントシークレット |
| `YOUTUBE_REFRESH_TOKEN` | OAuth リフレッシュトークン |
| `YOUTUBE_COOKIES` | (任意) Netscape 形式 cookies.txt の全文。bot 検知された時の最終フォールバック |

> logs/ への commit & push はワークフローの `GITHUB_TOKEN` (permissions: contents: write) で行うため、追加の PAT は不要です。
> cron-job.org から workflow_dispatch を叩く用途だけ別途 GitHub PAT が必要 (リポジトリ Secrets ではなく cron-job.org 側に設定)。

#### YOUTUBE_COOKIES の取得方法 (bot 検知された時用)

GitHub Actions の IP 帯は YouTube に bot として検知されやすく、`Sign in to confirm you're not a bot` でエラーになることがあります。
通常は `player_client=tv,web_safari,mweb,android` のフォールバックで回避しますが、それでも失敗する場合は cookies を渡してください。

1. Chrome / Firefox の拡張機能 [Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc) などを使い、`youtube.com` ドメインの cookies を Netscape 形式でエクスポート
2. ファイル全文を `YOUTUBE_COOKIES` Secret に貼り付け
3. ワークフロー側で自動的に `--cookies` に渡されます

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
├── config/sources.yml       # 動画ソース定義
├── src/
│   ├── fetch_videos.py
│   ├── select_video.py
│   ├── generate_script.py
│   ├── make_video.py
│   └── upload_youtube.py
├── logs/                    # 投稿履歴 (jsonl)
├── .gitignore
└── README.md
```

---

## ライセンス / 注意

- 本リポジトリのコードは private 運用前提
- 投稿動画の著作権は元映像の権利者に帰属。本プロジェクトは出典明記の上で
  CC BY または public domain ライセンス相当の映像のみを利用する
