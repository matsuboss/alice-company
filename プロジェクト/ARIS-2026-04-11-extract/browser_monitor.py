"""
ARIS PROJECT — browser_monitor.py
ブラウザのタブ・URL監視 (backend/browser_monitor.py のコピー)
"""
import platform
import subprocess
import json

SYSTEM = platform.system()

WASTE_DOMAINS = [
    "twitter.com", "x.com", "youtube.com", "instagram.com",
    "tiktok.com", "netflix.com", "reddit.com", "facebook.com",
    "twitch.tv", "discord.com", "spotify.com", "hulu.com",
    "pixiv.net", "nicovideo.jp",
]


def get_browser_tabs_applescript() -> list:
    script = '''
    set tabList to {}
    tell application "System Events"
        set runningApps to name of every process
    end tell

    if "Google Chrome" is in runningApps then
        tell application "Google Chrome"
            repeat with w in windows
                set winIndex to 0
                repeat with t in tabs of w
                    set winIndex to winIndex + 1
                    set isActive to (winIndex = active tab index of w)
                    set end of tabList to (title of t) & "|||" & (URL of t) & "|||" & (isActive as string)
                end repeat
            end repeat
        end tell
    end if

    if "Safari" is in runningApps then
        tell application "Safari"
            repeat with w in windows
                repeat with t in tabs of w
                    set end of tabList to (name of t) & "|||" & (URL of t) & "|||false"
                end repeat
            end repeat
        end tell
    end if

    return tabList
    '''
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=5)
    if result.returncode != 0:
        return []
    tabs = []
    raw = result.stdout.strip()
    if not raw:
        return []
    for item in raw.split(", "):
        parts = item.strip().split("|||")
        if len(parts) >= 2:
            title  = parts[0].strip()
            url    = parts[1].strip()
            active = parts[2].strip().lower() == "true" if len(parts) > 2 else False
            browser = "Chrome" if "google.com" in url or "chrome" in url.lower() else "Safari"
            tabs.append({"browser": browser, "title": title, "url": url, "active": active})
    return tabs


def get_browser_tabs_powershell() -> list:
    try:
        script = r"""
        $tabs = @()
        $chrome = Get-Process -Name "chrome" -ErrorAction SilentlyContinue
        if ($chrome) { $tabs += @{browser="Chrome"; title="Chrome"; url=""; active=$true} }
        $edge = Get-Process -Name "msedge" -ErrorAction SilentlyContinue
        if ($edge) { $tabs += @{browser="Edge"; title="Edge"; url=""; active=$true} }
        $tabs | ConvertTo-Json
        """
        result = subprocess.run(["powershell", "-Command", script], capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout)
            return [data] if isinstance(data, dict) else data
    except Exception:
        pass
    return []


def get_browser_tabs() -> list:
    try:
        if SYSTEM == "Darwin":
            return get_browser_tabs_applescript()
        elif SYSTEM == "Windows":
            return get_browser_tabs_powershell()
    except Exception as e:
        print(f"[browser] タブ取得エラー: {e}")
    return []
