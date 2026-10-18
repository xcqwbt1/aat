import re
import json
import asyncio
import logging
from urllib.parse import parse_qs

from playwright.async_api import async_playwright

# از همون فایل اصلی استفاده می‌کنیم (کلاینت، گرفتن URL و اسکریپت تزریقی)
import bot_telethon as bt

logger = logging.getLogger("diagnose")
logging.basicConfig(level=logging.INFO)

SAFE_KEYS = {'action', 'act', 'task', 'task_id', 'type', 'op', 'method', 'cmd', 'event', 'name'}

JS_KEYWORDS = {
    'if', 'for', 'while', 'switch', 'catch', 'function', 'return', 'typeof', 'await', 'async',
    'new', 'fetch', 'setTimeout', 'setInterval', 'clearTimeout', 'clearInterval', 'JSON',
    'parseInt', 'parseFloat', 'Number', 'String', 'Boolean', 'Array', 'Object', 'Math', 'Date',
    'Promise', 'alert', 'confirm', 'isNaN', 'encodeURIComponent', 'decodeURIComponent',
    'Error', 'console', 'then', 'catch', 'finally', 'getElementById', 'querySelector',
    'querySelectorAll', 'addEventListener', 'removeEventListener', 'setAttribute',
    'getAttribute', 'includes', 'indexOf', 'slice', 'split', 'join', 'map', 'filter',
    'forEach', 'push', 'toString', 'toFixed', 'trim', 'replace', 'stringify', 'parse',
    'append', 'remove', 'add', 'contains', 'preventDefault', 'stopPropagation', 'now',
    'floor', 'ceil', 'round', 'random', 'max', 'min', 'abs', 'keys', 'values', 'entries',
    'assign', 'get', 'set', 'has', 'delete', 'call', 'apply', 'bind', 'open', 'close',
}


def redact(s):
    return re.sub(r'[A-Za-z0-9_\-]{40,}', '<redacted>', s or '')


def summarize(obj, depth=0):
    """خلاصه‌ی بی‌خطر: مقدارهای کوتاه می‌مونن، رشته‌های بلند و ساختارهای تودرتو خلاصه می‌شن"""
    if isinstance(obj, dict):
        out = {}
        for k, v in list(obj.items())[:25]:
            if v is None or isinstance(v, (bool, int, float)):
                out[k] = v
            elif isinstance(v, str):
                out[k] = redact(v)[:80]
            elif isinstance(v, (dict, list)):
                out[k] = summarize(v, depth + 1) if depth < 2 else f'<{type(v).__name__} {len(v)}>'
        return out
    if isinstance(obj, list):
        return {'len': len(obj), 'first': [summarize(x, depth + 1) for x in obj[:3]]}
    return redact(str(obj))[:80]


def describe_request(post):
    """از بدنه‌ی درخواست فقط کلیدها رو نشون بده (مقدارها فقط برای کلیدهای بی‌خطر و کوتاه)"""
    if not post:
        return {}
    data = None
    try:
        data = json.loads(post)
    except Exception:
        data = {k: (v[0] if v else '') for k, v in parse_qs(post).items()}
    if not isinstance(data, dict):
        return {'type': type(data).__name__}
    out = {}
    for k, v in data.items():
        if k in SAFE_KEYS and isinstance(v, str) and len(v) <= 40:
            out[k] = v
        else:
            out[k] = '…'
    return out


async def handle_response(resp):
    req = resp.request
    try:
        if req.resource_type not in ('fetch', 'xhr'):
            return
        if 'index.php' not in resp.url:
            return
        req_desc = describe_request(req.post_data)
        text = await resp.text()
        try:
            body = summarize(json.loads(text))
        except Exception:
            body = redact(text)[:150]
        logger.info(f"📡 {req.method} index.php → {resp.status}\n    درخواست: {req_desc}\n    جواب: {body}")
    except Exception as e:
        logger.info(f"📡 خواندن جواب ناموفق: {type(e).__name__}")


async def get_source(page, name):
    try:
        src = await page.evaluate(
            "(n) => { try { const f = (0, eval)(n); return typeof f === 'function' ? f.toString() : null; } catch (e) { return null; } }",
            name,
        )
    except Exception:
        return None
    if not src or '[native code]' in src:
        return None
    return redact(src)


def print_chunks(title, text, size=3500):
    logger.info(f"\n===== {title} =====")
    for i in range(0, len(text), size):
        logger.info(text[i:i + size])


def called_names(src):
    names = set(re.findall(r'\b([A-Za-z_$][\w$]*)\s*\(', src))
    return [n for n in sorted(names) if n not in JS_KEYWORDS]


async def dump_sources(page):
    main_src = await get_source(page, 'handleTaskClick')
    if not main_src:
        logger.info("❌ handleTaskClick پیدا نشد")
        return
    print_chunks('handleTaskClick', main_src[:8000])

    seen = {'handleTaskClick'}
    queue = called_names(main_src)
    total = 0
    count = 0
    while queue and count < 20 and total < 60000:
        name = queue.pop(0)
        if name in seen:
            continue
        seen.add(name)
        src = await get_source(page, name)
        if not src:
            continue
        src = src[:3500]
        total += len(src)
        count += 1
        print_chunks(name, src)
        # یه سطح عمیق‌تر
        for sub in called_names(src):
            if sub not in seen and sub not in queue:
                queue.append(sub)


async def main():
    await bt.client.connect()
    try:
        if not await bt.client.is_user_authorized():
            logger.error("❌ Session معتبر نیست")
            return

        url = await bt.get_webapp_url()
        logger.info("🔗 URL وب‌اپ گرفته شد")

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                viewport={'width': 412, 'height': 915},
                user_agent=(
                    'Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 '
                    '(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36'
                ),
                is_mobile=True,
                has_touch=True,
            )
            await context.add_init_script(bt.INIT_SCRIPT)
            page = await context.new_page()
            page.on('response', lambda r: asyncio.ensure_future(handle_response(r)))
            page.on('console', lambda m: logger.info(f"🖥️ console.{m.type}: {m.text[:200]}") if m.type in ('error', 'warning') else None)
            page.on('pageerror', lambda e: logger.info(f"💥 pageerror: {str(e)[:200]}"))

            try:
                await page.goto(url, wait_until='networkidle', timeout=60000)
                await asyncio.sleep(6)
                await bt.click_text(page, 'Tasks')
                await asyncio.sleep(4)

                await dump_sources(page)

                for btn_id in ('btn-youtube_like_comment', 'btn-twitter_retweet'):
                    logger.info(f"\n👉 کلیک روی #{btn_id}")
                    before = await page.locator(f'#{btn_id}').first.inner_text()
                    await page.locator(f'#{btn_id}').first.click(timeout=5000)
                    await asyncio.sleep(10)
                    after = await page.locator(f'#{btn_id}').first.inner_text()
                    links = await page.evaluate("() => (window.__openedLinks || []).map(u => { try { return new URL(u).host } catch (e) { return String(u).slice(0, 40) } })")
                    logger.info(f"   متن دکمه: «{before}» ← «{after}» | لینک‌های ثبت‌شده: {links}")
            finally:
                await context.close()
                await browser.close()
    finally:
        await bt.client.disconnect()
        logger.info("✅ تموم شد")


if __name__ == "__main__":
    asyncio.run(main())
