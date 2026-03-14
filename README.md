# 🤖 Dangerbot — Discord × GitHub Copilot SDK ブリッジ

[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Discord Gateway](https://img.shields.io/badge/Discord-Gateway%20WebSocket-5865F2.svg)](https://discord.com/developers/docs/topics/gateway)

Discord のメッセージを **`github-copilot-sdk`**（`CopilotClient`）経由で GitHub Copilot にリアルタイムに橋渡しするボットです。  
Discord に自然言語で指示を書くだけで、Copilot がコードを書き・修正し・コマンドを実行し、結果を Discord に返信します。

---

## 目次

1. [概要](#概要)
2. [必要なもの](#必要なもの)
3. [インストール](#インストール)
4. [Discord Bot の設定](#discord-bot-の設定)
5. [環境変数の設定](#環境変数の設定)
6. [起動方法](#起動方法)
7. [systemd での常時起動](#systemd-での常時起動)
8. [スラッシュコマンド](#スラッシュコマンド)
9. [トラブルシューティング](#トラブルシューティング)
10. [エージェントへの修復依頼方法](#エージェントへの修復依頼方法)
11. [開発・カスタマイズ](#開発カスタマイズ)
12. [ライセンス](#ライセンス)

---

## 概要

### 何ができるのか

Dangerbot を起動すると、指定した Discord チャンネルを常時監視します。  
特定のユーザー（あなた）がそのチャンネルにメッセージを送ると、ボットが自動的に内容を GitHub Copilot CLI へ転送し、Copilot の返答を Discord に返信します。

**ユースケース例:**
- スマートフォンから「バグを修正して」と送るだけで、サーバー上で Copilot が自動修正する
- 「テストを実行してログを見せて」と送ると、結果が Discord に返ってくる
- 複数のチャンネルでそれぞれ異なるプロジェクトを管理する

### アーキテクチャ

```
Discord ユーザー
    │ (メッセージ)
    ▼
Discord Gateway WebSocket
    │ (INTERACTION_CREATE / MESSAGE_CREATE イベント)
    ▼
discord_to_copilot_bridge.py（このボット）
    │ (Python SDK 呼び出し)
    ▼
github-copilot-sdk（CopilotClient）
    │ (サブプロセス管理)
    ▼
copilot CLI バイナリ（内部で自動起動）
    │ (GitHub Copilot API 通信)
    ▼
GitHub Copilot API
    │ (レスポンステキスト)
    ▼
Discord REST API → チャンネルに返信
```

### 主な機能

| 機能 | 説明 |
|------|------|
| **Discord Gateway WebSocket** | ポーリングなし。リアルタイムイベント接続で即座に反応 |
| **スラッシュコマンド** | `/cancel`（処理中断）・`/model`（モデル切り替え）をサポート |
| **マルチチャンネル対応** | `--channel-id` で複数ブリッジを同時起動可能 |
| **自動引き継ぎ（takeover）** | ハートビート監視で停止したインスタンスを自動再起動 |
| **セッション継続** | ボット再起動後も同じ Copilot セッションを引き継ぐ |
| **進捗表示** | `⏳ 処理中 (Xs経過)` を約2秒ごとに更新 |
| **リアクション通知** | 受信時 👀、完了時 ✅ のリアクションを自動付与 |
| **インスタンスロック** | 同一チャンネルへの二重起動を防止 |

---

## 必要なもの

- **Python 3.11 以上**
  ```bash
  python3 --version  # 3.11.x 以上であることを確認
  ```

- **GitHub Copilot CLI**（`copilot` バイナリが PATH に存在すること）
  ```bash
  copilot --version  # バージョンが表示されれば OK
  ```
  インストール方法: [GitHub Copilot in the CLI 公式ドキュメント](https://docs.github.com/ja/copilot/github-copilot-in-the-cli)

  > ℹ️ **SDK が内部で自動的に `copilot` コマンドを起動**するため、直接操作は不要ですが、バイナリが PATH に存在している必要があります。

- **GitHub Copilot Python SDK**（`github-copilot-sdk` パッケージ）
  このブリッジは `CopilotClient` を通じて SDK 経由で Copilot と通信します。
  `pip install -r requirements.txt` で自動インストールされます。

- **GitHub Copilot の有効なサブスクリプション**
  ```bash
  gh auth status  # 認証済みであることを確認
  ```

- **Discord Bot トークン**（後述の手順で取得）

---

## インストール

### 1. リポジトリをクローン

```bash
git clone https://github.com/your-org/dangerbot-oss.git
cd dangerbot-oss
```

### 2. Python 依存パッケージをインストール

```bash
pip install -r requirements.txt
```

`requirements.txt` の内容:

```
websockets>=12.0
requests>=2.31.0
python-dotenv>=1.0.0
github-copilot-sdk>=0.1.0
```

### 3. 環境変数ファイルを作成

```bash
cp .env.example .env
```

次のセクションを参考に `.env` を編集してください。

---

## Discord Bot の設定

Bot トークンをまだ持っていない場合は、以下の手順で取得します。

### 1. Discord Developer Portal でアプリケーションを作成

1. [Discord Developer Portal](https://discord.com/developers/applications) を開く
2. 右上の **「New Application」** をクリック
3. アプリケーション名（例: `Dangerbot`）を入力して **「Create」**

### 2. Bot を追加してトークンを取得

1. 左メニューの **「Bot」** をクリック
2. **「Add Bot」** → **「Yes, do it!」**
3. **「Reset Token」** をクリックしてトークンを生成
4. 表示されたトークンをコピーして `.env` の `DISCORD_BOT_TOKEN` に貼り付ける

   > ⚠️ トークンは一度しか表示されません。必ず安全な場所に保存してください。

### 3. Privileged Gateway Intents を有効化

同じ「Bot」タブの下部にある **「Privileged Gateway Intents」** セクションで以下を **ON** にする:

- ✅ **SERVER MEMBERS INTENT**
- ✅ **MESSAGE CONTENT INTENT**

**「Save Changes」** をクリック。

> ⚠️ `MESSAGE CONTENT INTENT` を有効にしないと、Bot がメッセージの内容を読めません。

### 4. OAuth2 で招待 URL を生成

1. 左メニューの **「OAuth2」** → **「URL Generator」** をクリック
2. **Scopes** で以下を選択:
   - ✅ `bot`
   - ✅ `applications.commands`
3. **Bot Permissions** で以下を選択:
   - ✅ `Read Messages/View Channels`
   - ✅ `Send Messages`
   - ✅ `Read Message History`
   - ✅ `Add Reactions`
   - ✅ `Use Slash Commands`
4. ページ下部に生成された URL をコピー

### 5. Bot をサーバーに招待

生成した URL をブラウザで開き、Bot を招待したいサーバーを選択して **「認証」** をクリック。

### 6. チャンネル ID とユーザー ID を取得

Discord の設定で **開発者モード** を有効化します:

1. Discord アプリの設定 → **「詳細設定」**（または Advanced）
2. **「開発者モード」** を ON

その後:
- **チャンネル ID**: 対象チャンネルを右クリック → **「IDをコピー」**
- **ユーザー ID**: 自分のアイコンを右クリック → **「IDをコピー」**

---

## 環境変数の設定

`.env` ファイルに以下の変数を設定します。

```bash
# ── 必須 ────────────────────────────────────────────────
DISCORD_BOT_TOKEN=your-discord-bot-token-here
DISCORD_CHANNEL_INSTRUCTIONS=123456789012345678
DISCORD_INSTRUCTION_USER_ID=987654321098765432
COPILOT_PROJECT_ROOT=/home/user/my-project

# ── オプション ──────────────────────────────────────────
DISCORD_COPILOT_MODEL=gpt-5.4
DISCORD_COPILOT_SESSION_ID=

# ファイルパスの上書き（複数ブリッジ運用時に有用）
# BRIDGE_STATE_FILE=~/.copilot/bridge_state.json
# BRIDGE_LOCK_FILE=~/.copilot/bridge.lock
# BRIDGE_HEARTBEAT_FILE=~/.copilot/bridge_heartbeat.json

# Copilot に送るメッセージの先頭に付加するプレフィックス
# BRIDGE_PROMPT_PREFIX=以下の指示に従って /home/user/my-project で作業してください。
```

### 変数一覧

| 変数名 | 必須 | デフォルト | 説明 |
|--------|:----:|-----------|------|
| `DISCORD_BOT_TOKEN` | ✅ | — | Discord Bot のトークン。Developer Portal から取得 |
| `DISCORD_CHANNEL_INSTRUCTIONS` | ✅ | — | 監視するチャンネルの ID |
| `DISCORD_INSTRUCTION_USER_ID` | ✅ | — | 「指示」として扱うユーザーの Discord ID。このユーザー以外のメッセージは無視される |
| `COPILOT_PROJECT_ROOT` | ✅ | — | Copilot が作業するプロジェクトの絶対パス |
| `DISCORD_COPILOT_MODEL` | — | `gpt-5.4` | デフォルトで使用する AI モデル |
| `DISCORD_COPILOT_SESSION_ID` | — | （自動生成） | Copilot セッション ID を固定する。空欄の場合は起動ごとに新規セッション |
| `BRIDGE_PROMPT_PREFIX` | — | — | 各 Discord メッセージの前に自動挿入するテキスト（作業ディレクトリの指示など） |
| `BRIDGE_STATE_FILE` | — | `~/.copilot/discord_to_copilot_bridge_state.json` | 状態ファイルのパスを上書き |
| `BRIDGE_LOCK_FILE` | — | `~/.copilot/discord_to_copilot_bridge.lock` | ロックファイルのパスを上書き |
| `BRIDGE_HEARTBEAT_FILE` | — | `~/.copilot/discord_to_copilot_bridge.heartbeat.json` | ハートビートファイルのパスを上書き |

---

## 起動方法

### シンプルに起動する

```bash
python3 discord_to_copilot_bridge.py --watch
```

`--watch` を付けると継続監視モードになります。Discord のメッセージを常時待ち受けます。

### `run.sh` で起動する（推奨）

`run.sh` はラッパースクリプトで、以下を自動的に処理します:

- 既存ブリッジプロセスのハートビートを確認し、停止していれば引き継ぐ（takeover）
- クラッシュ時の自動再起動

```bash
bash run.sh
```

### CLI 引数の一覧

```
--watch                  継続監視モードで起動（Ctrl+C で停止）
--interval N             Discord API ポーリング間隔（秒）デフォルト: 10
--channel-id ID          監視チャンネル ID を上書き（.env の値より優先）
--user-id ID             指示ユーザー ID を上書き
--model NAME             使用モデルを上書き
--session-id ID          Copilot セッション ID を固定
--replay-message-id ID   特定のメッセージ ID を再実行
--once                   メッセージを1件だけ処理して終了
```

### 複数チャンネルを同時に監視する

異なるチャンネル・プロジェクトを並行して管理できます。

```bash
# チャンネル A（プロジェクト A）
DISCORD_CHANNEL_INSTRUCTIONS=111111111111111111 \
COPILOT_PROJECT_ROOT=/home/user/project-a \
DISCORD_COPILOT_SESSION_ID=session-a \
python3 discord_to_copilot_bridge.py --watch --channel-id 111111111111111111 &

# チャンネル B（プロジェクト B）
DISCORD_CHANNEL_INSTRUCTIONS=222222222222222222 \
COPILOT_PROJECT_ROOT=/home/user/project-b \
DISCORD_COPILOT_SESSION_ID=session-b \
python3 discord_to_copilot_bridge.py --watch --channel-id 222222222222222222 &
```

> ✅ セッション ID を固定（`DISCORD_COPILOT_SESSION_ID`）することで、ボットを再起動しても同じ Copilot セッション（会話履歴）が引き継がれます。

---

## systemd での常時起動

サーバーを再起動してもボットが自動的に起動するよう、systemd サービスとして登録します。

### 1. サービスファイルをコピー

```bash
cp deploy/copilot-bridge.service.example /etc/systemd/system/copilot-bridge.service
```

### 2. サービスファイルを編集

```bash
nano /etc/systemd/system/copilot-bridge.service
```

以下の箇所を自分の環境に合わせて変更してください:

```ini
[Unit]
Description=Dangerbot — Discord Copilot Bridge
After=network.target

[Service]
Type=simple
User=your-username                          # ← 実行ユーザーに変更
WorkingDirectory=/path/to/dangerbot-oss     # ← クローン先のパスに変更
EnvironmentFile=/path/to/dangerbot-oss/.env # ← .env のパスに変更
ExecStart=/usr/bin/bash run.sh
Restart=always
RestartSec=5
StandardOutput=append:/var/log/copilot-bridge.log
StandardError=append:/var/log/copilot-bridge-error.log

[Install]
WantedBy=multi-user.target
```

### 3. サービスを有効化して起動

```bash
# systemd に変更を反映
systemctl daemon-reload

# 自動起動を有効化して即時起動
systemctl enable --now copilot-bridge

# 状態を確認
systemctl status copilot-bridge --no-pager
```

### 4. ログを確認する

```bash
# リアルタイムでログを追う
journalctl -u copilot-bridge -f

# ログファイルを直接確認
tail -f /var/log/copilot-bridge.log
```

### サービスの操作コマンド

```bash
systemctl start copilot-bridge    # 起動
systemctl stop copilot-bridge     # 停止
systemctl restart copilot-bridge  # 再起動
systemctl status copilot-bridge   # 状態確認
```

---

## スラッシュコマンド

ブリッジが起動すると、Discord サーバーにスラッシュコマンドが自動的に登録されます（手動登録不要）。

### `/cancel` — 処理中のタスクをキャンセル

Copilot が長時間応答しない場合や、誤った指示を送ってしまった場合にキャンセルします。

```
/cancel
```

実行すると「⛔ タスクをキャンセルしました」のような返信が届きます。

---

### `/model` — モデルの確認・切り替え

引数なしで現在のモデルを確認、引数ありで切り替えます。

```
/model                    # 現在のモデルを表示
/model gpt-5.4            # GPT-5.4 に切り替え
/model claude-sonnet-4.6  # Claude Sonnet 4.6 に切り替え
```

**利用可能なモデル一覧:**

| モデル名 | 特徴 |
|----------|------|
| `gpt-5.4` | デフォルト。高精度・汎用（推奨） |
| `gpt-5.1` | バランス型 |
| `gpt-5-mini` | 高速・低コスト |
| `claude-sonnet-4.6` | Claude 最新 Sonnet |
| `claude-sonnet-4-5` | Claude Sonnet 4.5 |
| `claude-opus-4.6` | Claude 最高精度（低速） |
| `claude-opus-4-5` | Claude Opus 4.5 |
| `claude-haiku-4-5` | Claude 最速・軽量 |

---

## トラブルシューティング

### Bot がメッセージに反応しない

**確認事項:**

1. **正しいチャンネル ID が設定されているか**
   ```bash
   grep DISCORD_CHANNEL_INSTRUCTIONS .env
   ```
   Discord の開発者モードでチャンネルを右クリックしてIDを再確認してください。

2. **正しいユーザー ID が設定されているか**
   ```bash
   grep DISCORD_INSTRUCTION_USER_ID .env
   ```
   ボットは `DISCORD_INSTRUCTION_USER_ID` のユーザーのメッセージのみを処理します。

3. **MESSAGE CONTENT INTENT が有効か**
   Developer Portal の Bot タブで `MESSAGE CONTENT INTENT` が ON になっているか確認してください。

4. **ボットが起動しているか**
   ```bash
   systemctl status copilot-bridge
   # または
   ps aux | grep discord_to_copilot_bridge
   ```

---

### `DISCORD_BOT_TOKEN` エラー

```
ERROR: 401 Unauthorized — invalid token
```

**対処法:**
- Developer Portal でトークンを再生成し `.env` を更新する
- トークンに余分なスペースや改行が含まれていないか確認する
  ```bash
  cat -A .env | grep DISCORD_BOT_TOKEN  # $ で行末確認
  ```

---

### `ImportError: cannot import name 'CopilotClient'`

```
ImportError: cannot import name 'CopilotClient' from 'copilot'
# または
ModuleNotFoundError: No module named 'copilot'
```

**対処法:**
```bash
pip install github-copilot-sdk
# または
pip install -r requirements.txt
```

`github-copilot-sdk` パッケージがインストールされていないことが原因です。`requirements.txt` を使って依存パッケージを一括インストールしてください。

---

### `copilot` コマンドが見つからない

```
FileNotFoundError: [Errno 2] No such file or directory: 'copilot'
```

**対処法:**
```bash
# copilot が PATH に存在するか確認
which copilot

# 存在しない場合はインストール
gh extension install github/gh-copilot

# インストール後に確認
copilot --version
```

`github-copilot-sdk` は `CopilotClient.start()` 時に内部で `copilot` バイナリをサブプロセスとして自動起動します。ユーザーが直接 `copilot` を実行する必要はありませんが、**バイナリが PATH に存在していない**とこのエラーが発生します。

systemd サービスとして動かしている場合は、`PATH` が通っていないことがあります。  
サービスファイルに明示的に PATH を追記してください:

```ini
[Service]
Environment="PATH=/usr/local/bin:/usr/bin:/bin:/home/your-user/.local/bin"
```

---

### セッションエラー / 壊れたセッション

```
Error: session not found
# または
copilot がクラッシュして応答しない
```

**対処法:**

1. セッション ID をリセットする（`.env` の `DISCORD_COPILOT_SESSION_ID` を空にする）
   ```bash
   sed -i 's/^DISCORD_COPILOT_SESSION_ID=.*/DISCORD_COPILOT_SESSION_ID=/' .env
   ```

2. 状態ファイルを削除する
   ```bash
   rm -f ~/.copilot/discord_to_copilot_bridge_state.json
   rm -f ~/.copilot/discord_to_copilot_bridge.lock
   rm -f ~/.copilot/discord_to_copilot_bridge.heartbeat.json
   ```

3. ボットを再起動する
   ```bash
   systemctl restart copilot-bridge
   ```

---

### ハートビートが止まった / プロセスが応答しない

`run.sh` は定期的にハートビートファイルのタイムスタンプを確認し、古くなった場合にプロセスを引き継ぎます。  
それでも解決しない場合は手動で対処します:

```bash
# 古いプロセスを確認して停止
ps aux | grep discord_to_copilot_bridge
kill <PID>

# ロックファイルを削除
rm -f ~/.copilot/discord_to_copilot_bridge.lock

# 再起動
bash run.sh
```

---

### ログの確認方法

```bash
# systemd のログ（リアルタイム）
journalctl -u copilot-bridge -f

# ログファイルが設定されている場合
tail -100f /var/log/copilot-bridge.log

# エラーのみ絞り込む
journalctl -u copilot-bridge -p err --since "1 hour ago"

# Python のトレースバックを探す
journalctl -u copilot-bridge | grep -A 10 "Traceback"
```

---

## エージェントへの修復依頼方法

Dangerbot 自体の不具合やカスタマイズには、AI エージェント（Copilot CLI / ChatGPT / Claude など）に修復を依頼するのが効率的です。  
以下のガイドに従って、必要な情報を収集し、エージェントに渡してください。

### ステップ 1: 環境情報を収集する

以下のコマンドを実行し、出力をすべてコピーします:

```bash
echo "=== 環境情報 ===" && \
python3 --version && \
copilot --version 2>/dev/null || echo "copilot: not found" && \
gh auth status 2>&1 && \
pip show websockets requests python-dotenv 2>&1 | grep -E "^(Name|Version)" && \
echo "=== プロセス ===" && \
ps aux | grep discord_to_copilot | grep -v grep && \
echo "=== 最新ログ (50行) ===" && \
journalctl -u copilot-bridge -n 50 --no-pager 2>/dev/null || tail -50 /var/log/copilot-bridge.log 2>/dev/null
```

### ステップ 2: エラーメッセージを特定する

```bash
# エラーとトレースバックを抽出
journalctl -u copilot-bridge --since "1 hour ago" --no-pager | grep -E "(ERROR|Exception|Traceback|Error)" | tail -20
```

### ステップ 3: エージェントへの依頼文テンプレート

以下のテンプレートにログと環境情報を貼り付けてエージェントに送ります:

---

```
以下の問題が発生しています。原因を特定して修正してください。

【問題の概要】
（例: Bot がメッセージに反応しない / ハートビートが止まる / セッションエラーが頻発する）

【環境情報】
（上記コマンドの出力を貼り付け）

【エラーログ】
（エラーメッセージを貼り付け）

【再現手順】
1. （何をしたか）
2. （何が起きたか）

【期待する動作】
（本来どうなってほしいか）

関連ファイル:
- discord_to_copilot_bridge.py
- run.sh
- .env（トークン等の機密情報は除く）
```

---

### よくある依頼例

**接続エラーの修正:**
```
discord_to_copilot_bridge.py で以下のエラーが発生しています。
WebSocket の再接続ロジックに問題がある可能性があります。修正してください。

エラー:
websockets.exceptions.ConnectionClosedError: received 1006 (connection closed abnormally)
```

**パフォーマンス改善:**
```
Copilot の応答が返ってきた後、Discord への返信が遅延することがあります。
discord_to_copilot_bridge.py の応答送信部分を確認して、遅延の原因を特定してください。
```

**機能追加:**
```
discord_to_copilot_bridge.py に以下の機能を追加してください:
- /status スラッシュコマンド: 現在のセッション ID・モデル名・処理中かどうかを返す
```

### GitHub Issues の使い方

バグ報告や機能要望は GitHub Issues で管理しています:

1. [Issues](../../issues) ページを開く
2. **「New Issue」** をクリック
3. テンプレートに沿って以下を記載:
   - タイトル: 短く具体的に（例: `WebSocket が切断後に再接続しない`）
   - 再現手順
   - 期待する動作
   - 実際の動作
   - 環境情報（上記コマンドの出力）

---

## 開発・カスタマイズ

### プロンプトプレフィックスのカスタマイズ

`BRIDGE_PROMPT_PREFIX` を設定すると、Discord から受け取ったメッセージの先頭に自動的にテキストが挿入されます。  
Copilot に作業ディレクトリや制約を伝えるのに便利です:

```bash
# .env の例
BRIDGE_PROMPT_PREFIX=あなたは /home/user/my-project で作業するエンジニアです。\
指示には必ず日本語で返答し、コードは英語で書いてください。
```

### 応答メッセージのカスタマイズ

`discord_to_copilot_bridge.py` 内の文字列（「⏳ 処理中」「✅ 完了」など）を編集することで、  
ボットのメッセージを自由にカスタマイズできます。

```bash
grep -n "処理中\|✅\|👀\|⛔" discord_to_copilot_bridge.py
```

### 新機能の追加

スラッシュコマンドを追加するには:

1. `discord_to_copilot_bridge.py` 内の `register_slash_commands()` 関数にコマンド定義を追加
2. `handle_interaction()` 関数にコマンド処理ロジックを追加
3. ボットを再起動（コマンドの登録は起動時に自動実行）

---

## ライセンス

MIT License

Copyright (c) 2024 Dangerbot Contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
