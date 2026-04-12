"""
ARIS PROJECT — script_executor.py
サーバーから実行待ちスクリプトを取得し、安全に実行する。
"""

import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from typing import Optional

logger = logging.getLogger("aris_project.script_executor")

# セキュリティ: 実行を禁止するパターン
DANGEROUS_PATTERNS = [
    "os.remove", "os.unlink", "shutil.rmtree",
    "subprocess.call", "os.system",
    "eval(", "exec(",
    "import ctypes",
    "__import__",
]


def is_safe_script(content: str):
    """スクリプトが安全かチェック"""
    for pattern in DANGEROUS_PATTERNS:
        if pattern in content:
            return False, f"危険なパターンを検出: {pattern}"
    return True, ""


class ScriptExecutor:
    """AIが生成したスクリプトを安全に実行するクラス"""

    def __init__(self, server_url: str, token: str = "", token_getter=None, check_interval: int = 60):
        self.server_url     = server_url.rstrip("/")
        self._token_static  = token
        self._token_getter  = token_getter  # callable で常に最新トークンを取得
        self.check_interval = check_interval
        self.running_processes: dict = {}  # script_id -> Popen
        self.scripts_dir = os.path.join(tempfile.gettempdir(), "aris_project_scripts")
        os.makedirs(self.scripts_dir, exist_ok=True)
        self._running = False

    def start(self):
        """バックグラウンドで定期チェックを開始"""
        self._running = True
        t = threading.Thread(target=self._check_loop, daemon=True)
        t.start()
        logger.info(f"ScriptExecutor started. interval={self.check_interval}s")
        print(f"[ScriptExecutor] started — 自動化スクリプトの自動実行が有効です (interval={self.check_interval}s)")

    def stop(self):
        """停止し、実行中のプロセスを全終了"""
        self._running = False
        for script_id, proc in list(self.running_processes.items()):
            try:
                proc.terminate()
                logger.info(f"Stopped script {script_id}")
            except Exception:
                pass
        self.running_processes.clear()

    # ── 内部ループ ──────────────────────────────────────────────

    def _check_loop(self):
        while self._running:
            try:
                self._check_pending_scripts()
                self._check_stop_signals()
            except Exception as e:
                logger.error(f"ScriptExecutor loop error: {e}")
            time.sleep(self.check_interval)

    def _request(self, url: str, data: bytes = None) -> Optional[dict]:
        token = self._token_getter() if self._token_getter else self._token_static
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            logger.debug(f"Request failed ({url}): {e}")
            return None

    def _check_pending_scripts(self):
        """実行待ちスクリプトを取得して実行"""
        data = self._request(f"{self.server_url}/api/scripts/pending",
                             data=b"{}")  # POST
        if not data:
            return
        for script in data.get("scripts", []):
            sid = script["id"]
            if sid in self.running_processes:
                continue
            logger.info(f"New pending script: {script['filename']}")
            self._execute_script(script)

    def _execute_script(self, script: dict):
        sid      = script["id"]
        filename = script["filename"]
        content  = script["script_content"]

        # セキュリティチェック
        ok, reason = is_safe_script(content)
        if not ok:
            logger.warning(f"Security check failed for {filename}: {reason}")
            self._report(sid, "error", error=f"セキュリティチェック失敗: {reason}")
            return

        # ファイルに保存
        script_path = os.path.join(self.scripts_dir, filename)
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(content)

        self._report(sid, "running", output="実行開始")

        try:
            proc = subprocess.Popen(
                [sys.executable, script_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self.scripts_dir,
                text=True,
            )
            self.running_processes[sid] = proc
            t = threading.Thread(target=self._monitor, args=(sid, proc), daemon=True)
            t.start()
        except Exception as e:
            logger.error(f"Failed to start {filename}: {e}")
            self._report(sid, "error", error=str(e))

    def _monitor(self, sid: int, proc: subprocess.Popen):
        try:
            stdout, stderr = proc.communicate(timeout=3600)
            if proc.returncode == 0:
                self._report(sid, "completed", output=(stdout or "")[-2000:])
                logger.info(f"Script {sid} completed")
            else:
                self._report(sid, "error",
                             output=(stdout or "")[-1000:],
                             error=(stderr or "")[-1000:])
                logger.warning(f"Script {sid} exited with code {proc.returncode}")
        except subprocess.TimeoutExpired:
            proc.kill()
            self._report(sid, "error", error="タイムアウト（1時間）で強制停止しました")
            logger.warning(f"Script {sid} timed out")
        except Exception as e:
            self._report(sid, "error", error=str(e))
        finally:
            self.running_processes.pop(sid, None)

    def _check_stop_signals(self):
        """サーバーからの停止指示を確認"""
        data = self._request(f"{self.server_url}/api/scripts/status")
        if not data:
            return
        for s in data.get("scripts", []):
            if s["status"] == "stopped" and s["id"] in self.running_processes:
                proc = self.running_processes.pop(s["id"])
                try:
                    proc.terminate()
                    logger.info(f"Stopped script {s['id']} by server signal")
                except Exception:
                    pass

    def _report(self, sid: int, status: str, output: str = "", error: str = ""):
        payload = json.dumps({"status": status, "output": output, "error": error}).encode()
        self._request(f"{self.server_url}/api/scripts/{sid}/report", data=payload)
