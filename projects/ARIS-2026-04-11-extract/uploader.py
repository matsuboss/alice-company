"""
ARIS PROJECT — uploader.py
スナップショットをサーバーにバッチ送信する
"""
import json
import os

import httpx


class AgentUploader:
    BATCH_SIZE = 12  # 5秒 × 12 = 60秒分

    def __init__(self, config_path=None):
        if config_path is None:
            config_path = os.path.join(os.path.dirname(__file__), "config.json")
        with open(config_path) as f:
            self.config = json.load(f)
        self.server_url = self.config["server_url"].rstrip("/")
        self.token      = None
        self.buffer     = []

    def login(self) -> bool:
        """サーバーにログインしてJWTトークンを取得"""
        try:
            resp = httpx.post(
                f"{self.server_url}/api/auth/login",
                json={"email": self.config["username"], "password": self.config["password"]},
                timeout=10,
            )
            if resp.status_code == 200:
                self.token = resp.json().get("access_token", "")
                return bool(self.token)
        except Exception as e:
            print(f"[uploader] ログイン失敗: {e}")
        return False

    def _post_batch(self, snapshots: list) -> bool:
        """スナップショットのリストをサーバーに送信"""
        try:
            resp = httpx.post(
                f"{self.server_url}/api/agent/upload-batch",
                json={"snapshots": snapshots},
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=10,
            )
            if resp.status_code == 200:
                return resp.json().get("result") == "ok"
            if resp.status_code == 401:
                return False  # 呼び出し元でトークン更新
        except Exception as e:
            print(f"[uploader] 送信失敗: {e}")
        return False

    def add_snapshot(self, snapshot: dict):
        """スナップショットをバッファに追加し、BATCH_SIZE 到達時に送信"""
        self.buffer.append(snapshot)
        if len(self.buffer) >= self.BATCH_SIZE:
            self.flush()

    def flush(self):
        """バッファに残っているデータを強制送信"""
        if not self.buffer:
            return
        if not self.token and not self.login():
            print("[uploader] 送信スキップ（未認証）")
            return
        to_send = list(self.buffer)
        if self._post_batch(to_send):
            self.buffer.clear()
            print(f"[uploader] {len(to_send)}件 送信完了")
        else:
            # 401 → トークン更新して再試行
            self.token = None
            if self.login() and self._post_batch(to_send):
                self.buffer.clear()
                print(f"[uploader] {len(to_send)}件 送信完了（再認証後）")
            else:
                print(f"[uploader] 送信失敗。バッファ保持: {len(to_send)}件")
