"""
A.R.I.S. Playwright ローカルエージェント

Mac / Windows のローカルマシンで動作し、
サーバーから承認済みタスクを受け取ってブラウザを操作する。

使い方:
  cd ~/aris-project/agent
  pip install playwright httpx python-dotenv
  playwright install chromium
  python playwright_agent.py

環境変数（.env または config.json で設定）:
  ARIS_SERVER_URL   ... サーバーURL (デフォルト: https://aris-ai.net)
  ARIS_EMAIL        ... ログインメールアドレス
  ARIS_PASSWORD     ... パスワード
  ARIS_HEADLESS     ... ブラウザをヘッドレスで起動するか (default: false)
  ARIS_POLL_INTERVAL ... ポーリング間隔秒数 (default: 5)
"""

import asyncio
import base64
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional, List, Dict, Any

import httpx

# ── 設定読み込み ──────────────────────────────────────────────────

def _load_config() -> dict:
    """config.json → 環境変数の優先順でロード。
    anthropic_api_key は config.json に値があればそちらを優先する
    （ANTHROPIC_API_KEY 環境変数が別キーで設定されているケースを防ぐため）。
    """
    cfg: dict = {}
    config_path = Path(__file__).parent / "config.json"
    _file_api_key = ""
    if config_path.exists():
        with open(config_path, encoding="utf-8") as f:
            raw = json.load(f)
        cfg["server_url"]  = raw.get("server_url", "https://aris-ai.net")
        cfg["email"]       = raw.get("username", "")
        cfg["password"]    = raw.get("password", "")
        _file_api_key      = raw.get("anthropic_api_key", "").strip()

    # 環境変数で上書き（ARIS_* 系は常に環境変数優先）
    cfg["server_url"]    = os.environ.get("ARIS_SERVER_URL",    cfg.get("server_url", "https://aris-ai.net")).rstrip("/")
    cfg["email"]         = os.environ.get("ARIS_EMAIL",         cfg.get("email", ""))
    cfg["password"]      = os.environ.get("ARIS_PASSWORD",      cfg.get("password", ""))
    cfg["headless"]      = os.environ.get("ARIS_HEADLESS",      "false").lower() == "true"
    cfg["poll_interval"] = int(os.environ.get("ARIS_POLL_INTERVAL", "5"))

    # anthropic_api_key: config.json に値があれば優先、なければ環境変数を使用
    _env_api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if _file_api_key:
        cfg["anthropic_api_key"] = _file_api_key
        if _env_api_key and _env_api_key != _file_api_key:
            # 起動時の診断ログ用に記録（後で警告表示）
            cfg["_env_key_conflict"] = True
    else:
        cfg["anthropic_api_key"] = _env_api_key

    return cfg


CFG = _load_config()
SERVER_URL    = CFG["server_url"]
HEADLESS      = CFG["headless"]
POLL_INTERVAL = CFG["poll_interval"]

logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s [%(levelname)s] %(message)s",
    datefmt= "%H:%M:%S",
)
logger = logging.getLogger("playwright_agent")


# ── 認証 ─────────────────────────────────────────────────────────

async def get_token(client: httpx.AsyncClient) -> str:
    r = await client.post(
        f"{SERVER_URL}/api/auth/login",
        json={"email": CFG["email"], "password": CFG["password"]},
        timeout=10,
    )
    r.raise_for_status()
    token = r.json().get("access_token", "")
    if not token:
        raise RuntimeError("ログイン失敗: access_token が取得できませんでした")
    logger.info("認証成功: %s", CFG["email"])
    return token


# ── タスク取得 ────────────────────────────────────────────────────

async def fetch_pending_tasks(client: httpx.AsyncClient, token: str) -> List[Dict[str, Any]]:
    r = await client.get(
        f"{SERVER_URL}/api/playwright/tasks/pending",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    if r.status_code == 401:
        raise PermissionError("トークン期限切れ")
    r.raise_for_status()
    return r.json().get("tasks", [])


# ── 結果報告 ─────────────────────────────────────────────────────

async def report_result(
    client: httpx.AsyncClient,
    token: str,
    task_id: str,
    success: bool,
    result: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
    screenshot_b64: Optional[str] = None,
) -> None:
    body: dict = {"success": success}
    if result:
        body["result"] = result
    if error:
        body["error"] = error
    if screenshot_b64:
        body["screenshot_after"] = screenshot_b64

    await client.post(
        f"{SERVER_URL}/api/playwright/tasks/{task_id}/result",
        headers={"Authorization": f"Bearer {token}"},
        json=body,
        timeout=15,
    )


# ── ステップ実行 ──────────────────────────────────────────────────

async def execute_step(page, step: dict) -> None:
    """1ステップを Playwright で実行する"""
    action = step.get("action") or step.get("type", "")

    if action in ("navigate", "goto"):
        url = step.get("url", "")
        logger.info("  → navigate: %s", url)
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)

    elif action == "click":
        selector = step.get("selector") or step.get("css", "")
        logger.info("  → click: %s", selector)
        await page.click(selector, timeout=8000)

    elif action in ("type", "fill"):
        selector = step.get("selector") or step.get("css", "")
        text     = step.get("text", "")
        logger.info("  → type: %s → %s", selector, text[:30])
        await page.fill(selector, text, timeout=8000)

    elif action == "press":
        key = step.get("key", "Enter")
        logger.info("  → press: %s", key)
        await page.keyboard.press(key)

    elif action == "select":
        selector = step.get("selector") or step.get("css", "")
        value    = step.get("value", "")
        logger.info("  → select: %s = %s", selector, value)
        await page.select_option(selector, value=value, timeout=8000)

    elif action == "scroll":
        amount = step.get("amount", 300)
        if step.get("direction") == "up":
            amount = -amount
        logger.info("  → scroll: %d", amount)
        await page.mouse.wheel(0, amount)

    elif action in ("wait", "sleep"):
        ms = step.get("ms") or int(step.get("seconds", 1) * 1000)
        logger.info("  → wait: %dms", ms)
        await asyncio.sleep(ms / 1000)

    elif action == "screenshot":
        pass   # スクリーンショットは最後にまとめて撮る

    else:
        logger.warning("  ⚠ 不明なアクション: %s (スキップ)", action)


# ── スマート実行エンジン（Phase1: DOM解析+Claude計画, Phase2: CSS実行, Phase3: Vision回復）──

import re as _re

_CLAUDE_MODEL    = "claude-sonnet-4-20250514"
_ACTION_TIMEOUT  = 8000


def _extract_json(text: str):
    """レスポンスからJSONを抽出（コードブロック・前後テキスト対応）"""
    text = text.strip()
    m = _re.search(r"```(?:json)?\s*([\s\S]+?)```", text)
    if m:
        text = m.group(1).strip()
    for sc, ec in [('{', '}'), ('[', ']')]:
        s, e = text.find(sc), text.rfind(ec)
        if s != -1 and e != -1 and e > s:
            try:
                return json.loads(text[s:e + 1])
            except json.JSONDecodeError:
                pass
    return None


async def _get_page_context(page) -> dict:
    """ページのDOM構造をJSで解析してClaudeに渡す形式で返す"""
    try:
        ctx = await page.evaluate("""() => {
            const els = [];
            const selectors = [
                'button', 'a', 'input', 'select', 'textarea',
                '[role="button"]', '[role="link"]', '[role="tab"]',
                'form', 'h1', 'h2', 'h3', '[data-testid]', '[aria-label]',
            ];
            selectors.forEach(sel => {
                document.querySelectorAll(sel).forEach(el => {
                    const rect = el.getBoundingClientRect();
                    if (rect.width === 0 && rect.height === 0) return;
                    const css = (() => {
                        if (el.id) return '#' + CSS.escape(el.id);
                        if (el.name) return el.tagName.toLowerCase() + '[name="' + el.name + '"]';
                        const cls = Array.from(el.classList).slice(0,2).map(c => '.' + CSS.escape(c)).join('');
                        return el.tagName.toLowerCase() + cls;
                    })();
                    els.push({
                        tag:  el.tagName.toLowerCase(),
                        css,
                        text: (el.innerText || el.value || el.placeholder || el.getAttribute('aria-label') || '').slice(0, 80),
                        type: el.type || null,
                        href: el.href  || null,
                    });
                });
            });
            const seen = new Set();
            return els.filter(e => { if (seen.has(e.css)) return false; seen.add(e.css); return true; }).slice(0, 120);
        }""")
        return {"url": page.url, "title": await page.title(), "elements": ctx}
    except Exception as exc:
        logger.warning("get_page_context failed: %s", exc)
        return {"url": page.url, "title": "", "elements": []}


async def _generate_plan(
    page_ctx: dict,
    task_description: str,
    api_key: str,
) -> Optional[List[Dict[str, Any]]]:
    """DOMコンテキストをClaudeに送り全ステップの計画をJSONリストで取得"""
    elements_text = "\n".join(
        f"  [{e['tag']}] css={e['css']!r}  text={e['text']!r}"
        + (f"  type={e['type']!r}" if e.get("type") else "")
        + (f"  href={e['href']!r}"  if e.get("href")  else "")
        for e in page_ctx.get("elements", [])
    )
    prompt = (
        f"あなたはARIS（業務効率化AI）のブラウザ操作エンジンです。\n"
        f"社員の代わりにブラウザで業務タスクを実行します。\n\n"
        f"タスク: {task_description}\n"
        f"現在のURL: {page_ctx['url']}\n"
        f"ページタイトル: {page_ctx['title']}\n\n"
        f"ページ上の操作可能な要素:\n{elements_text}\n\n"
        f"上記の要素リストを使って、タスクを完了するために必要な全操作ステップを\n"
        f"JSON配列として一括で返してください。\n\n"
        f"各ステップの形式:\n"
        f'{{"type":"click",  "css":"#btn",               "description":"クリック"}}\n'
        f'{{"type":"type",   "css":"input[name=q]","text":"値","description":"入力"}}\n'
        f'{{"type":"select", "css":"select#s",    "value":"v","description":"選択"}}\n'
        f'{{"type":"press",  "key":"Enter",                    "description":"送信"}}\n'
        f'{{"type":"goto",   "url":"https://...",              "description":"移動"}}\n'
        f'{{"type":"wait",   "seconds":2,                      "description":"待機"}}\n'
        f'{{"type":"scroll", "direction":"down","amount":300,  "description":"スクロール"}}\n'
        f'{{"type":"done",   "summary":"完了理由"}}\n'
        f'{{"type":"error",  "message":"できない理由"}}\n\n'
        f"注意: cssは必ずページ上の要素リストから選ぶ。最後は{{\"type\":\"done\"}}で終わらせる。JSONのみ返す。"
    )
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key":         api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type":      "application/json",
                },
                json={
                    "model":      _CLAUDE_MODEL,
                    "max_tokens": 2048,
                    "messages":   [{"role": "user", "content": prompt}],
                },
            )
    except Exception as exc:
        logger.error("Plan generation request failed: %s", exc)
        return None

    if resp.status_code != 200:
        logger.error("Claude API error in plan: %s", resp.status_code)
        return None

    raw = resp.json()["content"][0]["text"]
    result = _extract_json(raw)
    if isinstance(result, list):
        return result
    if isinstance(result, dict) and "steps" in result:
        return result["steps"]
    logger.warning("Plan generation unexpected format: %s", raw[:200])
    return None


async def _execute_plan_step(page, step: dict) -> bool:
    """計画の1ステップをCSS Selectorで実行。成功: True / 失敗: False"""
    t = step.get("type")
    try:
        if t == "click":
            el = await page.wait_for_selector(step["css"], timeout=_ACTION_TIMEOUT)
            await el.click()
        elif t == "type":
            el = await page.wait_for_selector(step["css"], timeout=_ACTION_TIMEOUT)
            await el.click()
            await el.fill(step.get("text", ""))
        elif t == "select":
            el = await page.wait_for_selector(step["css"], timeout=_ACTION_TIMEOUT)
            await el.select_option(step.get("value", ""))
        elif t == "press":
            await page.keyboard.press(step.get("key", "Enter"))
        elif t == "goto":
            await page.goto(step["url"], wait_until="domcontentloaded", timeout=30000)
        elif t == "wait":
            await asyncio.sleep(step.get("seconds", 2))
        elif t == "scroll":
            amount = step.get("amount", 300)
            if step.get("direction") == "up":
                amount = -amount
            await page.mouse.wheel(0, amount)
        elif t in ("done", "error"):
            return True
        else:
            logger.warning("Unknown step type: %s (skip)", t)
        return True
    except Exception as exc:
        logger.warning("Step failed [%s css=%s]: %s", t, step.get("css"), exc)
        return False


async def _vision_recovery(
    page,
    step: dict,
    task_description: str,
    api_key: str,
) -> Optional[dict]:
    """失敗したステップについてスクリーンショット+VisionAPIで代替アクションを取得"""
    shot = await page.screenshot(type="png")
    shot_b64 = base64.standard_b64encode(shot).decode()
    prompt = (
        f"タスク: {task_description}\n"
        f"実行しようとしたステップ: {json.dumps(step, ensure_ascii=False)}\n"
        f"このステップが失敗しました。\n\n"
        f"スクリーンショットを見て、同じ目的を達成するための代替アクションを1つだけJSON形式で返してください。\n\n"
        f'{{"type":"click","x":100,"y":200}}\n'
        f'{{"type":"type","text":"value"}}\n'
        f'{{"type":"press","key":"Enter"}}\n'
        f'{{"type":"scroll","direction":"down","amount":300}}\n'
        f'{{"type":"goto","url":"https://..."}}\n'
        f'{{"type":"skip","reason":"スキップ可能"}}\n'
        f'{{"type":"error","message":"回復不可能"}}\n\n'
        f"JSONのみ返してください。"
    )
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key":         api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type":      "application/json",
                },
                json={
                    "model":      _CLAUDE_MODEL,
                    "max_tokens": 300,
                    "messages":   [{"role": "user", "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": shot_b64}},
                        {"type": "text",  "text": prompt},
                    ]}],
                },
            )
    except Exception as exc:
        logger.error("Vision recovery request failed: %s", exc)
        return None
    if resp.status_code != 200:
        return None
    return _extract_json(resp.json()["content"][0]["text"])


async def smart_execute(page, task_description: str, api_key: str) -> dict:
    """
    Phase1: DOM解析 + Claude計画生成
    Phase2: CSS Selector 高速実行
    Phase3: 失敗ステップのみ Vision 回復
    """
    logger.info("[Smart] Phase1: DOM解析+計画生成 — %s", task_description)
    page_ctx = await _get_page_context(page)
    plan = await _generate_plan(page_ctx, task_description, api_key)

    if plan is None:
        logger.warning("[Smart] 計画生成失敗 — 固定ステップモードにフォールバック")
        return {"success": False, "error": "Claude計画生成に失敗しました（API Key確認）"}

    logger.info("[Smart] Phase2: %d ステップを実行", len(plan))
    steps_done = 0
    for i, step in enumerate(plan):
        step_type = step.get("type")
        desc = step.get("description") or step.get("summary") or step_type
        logger.info("  [%d/%d] %s", i + 1, len(plan), desc)

        if step_type == "done":
            return {"success": True, "summary": step.get("summary", "タスク完了"), "steps": steps_done + 1}
        if step_type == "error":
            return {"success": False, "error": step.get("message", "エラー"), "steps": steps_done}

        ok = await _execute_plan_step(page, step)
        if not ok:
            logger.info("[Smart] Phase3: Vision回復試行 step=%d", i + 1)
            recovery = await _vision_recovery(page, step, task_description, api_key)
            if recovery:
                rt = recovery.get("type")
                try:
                    if rt == "click":
                        if "css" in recovery:
                            el = await page.wait_for_selector(recovery["css"], timeout=_ACTION_TIMEOUT)
                            await el.click()
                        else:
                            await page.mouse.click(recovery.get("x", 0), recovery.get("y", 0))
                    elif rt == "type":
                        await page.keyboard.type(recovery.get("text", ""), delay=50)
                    elif rt == "press":
                        await page.keyboard.press(recovery.get("key", "Enter"))
                    elif rt == "goto":
                        await page.goto(recovery["url"], wait_until="domcontentloaded", timeout=30000)
                    elif rt == "error":
                        return {"success": False, "error": recovery.get("message", "回復失敗"), "steps": steps_done}
                    # skip → continue
                except Exception as exc:
                    logger.warning("Vision recovery action failed: %s", exc)

        steps_done += 1
        if step_type in ("click", "press", "goto"):
            await asyncio.sleep(0.5)

    return {"success": True, "summary": "全ステップを実行しました", "steps": steps_done}


# ── タスク実行 ────────────────────────────────────────────────────

async def execute_task(
    client: httpx.AsyncClient,
    token: str,
    task: dict,
) -> None:
    task_id = task["task_id"]
    title   = task.get("title", task_id)
    steps   = task.get("steps", [])
    logger.info("▶ タスク実行開始: %s (%s)", title, task_id)

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        err = "playwright がインストールされていません。pip install playwright && playwright install chromium"
        logger.error(err)
        await report_result(client, token, task_id, success=False, error=err)
        return

    api_key = CFG.get("anthropic_api_key", "")

    screenshot_b64: Optional[str] = None
    try:
        # context manager を使わずに起動 → タスク完了後もブラウザを閉じない
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(
            headless=HEADLESS,
            args=["--no-sandbox", "--disable-infobars"],
        )
        page = await browser.new_page(viewport={"width": 1280, "height": 800})

        # 最初の navigate ステップを実行して初期URLへ移動
        initial_url = None
        for step in steps:
            action = step.get("action") or step.get("type", "")
            if action in ("navigate", "goto"):
                initial_url = step.get("url")
                break

        if initial_url:
            logger.info("  → navigate (初期): %s", initial_url)
            await page.goto(initial_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(1.0)

        if api_key:
            # ── スマート実行モード（Phase1+2+3） ──
            logger.info("[Smart] スマート実行モードで開始")
            result = await smart_execute(page, title, api_key)
            success     = result.get("success", False)
            result_msg  = result.get("summary") or result.get("error") or "完了"
            steps_count = result.get("steps", 0)
        else:
            # ── フォールバック: 固定ステップ実行 ──
            logger.info("[Fallback] 固定ステップモードで実行（Claude APIキー未設定）")
            for i, step in enumerate(steps):
                logger.info("  [%d/%d] %s", i + 1, len(steps), step.get("description", step.get("action", "")))
                await execute_step(page, step)
                await asyncio.sleep(0.5)
            success     = True
            result_msg  = f"タスク「{title}」が完了しました"
            steps_count = len(steps)

        # 完了スクリーンショット
        shot = await page.screenshot(type="png")
        screenshot_b64 = base64.standard_b64encode(shot).decode()
        # browser.close() しない — ユーザーが手動で閉じるまでそのまま

        if success:
            logger.info("✅ タスク完了: %s — ブラウザはそのまま残します", title)
            await report_result(
                client, token, task_id,
                success=True,
                result={"message": result_msg, "steps_executed": steps_count},
                screenshot_b64=screenshot_b64,
            )
        else:
            logger.error("❌ タスク失敗: %s — %s", title, result_msg)
            await report_result(client, token, task_id, success=False, error=result_msg, screenshot_b64=screenshot_b64)

    except Exception as e:
        logger.error("❌ タスク失敗: %s — %s", title, e)
        await report_result(client, token, task_id, success=False, error=str(e))


# ── メインループ ──────────────────────────────────────────────────

async def main() -> None:
    if not CFG["email"] or not CFG["password"]:
        logger.error(
            "メールアドレスとパスワードが設定されていません。"
            "config.json の username/password を確認してください。"
        )
        sys.exit(1)

    logger.info("=" * 50)
    logger.info("A.R.I.S. Playwright ローカルエージェント起動")
    logger.info("サーバー: %s", SERVER_URL)
    logger.info("ユーザー: %s", CFG["email"])
    logger.info("ヘッドレス: %s", HEADLESS)
    logger.info("ポーリング間隔: %d秒", POLL_INTERVAL)
    _api_key = CFG.get("anthropic_api_key", "")
    if _api_key:
        logger.info("Claude API: 有効（スマート実行モード）— key先頭5文字: %s... 長さ: %d", _api_key[:5], len(_api_key))
        if CFG.get("_env_key_conflict"):
            logger.warning("⚠ ANTHROPIC_API_KEY 環境変数と config.json のキーが異なります — config.json を優先して使用します")
    else:
        logger.info("Claude API: 未設定（固定ステップモード）")
    logger.info("=" * 50)

    token = ""
    async with httpx.AsyncClient() as client:
        # 初回ログイン
        while not token:
            try:
                token = await get_token(client)
            except Exception as e:
                logger.error("ログイン失敗: %s — %d秒後に再試行", e, POLL_INTERVAL)
                await asyncio.sleep(POLL_INTERVAL)

        # ポーリングループ
        while True:
            try:
                tasks = await fetch_pending_tasks(client, token)
                if tasks:
                    logger.info("承認済みタスク %d件 を取得", len(tasks))
                    for task in tasks:
                        await execute_task(client, token, task)
                else:
                    logger.debug("待機中... (承認済みタスクなし)")

            except PermissionError:
                logger.warning("トークン期限切れ — 再ログイン中...")
                try:
                    token = await get_token(client)
                except Exception as e:
                    logger.error("再ログイン失敗: %s", e)

            except httpx.ConnectError:
                logger.error("サーバーに接続できません: %s", SERVER_URL)

            except Exception as e:
                logger.error("ポーリングエラー: %s", e)

            await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("エージェント停止")
