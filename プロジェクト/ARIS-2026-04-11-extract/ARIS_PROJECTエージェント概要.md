# ARIS PROJECT — クライアントエージェント

**製品名: ARIS PROJECT**（クライアントPC（Mac/Windows）で動作するPC監視エージェント）。
5秒ごとにスナップショットを取得し、60秒分をまとめてサーバーに送信します。

## セットアップ

### 1. 依存インストール

```bash
pip install -r requirements.txt
```

macOS の場合、AppKit のインストールが必要です:
```bash
pip install pyobjc-framework-Cocoa
```

### 2. config.json を編集

```json
{
    "server_url": "http://160.251.174.90",
    "username": "あなたのメールアドレス",
    "password": "あなたのパスワード",
    "upload_interval": 60,
    "snapshot_interval": 5
}
```

### 3. 起動

```bash
python agent_main.py
```

## 動作概要

```
agent_main.py
  ├── monitor.py          キーボード/マウス/アクティブアプリを5秒ごとに記録
  ├── browser_monitor.py  Chrome/Safariのタブ情報を取得
  └── uploader.py         12件（60秒分）溜まったらサーバーのAPIへ一括送信
                          POST /api/agent/upload-batch
```

## ファイル構成

| ファイル | 役割 |
|----------|------|
| `agent_main.py` | メインループ |
| `monitor.py` | PC操作監視（pynput + psutil） |
| `browser_monitor.py` | ブラウザタブ監視 |
| `uploader.py` | サーバーへのデータ送信 |
| `config.json` | サーバーURL・認証情報 |
| `requirements.txt` | 依存パッケージ |

*最終更新: 2026-04-12（本ファイルを `README.md` から移行）*
