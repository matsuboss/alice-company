"""
ARIS PROJECT — monitor.py
PC操作のリアルタイム監視: キーボード/マウスカウント、アクティブウィンドウ、プロセスリスト
ミリ秒精度の行動データ（キーストローク間隔・マウス速度・方向転換）も収集し認知状態推定に使う。
"""
import datetime
import math
import platform
import statistics
import subprocess
import threading
import time

import psutil

_OS = platform.system()  # "Windows" / "Darwin" / "Linux"

# ── pynput リスナー ───────────────────────────────────────────────
try:
    from pynput import keyboard as _keyboard, mouse as _mouse
    _PYNPUT_AVAILABLE = True
except Exception:
    _PYNPUT_AVAILABLE = False


DEFAULT_EXCLUDED_APPS = [
    "1password", "bitwarden", "keepass", "lastpass", "dashlane",
    "keychain access", "パスワード",
]

# ミリ秒精度データの履歴保持上限
_MAX_KEYSTROKE_HISTORY = 100
_MAX_MOUSE_HISTORY     = 50
_MAX_VELOCITY_HISTORY  = 50
# マウス停止判定（ms以上動きがない場合をホバーとみなす）
_HOVER_THRESHOLD_MS    = 500


class PCMonitor:
    """PCの操作状態を監視し、スナップショットを提供する"""

    def __init__(self, excluded_apps=None):
        # 基本カウンター
        self._keyboard_count = 0
        self._mouse_clicks   = 0
        self._lock           = threading.Lock()
        self._kb_listener    = None
        self._ms_listener    = None
        user_excluded = [a.lower() for a in (excluded_apps or [])]
        self._excluded_apps  = list(set(DEFAULT_EXCLUDED_APPS + user_excluded))

        # ミリ秒精度データ
        self._keystroke_times:     list = []   # キー押下タイムスタンプ(ms)
        self._keystroke_intervals: list = []   # キー間隔(ms)
        self._delete_count:        int  = 0    # 削除キー回数
        self._mouse_positions:     list = []   # [(x, y, t_ms), ...]
        self._mouse_velocities:    list = []   # px/sec
        self._direction_changes:   int  = 0    # 方向転換回数
        self._hover_count:         int  = 0    # 停止回数
        self._last_move_time:      float = 0.0 # 最後のマウス移動時刻(ms)
        self._pause_detected:      bool  = False  # ホバー重複計上防止

        self._start_listeners()

    def _start_listeners(self):
        if not _PYNPUT_AVAILABLE:
            return
        try:
            self._kb_listener = _keyboard.Listener(on_press=self._on_key_press)
            self._ms_listener = _mouse.Listener(
                on_click=self._on_click,
                on_move=self._on_move,
            )
            self._kb_listener.daemon = True
            self._ms_listener.daemon = True
            self._kb_listener.start()
            self._ms_listener.start()
        except Exception as e:
            print(f"[monitor] リスナー起動失敗: {e}")

        # psutil warmup（初回は0を返すため）
        try:
            for p in psutil.process_iter(["cpu_percent"]):
                try:
                    p.cpu_percent()
                except Exception:
                    pass
        except Exception:
            pass

    def _on_key_press(self, key):
        now_ms = time.time() * 1000
        with self._lock:
            self._keyboard_count += 1

            # 削除キー検出
            try:
                if key in (_keyboard.Key.backspace, _keyboard.Key.delete):
                    self._delete_count += 1
            except Exception:
                pass

            # キーストローク間隔記録（3秒以内のみ）
            if self._keystroke_times:
                interval = now_ms - self._keystroke_times[-1]
                if 0 < interval < 3000:
                    self._keystroke_intervals.append(round(interval, 1))
                    if len(self._keystroke_intervals) > _MAX_KEYSTROKE_HISTORY:
                        self._keystroke_intervals.pop(0)

            self._keystroke_times.append(now_ms)
            if len(self._keystroke_times) > _MAX_KEYSTROKE_HISTORY:
                self._keystroke_times.pop(0)

    def _on_click(self, x, y, button, pressed):
        if pressed:
            with self._lock:
                self._mouse_clicks += 1

    def _on_move(self, x, y):
        now_ms = time.time() * 1000
        with self._lock:
            # ホバー検出（前回移動から _HOVER_THRESHOLD_MS 以上経過していた場合）
            if self._last_move_time > 0:
                gap = now_ms - self._last_move_time
                if gap >= _HOVER_THRESHOLD_MS and not self._pause_detected:
                    self._hover_count += 1
                    self._pause_detected = True
                elif gap < _HOVER_THRESHOLD_MS:
                    self._pause_detected = False
            self._last_move_time = now_ms

            self._mouse_positions.append((x, y, now_ms))

            if len(self._mouse_positions) >= 2:
                p1 = self._mouse_positions[-2]
                p2 = self._mouse_positions[-1]
                dist = math.sqrt((p2[0] - p1[0]) ** 2 + (p2[1] - p1[1]) ** 2)
                dt   = max(p2[2] - p1[2], 1)
                velocity = dist / dt * 1000  # px/sec
                self._mouse_velocities.append(round(velocity, 1))
                if len(self._mouse_velocities) > _MAX_VELOCITY_HISTORY:
                    self._mouse_velocities.pop(0)

                # 方向転換検出（内積が負 → 逆方向）
                if len(self._mouse_positions) >= 3:
                    p0 = self._mouse_positions[-3]
                    v1 = (p1[0] - p0[0], p1[1] - p0[1])
                    v2 = (p2[0] - p1[0], p2[1] - p1[1])
                    dot = v1[0] * v2[0] + v1[1] * v2[1]
                    if dot < 0:
                        self._direction_changes += 1

            if len(self._mouse_positions) > _MAX_MOUSE_HISTORY:
                self._mouse_positions.pop(0)

    def get_active_window(self) -> tuple:
        """アクティブウィンドウの (app_name, window_title) を返す"""
        try:
            if _OS == "Windows":
                try:
                    import ctypes
                    hwnd = ctypes.windll.user32.GetForegroundWindow()
                    pid = ctypes.c_ulong()
                    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    name = psutil.Process(pid.value).name()
                    buf = ctypes.create_unicode_buffer(512)
                    ctypes.windll.user32.GetWindowTextW(hwnd, buf, 512)
                    return name, buf.value
                except Exception:
                    return "Unknown", "Unknown"

            elif _OS == "Darwin":
                try:
                    from AppKit import NSWorkspace
                    app_info = NSWorkspace.sharedWorkspace().activeApplication()
                    app_name = app_info.get("NSApplicationName", "Unknown")
                    result = subprocess.run(
                        ["osascript", "-e",
                         'tell application "System Events" to get name of first window '
                         'of (first process whose frontmost is true)'],
                        capture_output=True, text=True, timeout=1
                    )
                    title = result.stdout.strip() if result.returncode == 0 else app_name
                    return app_name, title
                except Exception:
                    try:
                        from AppKit import NSWorkspace
                        app_info = NSWorkspace.sharedWorkspace().activeApplication()
                        app_name = app_info.get("NSApplicationName", "Unknown")
                        return app_name, app_name
                    except Exception:
                        return "Unknown", "Unknown"

            elif _OS == "Linux":
                try:
                    win_id = subprocess.run(
                        ["xdotool", "getactivewindow"],
                        capture_output=True, text=True, timeout=1
                    ).stdout.strip()
                    title = subprocess.run(
                        ["xdotool", "getwindowname", win_id],
                        capture_output=True, text=True, timeout=1
                    ).stdout.strip()
                    pid_str = subprocess.run(
                        ["xdotool", "getwindowpid", win_id],
                        capture_output=True, text=True, timeout=1
                    ).stdout.strip()
                    name = psutil.Process(int(pid_str)).name() if pid_str else "Unknown"
                    return name, title
                except Exception:
                    return "Unknown", "Unknown"

        except Exception:
            pass
        return "Unknown", "Unknown"

    def get_top_processes(self, limit: int = 10) -> list:
        """CPU使用率上位プロセス一覧を返す"""
        try:
            procs = []
            for p in psutil.process_iter(["name", "cpu_percent"]):
                try:
                    procs.append({"name": p.info["name"], "cpu": p.info["cpu_percent"] or 0.0})
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            procs.sort(key=lambda x: x["cpu"], reverse=True)
            return procs[:limit]
        except Exception:
            return []

    def _is_excluded(self, app_name: str) -> bool:
        lower = app_name.lower()
        return any(ex in lower or lower in ex for ex in self._excluded_apps)

    def take_snapshot(self) -> dict:
        """現在のスナップショットを取得し、カウンターをリセット"""
        with self._lock:
            kb = self._keyboard_count
            mc = self._mouse_clicks
            self._keyboard_count = 0
            self._mouse_clicks   = 0

            # ミリ秒精度データをコピーしてリセット
            intervals        = list(self._keystroke_intervals[-20:])
            velocities       = list(self._mouse_velocities[-20:])
            delete_count     = self._delete_count
            direction_chg    = self._direction_changes
            hover_cnt        = self._hover_count

            self._keystroke_intervals.clear()
            self._mouse_velocities.clear()
            self._delete_count       = 0
            self._direction_changes  = 0
            self._hover_count        = 0

        app_name, window_title = self.get_active_window()

        # 除外アプリのウィンドウタイトルを匿名化
        if self._is_excluded(app_name):
            app_name     = "除外済みアプリ"
            window_title = ""

        processes = self.get_top_processes()

        # キーストロークデータ集計
        avg_interval = round(sum(intervals) / len(intervals)) if intervals else 0
        std_interval = round(statistics.stdev(intervals)) if len(intervals) > 1 else 0
        delete_ratio = round(delete_count / max(kb, 1) * 100, 1)

        # マウスデータ集計
        avg_velocity = round(sum(velocities) / len(velocities)) if velocities else 0

        return {
            "timestamp":      datetime.datetime.now().isoformat(),
            "active_app":     app_name,
            "window_title":   window_title,
            "process_list":   processes,
            "keyboard_count": kb,
            "mouse_clicks":   mc,
            # ── ミリ秒精度データ ────────────────────────────
            "keystroke_data": {
                "intervals":        intervals,
                "avg_interval_ms":  avg_interval,
                "std_interval_ms":  std_interval,
                "delete_ratio":     delete_ratio,
            },
            "mouse_data": {
                "avg_velocity":     avg_velocity,
                "direction_changes": direction_chg,
                "hover_count":       hover_cnt,
            },
        }
