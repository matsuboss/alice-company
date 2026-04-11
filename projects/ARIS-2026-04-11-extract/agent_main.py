"""
ARIS PROJECT — agent_main.py
クライアントPC用エージェントのメインループ

起動方法:
    python agent_main.py

config.json に server_url / username / password を設定してから起動してください。
"""
import time

from browser_monitor import get_browser_tabs
from monitor import PCMonitor
from uploader import AgentUploader
from script_executor import ScriptExecutor


def main():
    print("ARIS PROJECT — agent starting...")

    uploader = AgentUploader()
    if not uploader.login():
        print("ログイン失敗。config.json の username / password を確認してください。")
        return

    print(f"Connected to {uploader.server_url}")

    # スクリプト自動実行エンジンを起動
    check_interval = uploader.config.get("script_check_interval", 60)
    executor = ScriptExecutor(
        server_url=uploader.server_url,
        token_getter=lambda: uploader.token,  # 常に最新トークンを参照
        check_interval=check_interval,
    )
    executor.start()

    excluded_apps = uploader.config.get("excluded_apps", [])
    monitor = PCMonitor(excluded_apps=excluded_apps)
    snapshot_interval = uploader.config.get("snapshot_interval", 5)

    try:
        while True:
            try:
                snapshot = monitor.take_snapshot()

                # ブラウザタブ情報を追加
                try:
                    tabs = get_browser_tabs()
                    if tabs:
                        snapshot["browser_tabs"] = tabs
                except Exception:
                    pass

                # バッファに追加（BATCH_SIZE 件溜まったら自動送信）
                uploader.add_snapshot(snapshot)

                time.sleep(snapshot_interval)

            except Exception as e:
                print(f"[agent] エラー: {e}")
                time.sleep(snapshot_interval)

    except KeyboardInterrupt:
        print("\nAgent stopped.")
        executor.stop()
        uploader.flush()  # 残りを送信


if __name__ == "__main__":
    main()
