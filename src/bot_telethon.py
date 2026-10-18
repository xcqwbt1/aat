import os
import re
import asyncio
import logging
import random
import json
from datetime import datetime, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError
from telethon.tl.functions.messages import RequestWebViewRequest
from telethon.tl.types import (
    InputUser,
    KeyboardButtonWebView,
    KeyboardButtonSimpleWebView,
)
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# منطقه‌ی زمانی
TIMEZONE = ZoneInfo('Asia/Tehran')

# خواندن از Environment Variables
API_ID_RAW = os.getenv('API_ID')
API_HASH = os.getenv('API_HASH')
SESSION_STRING = os.getenv('TELEGRAM_SESSION', '').strip()
SESSION_NAME = 'user_session'  # نام فایل session (فقط برای اجرای لوکال)

# اجرای اجباری (بدون بررسی زمان‌بندی) - برای اجرای دستی
FORCE_RUN = os.getenv('FORCE_RUN', 'false').strip().lower() == 'true'

# ذخیره‌ی DOM برای دیباگ (با DUMP_DOM=false خاموش می‌شه)
DUMP_DOM = os.getenv('DUMP_DOM', 'true').strip().lower() != 'false'

BOT_USERNAME = 'ATF_AIRDROP_bot'
WEBAPP_BASE = 'https://atfminers.asloni.online/miner/index.html'
APP_HOST = 'atfminers.asloni.online'
SCREENSHOT_DIR = 'screenshots'

# --- زمان‌بندی: تقریباً هر ساعت ---
STATE_FILE = 'schedule_state.json'
MIN_GAP_MIN = 55
MAX_GAP_MIN = 65
RETRY_GAP_MIN = (20, 40)

# --- آنلاین شدن اپ ---
ONLINE_WAIT_MAX = 180
WARMUP_TABS = ['Miners', 'Friends', 'Profile']

# --- صفحه‌ی اول (Mine) ---
COIN_TAPS = (8, 15)
COIN_TAP_DELAY = (0.3, 1.2)
CLAIM_CLICKS = (3, 5)
CLAIM_CLICK_DELAY = (2, 5)

# --- رفتار نامنظم داخل اپ ---
START_JITTER = (0, 90)
WANDER_CHANCE = 0.30
EXTRA_PAUSE_CHANCE = 0.15
FINAL_MINE_CLAIM_CHANCE = 1.0
WANDER_TABS = ['Miners', 'Friends', 'Profile']

# --- تسک‌ها ---
AFTER_GO_WAIT = (4, 8)        # صبر کوتاه بعد از Go (مثل دیدن صفحه‌ی خارجی)
CLAIM_APPEAR_MAX = 45         # حداکثر صبر برای تبدیل شدن Go به Claim بعد از شبیه‌سازی برگشت (ثانیه)
ROW_CHECK_FIRST = 8           # صبر قبل از اولین بررسی تغییر ردیف (ثانیه)
MAX_TASKS = 6

if not API_ID_RAW or not API_HASH:
    raise SystemExit("❌ API_ID یا API_HASH تنظیم نشده (توی Secrets و env چک کن)")

API_ID = int(API_ID_RAW)

# ساخت کلاینت: اگه StringSession داریم از اون، وگرنه از فایل
if SESSION_STRING:
    logger.info(f"🔑 استفاده از StringSession (طول: {len(SESSION_STRING)})")
    try:
        client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    except Exception as e:
        raise SystemExit(
            f"❌ مقدار TELEGRAM_SESSION خراب یا ناقصه ({type(e).__name__}). "
            "دوباره StringSession بساز و کامل (بدون فاصله/خط اضافه) توی secret بذار."
        )
else:
    logger.info("📁 TELEGRAM_SESSION خالیه، استفاده از فایل session")
    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)


# اسکریپتی که قبل از لود اپ تزریق می‌شه:
# - openLink و window.open رو می‌گیره و ثبت می‌کنه
# - ثبت می‌کنه اپ به چه رویدادهایی (visibilitychange، focus، ...) گوش می‌ده
INIT_SCRIPT = """
(() => {
  window.__openedLinks = [];
  window.__listenTypes = {};
  window.__tgEvents = {};
  const record = (u) => { try { window.__openedLinks.push(String(u)); } catch (e) {} };

  // ثبت نوع رویدادهایی که روی window/document گوش داده می‌شه
  const origAdd = EventTarget.prototype.addEventListener;
  EventTarget.prototype.addEventListener = function (type, fn, opts) {
    try {
      if (this === window || this === document) {
        const k = (this === window ? 'window:' : 'document:') + type;
        window.__listenTypes[k] = (window.__listenTypes[k] || 0) + 1;
      }
    } catch (e) {}
    return origAdd.call(this, type, fn, opts);
  };

  window.open = function (u) { record(u); return null; };

  const patchWebApp = () => {
    const tg = window.Telegram && window.Telegram.WebApp;
    if (!tg || tg.__patched) return;
    tg.__patched = true;
    try { tg.openLink = function (u) { record(u); }; } catch (e) {}
    try { tg.openTelegramLink = function (u) { record(u); }; } catch (e) {}
    try {
      const origOn = tg.onEvent && tg.onEvent.bind(tg);
      if (origOn) {
        tg.onEvent = function (type, cb) {
          window.__tgEvents[type] = (window.__tgEvents[type] || 0) + 1;
          return origOn(type, cb);
        };
      }
    } catch (e) {}
  };

  patchWebApp();
  let tries = 0;
  const iv = setInterval(() => {
    patchWebApp();
    if (++tries > 200) clearInterval(iv);
  }, 50);

  document.addEventListener('click', (ev) => {
    const a = ev.target && ev.target.closest && ev.target.closest('a[href]');
    if (a && /^https?:/i.test(a.href) && a.target === '_blank') {
      record(a.href);
      ev.preventDefault();
    }
  }, true);
})();
"""

# شبیه‌سازی «رفتن به صفحه‌ی بیرونی و برگشتن»
SIMULATE_RETURN_JS = """
() => {
  const fire = (target, type) => { try { target.dispatchEvent(new Event(type)); } catch (e) {} };
  const setHidden = (hidden) => {
    try {
      Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden });
      Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => (hidden ? 'hidden' : 'visible') });
    } catch (e) {}
  };
  const tg = window.Telegram && window.Telegram.WebApp;
  const tgFire = (type) => {
    try { if (tg && tg.receiveEvent) tg.receiveEvent(type, {}); } catch (e) {}
    try {
      if (window.Telegram && window.Telegram.WebView && window.Telegram.WebView.receiveEvent) {
        window.Telegram.WebView.receiveEvent(type, {});
      }
    } catch (e) {}
  };

  // رفتن
  fire(window, 'blur');
  setHidden(true);
  fire(document, 'visibilitychange');
  fire(window, 'pagehide');
  tgFire('visibility_changed');
  tgFire('deactivated');

  // برگشت (بعد از چند ثانیه، با تاخیر)
  return new Promise((resolve) => {
    setTimeout(() => {
      setHidden(false);
      fire(document, 'visibilitychange');
      fire(window, 'pageshow');
      fire(window, 'focus');
      tgFire('visibility_changed');
      tgFire('activated');
      tgFire('viewport_changed');
      resolve(true);
    }, 4000);
  });
}
"""

# ذخیره‌ی سورس توابع مرتبط اپ (با پاک کردن توکن‌های بلند)
APP_FUNCTIONS_JS = """
() => {
  const out = [];
  const nameRe = /task|claim|visib|return|focus|openlink|reward|verify|process|check/i;
  const redact = (s) => s.replace(/[A-Za-z0-9_\\-]{40,}/g, '<redacted>');
  let total = 0;
  const names = Object.getOwnPropertyNames(window).filter((k) => {
    try { return typeof window[k] === 'function' && nameRe.test(k); } catch (e) { return false; }
  });
  if (typeof window.handleTaskClick === 'function' && !names.includes('handleTaskClick')) names.unshift('handleTaskClick');
  names.sort((a, b) => (a === 'handleTaskClick' ? -1 : b === 'handleTaskClick' ? 1 : 0));
  for (const k of names) {
    let src = '';
    try { src = window[k].toString(); } catch (e) { continue; }
    if (src.includes('[native code]')) continue;
    src = redact(src).slice(0, 6000);
    if (total + src.length > 90000) break;
    total += src.length;
    out.push('// ===== ' + k + ' =====\\n' + src);
  }
  return {
    functions: out,
    listenTypes: window.__listenTypes || {},
    tgEvents: window.__tgEvents || {},
    hasTelegram: !!(window.Telegram && window.Telegram.WebApp),
    platform: (window.Telegram && window.Telegram.WebApp && window.Telegram.WebApp.platform) || '',
  };
}
"""

# --- جاوااسکریپت ذخیره‌ی DOM ---

# HTML صفحه بدون script/noscript و بدون داده‌ی سنگین
DOM_HTML_JS = """
() => {
  const clone = document.documentElement.cloneNode(true);
  clone.querySelectorAll('script,noscript').forEach((e) => e.remove());
  clone.querySelectorAll('img[src^="data:"]').forEach((e) => e.setAttribute('src', 'data:(حذف شد)'));
  clone.querySelectorAll('input,textarea').forEach((e) => e.removeAttribute('value'));
  return clone.outerHTML;
}
"""

# خلاصه‌ی المان‌های کلیک‌پذیر و دکمه‌های Go/Claim/CLAIM/Processing با والدها
DOM_SUMMARY_JS = """
() => {
  const short = (s, n) => (s || '').replace(/\\s+/g, ' ').trim().slice(0, n);
  const cls = (el) => (typeof el.className === 'string' ? el.className : (el.getAttribute('class') || ''));
  const desc = (el) => {
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    return {
      tag: el.tagName.toLowerCase(),
      id: el.id || '',
      class: short(cls(el), 80),
      role: el.getAttribute('role') || '',
      disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
      pe: cs.pointerEvents,
      cursor: cs.cursor,
      rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
      text: short(el.innerText, 60),
    };
  };

  const out = { clickable: [], actions: [] };

  document.querySelectorAll('button,[role="button"],a,[onclick]').forEach((el) => {
    out.clickable.push(desc(el));
  });

  const wanted = /^(go|claim|processing\\.*|checking\\.*|verifying\\.*)$/i;
  document.querySelectorAll('body *').forEach((el) => {
    const t = short(el.innerText, 30);
    if (!wanted.test(t)) return;
    const deeper = Array.from(el.children).some((c) => wanted.test(short(c.innerText, 30)));
    if (deeper) return;
    const chain = [];
    let cur = el;
    for (let i = 0; i < 6 && cur && cur !== document.body; i++) {
      chain.push(desc(cur));
      cur = cur.parentElement;
    }
    out.actions.push({ text: t, chain });
  });

  out.openedLinks = (window.__openedLinks || []).map((u) => {
    try { return new URL(u).host; } catch (e) { return String(u).slice(0, 40); }
  });
  out.url = location.origin + location.pathname;
  return out;
}
"""


def _fmt(d):
    return (
        f"<{d['tag']}"
        f"{' #' + d['id'] if d['id'] else ''}"
        f"{' .' + d['class'] if d['class'] else ''}"
        f"{' role=' + d['role'] if d['role'] else ''}>"
        f" rect={d['rect']} pe={d['pe']} cursor={d['cursor']}"
        f"{' DISABLED' if d['disabled'] else ''} text=«{d['text']}»"
    )


async def dump_dom(page, name):
    """ذخیره‌ی خلاصه‌ی دکمه‌ها (و HTML بدون script) برای دیباگ"""
    if not DUMP_DOM:
        return
    try:
        os.makedirs(SCREENSHOT_DIR, exist_ok=True)

        # HTML فقط برای چند مرحله‌ی کلیدی ذخیره می‌شه تا حجم artifact کم بمونه
        if name in ('tasks', 'go1_after'):
            html = await page.evaluate(DOM_HTML_JS)
            if len(html) > 600000:
                html = html[:600000] + '\n<!-- بریده شد -->'
            with open(os.path.join(SCREENSHOT_DIR, f'dom_{name}.html'), 'w', encoding='utf-8') as f:
                f.write(html)

        summary = await page.evaluate(DOM_SUMMARY_JS)
        lines = [
            f"# DOM خلاصه: {name}",
            f"# آدرس: {summary.get('url')}",
            f"# لینک‌های ثبت‌شده (فقط دامنه): {summary.get('openedLinks')}",
            "",
            f"## دکمه‌های Go / Claim / Processing ({len(summary['actions'])} مورد)",
        ]
        for i, a in enumerate(summary['actions'], 1):
            lines.append(f"[{i}] متن: {a['text']}")
            for lvl, d in enumerate(a['chain']):
                lines.append(f"    {'  ' * lvl}↑{lvl} {_fmt(d)}")
        lines.append("")
        lines.append(f"## همه‌ی المان‌های کلیک‌پذیر ({len(summary['clickable'])} مورد)")
        for d in summary['clickable']:
            lines.append(f"  {_fmt(d)}")

        with open(os.path.join(SCREENSHOT_DIR, f'dom_{name}.txt'), 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))
        logger.info(f"🧬 DOM ذخیره شد: dom_{name}")
    except Exception as e:
        logger.warning(f"⚠️ ذخیره‌ی DOM ناموفق ({name}): {type(e).__name__}")


async def dump_app_functions(page):
    """ذخیره‌ی سورس handleTaskClick و توابع مرتبط + رویدادهایی که اپ گوش می‌ده"""
    if not DUMP_DOM:
        return
    try:
        os.makedirs(SCREENSHOT_DIR, exist_ok=True)
        info = await page.evaluate(APP_FUNCTIONS_JS)
        lines = [
            f"# Telegram.WebApp موجود: {info.get('hasTelegram')} | platform: {info.get('platform')}",
            f"# رویدادهای window/document که اپ گوش می‌ده: {json.dumps(info.get('listenTypes'), ensure_ascii=False)}",
            f"# رویدادهای SDK تلگرام (onEvent): {json.dumps(info.get('tgEvents'), ensure_ascii=False)}",
            f"# تعداد توابع ذخیره‌شده: {len(info.get('functions', []))}",
            "",
        ]
        lines.extend(info.get('functions', []))
        with open(os.path.join(SCREENSHOT_DIR, 'app_functions.txt'), 'w', encoding='utf-8') as f:
            f.write('\n\n'.join(lines))
        logger.info(
            f"🧬 app_functions.txt ذخیره شد ({len(info.get('functions', []))} تابع) | "
            f"رویدادها: {list((info.get('listenTypes') or {}).keys())} | tg: {list((info.get('tgEvents') or {}).keys())}"
        )
    except Exception as e:
        logger.warning(f"⚠️ ذخیره‌ی توابع اپ ناموفق: {type(e).__name__}")


def attach_diagnostics(page):
    """لاگ خطاهای کنسول، dialog ها و درخواست‌های fetch/xhr (فقط متد/مسیر/status، بدون query و بدنه)"""

    def short_url(u):
        try:
            p = urlparse(u)
            return f"{p.netloc}{p.path}"[:100]
        except Exception:
            return str(u)[:100]

    def on_console(msg):
        try:
            if msg.type in ('error', 'warning'):
                logger.info(f"🖥️ console.{msg.type}: {msg.text[:200]}")
        except Exception:
            pass

    def on_pageerror(err):
        logger.info(f"💥 pageerror: {str(err)[:200]}")

    async def on_dialog(dialog):
        logger.info(f"💬 dialog({dialog.type}): {dialog.message[:150]}")
        try:
            await dialog.accept()
        except Exception:
            pass

    def on_response(resp):
        try:
            rt = resp.request.resource_type
            if rt in ('fetch', 'xhr'):
                logger.info(f"🌐 {resp.request.method} {short_url(resp.url)} → {resp.status}")
        except Exception:
            pass

    def on_requestfailed(req):
        try:
            if req.resource_type in ('fetch', 'xhr'):
                logger.info(f"🚫 ناموفق: {req.method} {short_url(req.url)}")
        except Exception:
            pass

    page.on('console', on_console)
    page.on('pageerror', on_pageerror)
    page.on('dialog', lambda d: asyncio.ensure_future(on_dialog(d)))
    page.on('response', on_response)
    page.on('requestfailed', on_requestfailed)


# ---------- زمان‌بندی ----------

def load_next_run():
    try:
        with open(STATE_FILE, 'r') as f:
            data = json.load(f)
        return datetime.fromisoformat(data['next_run'])
    except Exception:
        return None


def save_next_run(gap, reason):
    next_run = datetime.now(TIMEZONE) + timedelta(minutes=gap)
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump({
                'next_run': next_run.isoformat(),
                'gap_minutes': gap,
                'reason': reason,
            }, f, indent=2)
    except Exception as e:
        logger.warning(f"⚠️ ذخیره‌ی زمان‌بندی ناموفق: {e}")
    logger.info(
        f"📅 اجرای بعدی ({reason}): {next_run.strftime('%Y-%m-%d %H:%M')} تهران "
        f"(بعد از {gap // 60} ساعت و {gap % 60} دقیقه)"
    )


def schedule_next_run():
    save_next_run(random.randint(MIN_GAP_MIN, MAX_GAP_MIN), 'success')


def schedule_retry():
    save_next_run(random.randint(*RETRY_GAP_MIN), 'retry')


def is_due():
    next_run = load_next_run()
    now = datetime.now(TIMEZONE)
    if next_run is None:
        logger.info("🆕 زمان‌بندی قبلی پیدا نشد، اجرا می‌شه")
        return True
    if now >= next_run:
        logger.info("✅ وقت اجرا رسیده")
        return True
    remaining = int((next_run - now).total_seconds() // 60)
    logger.info(
        f"⏰ هنوز وقتش نشده. اجرای بعدی: {next_run.strftime('%H:%M')} تهران "
        f"({remaining} دقیقه‌ی دیگه)"
    )
    return False


# ---------- ابزارها ----------

async def random_delay(min_s=2, max_s=6):
    delay = random.uniform(min_s, max_s)
    if random.random() < EXTRA_PAUSE_CHANCE:
        extra = random.uniform(5, 12)
        delay += extra
        logger.info(f"💤 مکث اضافه {extra:.1f} ثانیه")
    logger.info(f"⏳ تاخیر {delay:.1f} ثانیه...")
    await asyncio.sleep(delay)


async def get_webapp_url():
    """گرفتن URL احراز هویت‌شده‌ی وب‌اپ از تلگرام (فقط داخل حافظه، چاپ نمی‌شه)"""
    bot_entity = await client.get_entity(BOT_USERNAME)
    input_user = InputUser(
        user_id=bot_entity.id,
        access_hash=bot_entity.access_hash,
    )

    await client.send_message(bot_entity, '/start')
    await asyncio.sleep(4)
    messages = await client.get_messages(bot_entity, limit=10)

    button_url = None
    for msg in messages:
        if not msg.reply_markup:
            continue
        for row in getattr(msg.reply_markup, 'rows', []):
            for btn in row.buttons:
                if isinstance(btn, (KeyboardButtonWebView, KeyboardButtonSimpleWebView)):
                    button_url = btn.url
                    break
            if button_url:
                break
        if button_url:
            break

    if not button_url:
        button_url = WEBAPP_BASE
        logger.warning("⚠️ دکمه‌ی وب‌اپ توی پیام پیدا نشد، استفاده از آدرس ثابت")

    result = await client(RequestWebViewRequest(
        peer=input_user,
        bot=input_user,
        platform='android',
        url=button_url,
    ))
    return result.url


async def save_shot(page, name):
    try:
        os.makedirs(SCREENSHOT_DIR, exist_ok=True)
        path = os.path.join(SCREENSHOT_DIR, f"{name}.png")
        await page.screenshot(path=path, full_page=False)
        logger.info(f"📸 اسکرین‌شات: {path}")
    except Exception as e:
        logger.warning(f"⚠️ اسکرین‌شات ذخیره نشد: {e}")


# ---------- کلیک مطمئن ----------

async def robust_click(locator, label=''):
    """کلیک با سه روش پشت‌سرهم: عادی، با مختصات، dispatch_event"""
    try:
        await locator.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass

    try:
        await locator.click(timeout=4000)
        logger.info(f"   ↳ [{label}] کلیک عادی انجام شد")
        return 'normal'
    except Exception as e:
        logger.info(f"   ↳ [{label}] کلیک عادی ناموفق ({type(e).__name__})")

    try:
        box = await locator.bounding_box()
        if box:
            page = locator.page
            x = box['x'] + box['width'] / 2
            y = box['y'] + box['height'] / 2
            await page.mouse.click(x, y)
            logger.info(f"   ↳ [{label}] کلیک با مختصات ({x:.0f}, {y:.0f})")
            return 'coords'
    except Exception as e:
        logger.info(f"   ↳ [{label}] کلیک با مختصات ناموفق ({type(e).__name__})")

    try:
        await locator.dispatch_event('click')
        logger.info(f"   ↳ [{label}] dispatch_event انجام شد")
        return 'dispatch'
    except Exception as e:
        logger.info(f"   ↳ [{label}] dispatch_event ناموفق ({type(e).__name__})")

    return None


def clickable_locator(page, text):
    pattern = re.compile(rf'^\s*{re.escape(text)}\s*$')
    return page.locator('button, [role="button"], a').filter(has_text=pattern)


async def list_buttons(page, text):
    """لیست دکمه‌های قابل‌دیدن و فعال با متن مشخص"""
    found = []
    for loc in (clickable_locator(page, text), page.get_by_text(text, exact=True)):
        try:
            count = await loc.count()
        except Exception:
            continue
        for idx in range(count):
            item = loc.nth(idx)
            try:
                if await item.is_visible() and await item.is_enabled():
                    found.append(item)
            except Exception:
                continue
        if found:
            break
    return found


async def count_buttons(page, text):
    return len(await list_buttons(page, text))


async def click_text(page, text, timeout=15000):
    deadline = asyncio.get_event_loop().time() + timeout / 1000
    while True:
        buttons = await list_buttons(page, text)
        if buttons:
            method = await robust_click(buttons[0], text)
            if method:
                logger.info(f"✅ کلیک شد: {text} (روش: {method})")
                return True
        if asyncio.get_event_loop().time() >= deadline:
            logger.warning(f"⚠️ دکمه‌ی '{text}' پیدا یا کلیک نشد")
            return False
        await asyncio.sleep(1)


async def click_visible(page, text, randomize=False):
    buttons = await list_buttons(page, text)
    if randomize:
        random.shuffle(buttons)
    for btn in buttons:
        method = await robust_click(btn, text)
        if method:
            logger.info(f"✅ کلیک شد: {text} (روش: {method})")
            return True
    return False


async def close_extra_tabs(context, page):
    for extra in list(context.pages):
        if extra != page:
            try:
                await extra.close()
                logger.info("🗙 تب خارجی بسته شد")
            except Exception:
                pass

    if page.url and APP_HOST not in page.url:
        try:
            await page.go_back(timeout=15000)
            logger.info("↩️ برگشت به اپ")
        except Exception:
            pass


# ---------- آنلاین شدن ----------

async def is_online(page):
    try:
        return await page.get_by_text('ONLINE', exact=True).first.is_visible(timeout=1000)
    except Exception:
        return False


async def wait_until_online(page, action_log):
    if await is_online(page):
        logger.info("🟢 اپ از قبل ONLINE است")
        return True

    logger.info("🕐 اپ هنوز ONLINE نیست، شروع گشت اولیه...")
    start = asyncio.get_event_loop().time()
    tabs = WARMUP_TABS[:]
    random.shuffle(tabs)
    tab_idx = 0

    while asyncio.get_event_loop().time() - start < ONLINE_WAIT_MAX:
        if await is_online(page):
            elapsed = asyncio.get_event_loop().time() - start
            logger.info(f"🟢 اپ ONLINE شد (بعد از {elapsed:.0f} ثانیه)")
            action_log.append({
                'action': 'WAIT_ONLINE',
                'time': datetime.now().isoformat(),
                'seconds': round(elapsed),
                'success': True,
            })
            return True

        tab = tabs[tab_idx % len(tabs)]
        tab_idx += 1
        await click_text(page, tab, timeout=5000)
        await asyncio.sleep(random.uniform(15, 30))
        await click_text(page, 'Mine', timeout=5000)
        await asyncio.sleep(random.uniform(10, 20))

    logger.warning("⚠️ تا سقف زمان، ONLINE ظاهر نشد؛ ادامه با هشدار")
    await save_shot(page, '01b_not_online')
    action_log.append({
        'action': 'WAIT_ONLINE',
        'time': datetime.now().isoformat(),
        'success': False,
    })
    return False


# ---------- صفحه‌ی اول: دایره‌ی ATF و CLAIM ----------

async def read_gain(page):
    try:
        body = await page.inner_text('body', timeout=3000)
        m = re.search(r'\+\s*(\d+(?:\.\d+)?)', body)
        if m:
            return float(m.group(1))
    except Exception:
        pass
    return None


async def find_coin_center(page):
    vp = page.viewport_size or {'width': 412, 'height': 915}
    vw, vh = vp['width'], vp['height']
    try:
        candidates = await page.evaluate(
            """() => {
                const out = [];
                for (const el of document.querySelectorAll('img,canvas,svg,div')) {
                    const r = el.getBoundingClientRect();
                    if (r.width < 150 || r.width > 360) continue;
                    if (r.height < 150 || r.height > 360) continue;
                    const ratio = r.width / r.height;
                    if (ratio < 0.85 || ratio > 1.15) continue;
                    if (r.bottom < 0 || r.top > window.innerHeight) continue;
                    out.push({
                        x: r.x + r.width / 2,
                        y: r.y + r.height / 2,
                        w: r.width,
                        tag: el.tagName,
                    });
                }
                return out;
            }"""
        )
    except Exception as e:
        logger.warning(f"⚠️ جستجوی دایره ناموفق: {type(e).__name__}")
        candidates = []

    good = [
        c for c in candidates
        if abs(c['x'] - vw / 2) < vw * 0.15 and vh * 0.30 < c['y'] < vh * 0.80
    ]
    if good:
        good.sort(key=lambda c: (c['tag'] not in ('IMG', 'CANVAS'), abs(c['x'] - vw / 2)))
        c = good[0]
        logger.info(f"🪙 دایره پیدا شد ({c['tag']}, عرض {c['w']:.0f}) در ({c['x']:.0f}, {c['y']:.0f})")
        return c['x'], c['y'], min(c['w'], 300)

    fx, fy = vw * 0.5, vh * 0.52
    logger.warning(f"⚠️ دایره پیدا نشد، استفاده از نقطه‌ی تقریبی ({fx:.0f}, {fy:.0f})")
    return fx, fy, 200


async def tap_coin(page, action_log):
    x, y, size = await find_coin_center(page)
    taps = random.randint(*COIN_TAPS)
    spread = max(10, size * 0.12)
    before = await read_gain(page)
    logger.info(f"👆 شروع {taps} تپ روی دایره‌ی ATF (عدد قبل: {before})")

    done = 0
    for i in range(taps):
        tx = x + random.uniform(-spread, spread)
        ty = y + random.uniform(-spread, spread)
        try:
            await page.touchscreen.tap(tx, ty)
        except Exception:
            try:
                await page.mouse.click(tx, ty)
            except Exception as e:
                logger.warning(f"⚠️ تپ {i+1} ناموفق: {type(e).__name__}")
                continue
        done += 1
        await asyncio.sleep(random.uniform(*COIN_TAP_DELAY))

    await asyncio.sleep(1.5)
    after = await read_gain(page)
    changed = (before is not None and after is not None and before != after)
    logger.info(
        f"🎯 تپ انجام‌شده: {done} از {taps} | عدد قبل: {before} ← بعد: {after} "
        f"| {'✅ اثر داشت' if changed else '❓ تغییر عدد دیده نشد'}"
    )
    action_log.append({
        'action': 'COIN_TAPS',
        'time': datetime.now().isoformat(),
        'count': done,
        'gain_before': before,
        'gain_after': after,
        'effect_seen': changed,
        'success': done > 0,
    })
    await random_delay(1, 3)
    await save_shot(page, '02a_coin_taps')
    return done


async def multi_claim(page, action_log):
    clicks = random.randint(*CLAIM_CLICKS)
    logger.info(f"🟡 شروع {clicks} کلیک روی CLAIM صفحه‌ی اول")

    done = 0
    for i in range(clicks):
        before = await read_gain(page)
        ok = await click_text(page, 'CLAIM', timeout=5000)
        if ok:
            done += 1
            await asyncio.sleep(2)
            after = await read_gain(page)
            logger.info(f"   عدد قبل از CLAIM: {before} ← بعد: {after}")
        await random_delay(*CLAIM_CLICK_DELAY)

    logger.info(f"🎯 تعداد کلیک CLAIM: {done} از {clicks}")
    action_log.append({
        'action': 'MINE_CLAIMS',
        'time': datetime.now().isoformat(),
        'count': done,
        'success': done > 0,
    })
    return done


# ---------- تسک‌ها (ردیف‌محور) ----------

# پیدا کردن کارت ردیف از روی دکمه: بالاترین والدی که فقط یک دکمه‌ی Go/Claim داره
ROW_JS = """
(btn) => {
  const isAction = (el) => {
    const t = (el.innerText || '').trim();
    return t === 'Go' || t === 'Claim';
  };
  let row = btn;
  let cur = btn.parentElement;
  while (cur && cur !== document.body) {
    const actions = Array.from(cur.querySelectorAll('button,[role="button"],a,div,span'))
      .filter((e) => e.children.length === 0 && isAction(e));
    if (actions.length > 1) break;
    row = cur;
    cur = cur.parentElement;
  }
  return row;
}
"""


async def row_title(btn):
    """عنوان کارت تسک (چند کلمه‌ی اول متن ردیف) برای شناسایی ردیف بعد از تغییر"""
    try:
        handle = await btn.evaluate_handle(ROW_JS)
        text = await handle.evaluate("(el) => (el.innerText || '').trim()")
        lines = [l.strip() for l in text.split('\n') if l.strip()]
        lines = [l for l in lines if l not in ('Go', 'Claim')]
        return ' '.join(lines[:2])[:60]
    except Exception:
        return ''


async def find_row_button(page, title, text):
    """دکمه‌ای با متن مشخص (Go/Claim) داخل ردیفی که عنوانش title هست"""
    if not title:
        return None
    for btn in await list_buttons(page, text):
        if await row_title(btn) == title:
            return btn
    return None


async def row_button_text(page, button_id):
    """متن فعلی دکمه‌ی یک تسک (با id ثابتش مثل btn-youtube_like_comment)"""
    if not button_id:
        return None
    try:
        return (await page.locator(f'#{button_id}').first.inner_text(timeout=2000)).strip()
    except Exception:
        return None


async def read_opened_links(page):
    try:
        links = await page.evaluate("() => window.__openedLinks || []")
    except Exception:
        return []
    out = []
    for u in links:
        try:
            out.append(urlparse(u).netloc or u[:40])
        except Exception:
            out.append(str(u)[:40])
    return out


async def button_id_of(btn):
    try:
        return await btn.get_attribute('id')
    except Exception:
        return None


async def claim_ready_tasks(page, action_log):
    """Claim همه‌ی تسک‌های آماده؛ موفقیت = تغییر متن/ناپدید شدن دکمه‌ی Claim همون ردیف"""
    claimed = 0
    seen_titles = set()
    for n in range(MAX_TASKS):
        buttons = await list_buttons(page, 'Claim')
        target = None
        title = ''
        for b in buttons:
            t = await row_title(b)
            if t not in seen_titles:
                target, title = b, t
                break
        if target is None:
            break

        seen_titles.add(title)
        bid = await button_id_of(target)
        logger.info(f"🔎 Claim تسک: «{title}» (id: {bid})")
        await save_shot(page, f'04_claim{n+1}_before')
        await dump_dom(page, f'claim{n+1}_before')
        method = await robust_click(target, 'Claim')
        await asyncio.sleep(4)

        now_text = await row_button_text(page, bid)
        effect = now_text is not None and now_text != 'Claim'
        logger.info(
            f"   Claim «{title}»: کلیک={'✅' if method else '❌'} | متن دکمه الان: «{now_text}» "
            f"| {'✅ تغییر کرد' if effect else '❓ هنوز Claim هست'}"
        )
        action_log.append({
            'action': f'READY_CLAIM_{n+1}',
            'time': datetime.now().isoformat(),
            'task': title,
            'button_text_after': now_text,
            'effect_seen': effect,
            'success': bool(method),
        })
        await save_shot(page, f'04_claim{n+1}_after')
        if effect:
            claimed += 1
        await random_delay(2, 4)

    logger.info(f"🎯 تعداد Claim های موفق: {claimed}")
    return claimed


async def process_go_tasks(context, page, action_log):
    """هر Go: کلیک ← صبر ← (اگه تغییر نکرد) شبیه‌سازی برگشت ← تبدیل به Claim ← Claim همون ردیف"""
    done_titles = set()
    for i in range(MAX_TASKS):
        target = None
        title = ''
        for b in await list_buttons(page, 'Go'):
            t = await row_title(b)
            if t not in done_titles:
                target, title = b, t
                break
        if target is None:
            break

        done_titles.add(title)
        bid = await button_id_of(target)
        logger.info(f"📋 تسک Go شماره {i+1}: «{title}» (id: {bid})")
        await save_shot(page, f'05_go{i+1}_before')
        await dump_dom(page, f'go{i+1}_before')

        links_before = len(await read_opened_links(page))
        pages_before = len(context.pages)
        method = await robust_click(target, 'Go')
        await asyncio.sleep(random.uniform(*AFTER_GO_WAIT))

        links_after = await read_opened_links(page)
        new_links = links_after[links_before:]
        opened_tab = len(context.pages) > pages_before
        logger.info(
            f"   Go «{title}»: کلیک={'✅' if method else '❌'} | لینک ثبت‌شده: {new_links} "
            f"| تب جدید: {opened_tab}"
        )
        await close_extra_tabs(context, page)
        await save_shot(page, f'05_go{i+1}_after')
        await dump_dom(page, f'go{i+1}_after')

        text_after_go = await row_button_text(page, bid)
        logger.info(f"   متن دکمه بعد از Go: «{text_after_go}»")

        # اگه بعد از چند ثانیه دکمه هنوز Go هست، برگشت از صفحه‌ی بیرونی رو شبیه‌سازی کن
        simulated = False
        if method and text_after_go == 'Go':
            await asyncio.sleep(ROW_CHECK_FIRST)
            text_wait = await row_button_text(page, bid)
            logger.info(f"   بعد از {ROW_CHECK_FIRST} ثانیه صبر، متن دکمه: «{text_wait}»")
            if text_wait == 'Go':
                logger.info("   🔄 شبیه‌سازی برگشت از صفحه‌ی بیرونی (blur/visibility/focus + رویدادهای تلگرام)")
                try:
                    await page.evaluate(SIMULATE_RETURN_JS)
                    simulated = True
                except Exception as e:
                    logger.warning(f"   ⚠️ شبیه‌سازی برگشت ناموفق: {type(e).__name__}")
                await save_shot(page, f'05_go{i+1}_after_return')

        action_log.append({
            'action': f'GO_{i+1}',
            'time': datetime.now().isoformat(),
            'task': title,
            'button_id': bid,
            'links': new_links,
            'opened_tab': opened_tab,
            'text_after_go': text_after_go,
            'simulated_return': simulated,
            'success': bool(method),
        })
        if not method:
            continue

        # صبر تا متن دکمه از Go به Claim (یا Processing) تغییر کنه
        claim_btn = None
        last_text = None
        start = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start < CLAIM_APPEAR_MAX:
            last_text = await row_button_text(page, bid)
            if last_text == 'Claim':
                claim_btn = page.locator(f'#{bid}').first
                break
            await asyncio.sleep(2)

        waited = asyncio.get_event_loop().time() - start
        if claim_btn is None:
            logger.warning(f"   ⚠️ بعد از {waited:.0f} ثانیه، Claim برای «{title}» ظاهر نشد (آخرین متن: «{last_text}»)")
            await save_shot(page, f'06_go{i+1}_no_claim')
            action_log.append({
                'action': f'GO_CLAIM_{i+1}',
                'time': datetime.now().isoformat(),
                'task': title,
                'claim_appeared': False,
                'last_text': last_text,
                'success': False,
            })
            continue

        logger.info(f"   ✅ Claim برای «{title}» بعد از {waited:.0f} ثانیه ظاهر شد")
        await random_delay(1, 3)
        await save_shot(page, f'06_go{i+1}_before_claim')
        claim_method = await robust_click(claim_btn, 'Claim')
        await asyncio.sleep(4)

        final_text = await row_button_text(page, bid)
        effect = final_text is not None and final_text != 'Claim'
        logger.info(
            f"   Claim «{title}»: کلیک={'✅' if claim_method else '❌'} | متن دکمه الان: «{final_text}» "
            f"| {'✅ Claim شد' if effect else '❓ هنوز Claim هست'}"
        )
        action_log.append({
            'action': f'GO_CLAIM_{i+1}',
            'time': datetime.now().isoformat(),
            'task': title,
            'claim_appeared': True,
            'final_text': final_text,
            'effect_seen': effect,
            'success': bool(claim_method),
        })
        await random_delay(2, 4)
        await save_shot(page, f'06_go{i+1}_after_claim')
        await dump_dom(page, f'go{i+1}_after_claim')


async def do_mine_page(page, action_log, label):
    await click_text(page, 'Mine')
    await random_delay(2, 4)
    await dump_dom(page, f'mine_{label.lower()}')
    await tap_coin(page, action_log)
    await multi_claim(page, action_log)
    await random_delay(2, 4)
    await save_shot(page, f'02_{label.lower()}')


async def do_tasks(context, page, action_log):
    ok = await click_text(page, 'Tasks')
    action_log.append({
        'action': 'TASKS_TAB',
        'time': datetime.now().isoformat(),
        'success': ok,
    })
    await random_delay(3, 6)
    await save_shot(page, '03_tasks')
    await dump_dom(page, 'tasks')
    await dump_app_functions(page)

    await claim_ready_tasks(page, action_log)
    await process_go_tasks(context, page, action_log)


async def maybe_wander(page, action_log):
    if random.random() >= WANDER_CHANCE:
        return
    tab = random.choice(WANDER_TABS)
    logger.info(f"🚶 سر زدن به تب {tab}")
    ok = await click_text(page, tab, timeout=8000)
    action_log.append({
        'action': f'WANDER_{tab}',
        'time': datetime.now().isoformat(),
        'success': ok,
    })
    await random_delay(3, 8)


async def run_miner(url, action_log):
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
        # شبیه‌سازی openLink تلگرام + ثبت رویدادهای گوش‌داده‌شده، قبل از لود هر صفحه
        await context.add_init_script(INIT_SCRIPT)
        page = await context.new_page()
        attach_diagnostics(page)

        try:
            logger.info("🌐 باز کردن وب‌اپ...")
            await page.goto(url, wait_until='networkidle', timeout=60000)
            await random_delay(3, 6)
            await save_shot(page, '01_mine')
            await dump_dom(page, 'start')

            await wait_until_online(page, action_log)
            await click_text(page, 'Mine', timeout=5000)
            await random_delay(2, 4)

            mine_first = random.random() < 0.5
            logger.info(f"🎲 ترتیب این اجرا: {'Mine ← Tasks' if mine_first else 'Tasks ← Mine'}")

            if mine_first:
                await do_mine_page(page, action_log, 'MINE_1')
                await maybe_wander(page, action_log)
                await do_tasks(context, page, action_log)
                if random.random() < FINAL_MINE_CLAIM_CHANCE:
                    await do_mine_page(page, action_log, 'MINE_2')
            else:
                await do_tasks(context, page, action_log)
                await maybe_wander(page, action_log)
                await do_mine_page(page, action_log, 'MINE_1')

            await save_shot(page, '07_final')

        finally:
            await context.close()
            await browser.close()


async def run_bot():
    logger.info("🚀 شروع ربات...")

    if FORCE_RUN:
        logger.info("⚡ اجرای دستی: زمان‌بندی نادیده گرفته شد")
    elif not is_due():
        return

    action_log = []
    success = False

    try:
        await client.connect()

        if not await client.is_user_authorized():
            logger.error("❌ Session معتبر نیست!")
            if SESSION_STRING:
                logger.info("💡 مقدار secret به اسم TELEGRAM_SESSION باطل یا ناقصه؛ با همون API_ID/API_HASH دوباره بسازش")
            else:
                logger.info("💡 secret به اسم TELEGRAM_SESSION به ربات نرسیده یا خالیه، یا فایل user_session.session لاگین نیست")
            return

        logger.info("✅ متصل شد به تلگرام")

        if not FORCE_RUN:
            await random_delay(*START_JITTER)

        webapp_url = await get_webapp_url()
        logger.info("🔗 URL وب‌اپ گرفته شد")

        await run_miner(webapp_url, action_log)

        logger.info("✅ چرخه کامل انجام شد")
        success = True

    except SessionPasswordNeededError:
        logger.error("❌ نیاز به رمز عبور دو مرحله‌ای!")

    except Exception as e:
        logger.error(f"❌ خطای کلی: {type(e).__name__}: {e}")

    finally:
        if success:
            schedule_next_run()
        else:
            schedule_retry()

        try:
            with open('action_log.json', 'w') as f:
                json.dump(action_log, f, indent=2)
        except Exception:
            pass
        await client.disconnect()
        logger.info("✅ قطع اتصال")


if __name__ == "__main__":
    asyncio.run(run_bot())
