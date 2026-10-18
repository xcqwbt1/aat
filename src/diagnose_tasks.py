import re
import json
import asyncio
import logging
from urllib.parse import urlparse, parse_qs

from playwright.async_api import async_playwright

# از همون فایل اصلی استفاده می‌کنیم (کلاینت، گرفتن URL و اسکریپت تزریقی)
import bot_telethon as bt

logger = logging.getLogger("diagnose")
logging.basicConfig(level=logging.INFO)

# دکمه‌هایی که تست می‌شن: (id دکمه، توضیح)
TARGETS = [
    ('btn-youtube_like_comment', 'Go یوتیوب'),
    ('btn-twitter_retweet', 'Claim توییتر'),
    ('btn-bio_referral', 'Claim بیو'),
]

STATE_JS = """
() => {
  const g = (n) => { try { return (0, eval)(n); } catch (e) { return '<err ' + e.name + '>'; } };
  const cfg = g('tasksConfig');
  const brief = Array.isArray(cfg)
    ? cfg.map((t) => ({ id: t && t.id, minSeconds: t && t.minSeconds, cooldown: t && t.cooldown, manual: t && t.manualReview }))
    : cfg;
  const flight = g('__atfTaskClaimInFlight');
  return {
    now: Math.floor(Date.now() / 1000),
    tasksConfig: brief,
    taskCooldowns: g('taskCooldowns'),
    taskStarts: g('taskStarts'),
    completedTasks: g('completedTasks'),
    authOk: g('__authOk'),
    inFlight: (flight && flight.size !== undefined) ? Array.from(flight) : flight,
    webdriver: navigator.webdriver,
  };
}
"""

WRAP_JS = """
() => {
  if (typeof handleTaskClick !== 'function') return 'missing';
  if (window.__htcWrapped) return 'already';
  const orig = handleTaskClick;
  const txt = (id) => { const b = document.getElementById('btn-' + id); return b ? JSON.stringify(b.innerText) : 'null'; };
  window.handleTaskClick = async function (id, link) {
    console.log('HTC enter ' + id + ' text=' + txt(id));
    try {
      const r = await orig.apply(this, arguments);
      console.log('HTC done ' + id + ' text=' + txt(id));
      return r;
    } catch (e) {
      console.log('HTC throw ' + id + ' ' + String(e).slice(0, 150));
      throw e;
    }
  };
  window.__htcWrapped = true;
  return 'ok';
}
"""

HIT_JS = """
(id) => {
  const b = document.getElementById(id);
  if (!b) return null;
  const r = b.getBoundingClientRect();
  const el = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
  return {
    tag: el && el.tagName,
    id: el && el.id,
    cls: el ? String(el.className).slice(0, 60) : '',
    same: !!(el && (el === b || b.contains(el))),
  };
}
"""


def redact(s):
    return re.sub(r'[A-Za-z0-9_\-]{40,}', '<redacted>', s or '')


def action_of(url):
    try:
        return parse_qs(urlparse(url).query).get('action', ['-'])[0]
    except Exception:
        return '-'


def on_request(req):
    try:
        if 'index.php' in req.url:
            logger.info(f"➡️ {req.method} action={action_of(req.url)} ({req.resource_type})")
    except Exception:
        pass


async def on_response(resp):
    try:
        if 'index.php' not in resp.url:
            return
        action = action_of(resp.url)
        text = await resp.text()
        logger.info(f"⬅️ action={action} → {resp.status} | {redact(text)[:220]}")
    except Exception as e:
        logger.info(f"⬅️ خواندن جواب ناموفق: {type(e).__name__}")


def on_console(msg):
    try:
        t = msg.text or ''
        if t.startswith('HTC') or msg.type in ('error', 'warning'):
            logger.info(f"🖥️ console.{msg.type}: {redact(t)[:220]}")
    except Exception:
        pass


async def dump_state(page, title):
    try:
        state = await page.evaluate(STATE_JS)
        logger.info(f"\n🧠 وضعیت اپ ({title}):\n{redact(json.dumps(state, ensure_ascii=False, default=str))[:3500]}")
    except Exception as e:
        logger.info(f"🧠 وضعیت اپ ({title}) ناموفق: {type(e).__name__}")


async def btn_text(page, btn_id):
    try:
        return (await page.locator(f'#{btn_id}').first.inner_text(timeout=2000)).strip()
    except Exception:
        return None


async def try_methods(page, btn_id, label):
    logger.info(f"\n================ {label} (#{btn_id}) ================")
    loc = page.locator(f'#{btn_id}').first
    try:
        await loc.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass

    start_text = await btn_text(page, btn_id)
    logger.info(f"متن اولیه: «{start_text}»")
    if start_text is None:
        logger.info("⚠️ دکمه پیدا نشد")
        return

    try:
        hit = await page.evaluate(HIT_JS, btn_id)
        logger.info(f"🎯 المانی که روی مرکز دکمه قرار داره: {hit}")
    except Exception:
        pass

    # لینک از attribute onclick
    link = ''
    try:
        onclick = await loc.get_attribute('onclick') or ''
        m = re.search(r"'([^']+)'\s*,\s*'([^']*)'", onclick)
        if m:
            link = m.group(2)
    except Exception:
        pass

    async def mouse_click():
        await loc.click(timeout=4000)

    async def touch_tap():
        box = await loc.bounding_box()
        if not box:
            raise RuntimeError('no box')
        await page.touchscreen.tap(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)

    async def direct_call():
        task_id = btn_id.replace('btn-', '', 1)
        res = await page.evaluate("([i, l]) => handleTaskClick(i, l)", [task_id, link])
        logger.info(f"   مقدار برگشتی handleTaskClick: {res}")

    for name, fn in (('کلیک ماوس', mouse_click), ('تپ لمسی', touch_tap), ('صدا زدن مستقیم تابع', direct_call)):
        logger.info(f"\n👉 روش: {name}")
        try:
            await fn()
        except Exception as e:
            logger.info(f"   ❌ خطا: {type(e).__name__}: {str(e)[:150]}")
        await asyncio.sleep(9)
        now_text = await btn_text(page, btn_id)
        links = await page.evaluate(
            "() => (window.__openedLinks || []).map(u => { try { return new URL(u).host } catch (e) { return String(u).slice(0, 40) } })"
        )
        logger.info(f"   متن دکمه: «{start_text}» ← «{now_text}» | لینک‌های ثبت‌شده: {links}")
        if now_text != start_text:
            logger.info("   ✅ این روش اثر داشت، بقیه‌ی روش‌ها برای این دکمه رد می‌شن")
            break


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
            page.on('request', on_request)
            page.on('response', lambda r: asyncio.ensure_future(on_response(r)))
            page.on('console', on_console)
            page.on('pageerror', lambda e: logger.info(f"💥 pageerror: {redact(str(e))[:200]}"))

            try:
                await page.goto(url, wait_until='networkidle', timeout=60000)
                await asyncio.sleep(8)
                await bt.click_text(page, 'Tasks')
                await asyncio.sleep(5)

                wrap = await page.evaluate(WRAP_JS)
                logger.info(f"🔧 wrap کردن handleTaskClick: {wrap}")

                await dump_state(page, 'قبل از تست')

                for btn_id, label in TARGETS:
                    await try_methods(page, btn_id, label)

                await dump_state(page, 'بعد از تست')
                await bt.save_shot(page, 'diag_final')
            finally:
                await context.close()
                await browser.close()
    finally:
        await bt.client.disconnect()
        logger.info("✅ تموم شد")


if __name__ == "__main__":
    asyncio.run(main())
