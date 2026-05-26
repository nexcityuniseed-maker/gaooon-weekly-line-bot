# GaoooN 日次レポート → LINE 公式アカウント配信

毎朝6:00（JST）に、GaoooN の店舗評価アンケートと Google クチコミから低評価フィードバックを抽出し、
Claude API で要約して LINE 公式アカウントの友達全員にブロードキャスト配信します。

## 構成

```
[GitHub Actions cron 06:00 JST]
   ↓
[Playwright で GaoooN 自動ログイン]
   ↓
[アンケート低評価 + Google クチコミ取得]
   ↓
[Claude API で要約]
   ↓
[LINE 公式アカウント broadcast API → 友達全員]
```

## 対象店舗（初期は1店舗でPoC）

| 店舗名 | location |
|---|---|
| 中華そば二兎 | `chukasobanito_nagoya` |

## ローカル動作確認

```bash
# 1. 仮想環境
python3 -m venv .venv
source .venv/bin/activate

# 2. 依存
pip install -r requirements.txt
python -m playwright install chromium

# 3. 環境変数
cp .env.example .env
# .env を編集して各値を埋める

# 4. ドライラン（LINE送信せず標準出力のみ）
python daily_report.py

# 5. 実送信（注意: 本番LINEに届きます）
SEND_MODE=send python daily_report.py
```

## GitHub Actions で本番運用するには

### Step 1. このディレクトリをGitリポジトリにする

```bash
cd /Users/kosuke/Desktop/flead_meo/daily_line_bot
git init
git add .
git commit -m "Initial commit"
gh repo create gaooon-daily-line --private --source=. --push
```

### Step 2. GitHub Secrets を設定

リポジトリの「Settings → Secrets and variables → Actions → New repository secret」で以下を登録：

| Secret 名 | 値 |
|---|---|
| `GAOOON_EMAIL` | `d-iguchi@bw-c.com` |
| `GAOOON_PASSWORD` | (パスワード) |
| `ANTHROPIC_API_KEY` | `sk-ant-...` |
| `LINE_CHANNEL_ACCESS_TOKEN` | (公式LINE作成後に発行) |

### Step 3. Variables（任意・店舗を変えたい場合）

「Settings → Secrets and variables → Actions → Variables タブ」で：

| Variable 名 | 値 |
|---|---|
| `STORE_LOCATION` | `chukasobanito_nagoya` |
| `STORE_NAME` | `中華そば二兎` |

### Step 4. 動作確認

「Actions」タブ → ワークフロー名「GaoooN 日次レポート → LINE配信」→ Run workflow で手動起動 → LINE着信を確認。

### Step 5. 翌朝6:00を待つ

cron スケジュールにより自動実行。

## 店舗を追加するには

`.github/workflows/daily.yml` を Matrix Strategy 化するか、または別ワークフローファイルを店舗分作成。
1店舗が安定稼働してから検討する。

## トラブルシューティング

| 現象 | 確認ポイント |
|---|---|
| ログイン失敗 | パスワード変更 / アカウントロック |
| データ取得空 | サイトのHTML構造変更 → セレクタを `parse_*` 関数で要修正 |
| 要約が変 | Claude API レート / モデル名 / プロンプト |
| LINE送信失敗 | Token失効 / 公式アカウントBan / Bot友達0 |

## 仕様メモ

- LINEメッセージは2,000字制限あり。超える場合は自動で複数吹き出しに分割
- LINE 公式アカウントの broadcast は無料プラン200通/月（5人友達×30日=150通で余裕）
- Anthropic API は claude-sonnet-4-5 を使用
- Playwright はヘッドレス Chromium、JST 06:00 起動
