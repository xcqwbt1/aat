import os
import re
import asyncio
import logging
import random
import json
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs
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
ONLINE_WAIT_MAX = 180         # صبر برای ONLINE شدن اول کار (ثانیه)
ONLINE_EXIT_WAIT = 150        # صبر برای ONLINE بودن قبل از خروج (ثانیه)
RETRY_IF_OFFLINE = True       # اگه موقع خروج ONLINE نبود، اجرای بعدی زودتر (۲۰ تا ۴۰ دقیقه)
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

# --- تسک‌ها: (id دکمه بدون btn-، برچسب، حداقل ثانیه‌ی Processing طبق کد اپ) ---
TASKS = [
    ('youtube_like_comment', 'YouTube Like & Comment', 30),
    ('twitter_retweet', 'X Retweet', 30),
    ('website_visit', 'Visit Website', 10),
    ('telegram_react_latest', 'React to latest post', 20),
    ('bio_referral', 'Keep ATF in Bio', 0),
    ('name_eye', 'Keep eye in Name', 0),
]
GO_SETTLE_WAIT = 6        # صبر بعد از تپ Go تا متن دکمه عوض بشه (ثانیه)
PROCESSING_MARGIN = 15    # حاشیه‌ی اضافه روی minSeconds برای ظاهر شدن Claim (ثانیه)
CLAIM_RESULT_WAIT = 30    # حداکثر صبر برای نتیجه‌ی Claim (ثانیه)

# درخواست‌های سرور که لاگ می‌شن
TASK_ACTIONS = {'start_task', 'claim_task', 'bio_task_claim', 'name_eye_task_claim'}

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


# اسکریپتی که قبل از لود اپ تزریق می‌شه: openLink و window.open رو می‌گیره (لینک بیرونی باز نشه)
INIT_SCRIPT = """
(() => {
  window.__openedLinks = [];
  const record = (u) => { try { window.__openedLinks.push(String(u)); } catch (e) {} };

  window.open = function (u) { record(u); return null; };

  const patchWebApp = () => {
    const tg = window.Telegram && window.Telegram.WebApp;
    if (!tg || tg.__patched) return;
    tg.__patched = true;
    try { tg.openLink = function (u) { record(u); }; } catch (e) {}
    try { tg.openTelegramLink = function (u) { record(u); }; } catch (e) {}
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


def redact(s):
    return re.sub(r'[A-Za-z0-9_\-]{40,}', '<redacted>', s or '')


def attach_diagnostics(page):
    """لاگ خطاهای صفحه و درخواست‌های تسک (اکشن + status + خلاصه‌ی کوتاه جواب)"""

    def action_of(url):
        try:
            return parse_qs(urlparse(url).query).get('action', ['-'])[0]
        except Exception:
            return '-'

    async def on_response(resp):
        try:
            if 'index.php' not in resp.url:
                return
            action = action_of(resp.url)
            if action not in TASK_ACTIONS:
                return
            text = await resp.text()
            logger.info(f"📡 {action} → {resp.status} | {redact(text)[:180]}")
        except Exception:
            pass

    page.on('response', lambda r: asyncio.ensure_future(on_response(r)))
    page.on('pageerror', lambda e: logger.info(f"💥 pageerror: {redact(str(e))[:160]}"))


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


def schedule_retry(reason='retry'):
    save_next_run(random.randint(*RETRY_GAP_MIN), reason)


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


# ---------- کلیک ----------

async def robust_click(locator, label=''):
    """کلیک با سه روش پشت‌سرهم: عادی، با مختصات، dispatch_event (برای تب‌ها و Mine)"""
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


async def tap_locator(page, locator, label=''):
    """تپ لمسی وسط المان (اپ به کلیک ماوس روی دکمه‌های تسک واکنش نشون نمی‌ده)"""
    try:
        await locator.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    try:
        box = await locator.bounding_box()
        if box:
            x = box['x'] + box['width'] / 2 + random.uniform(-box['width'] * 0.2, box['width'] * 0.2)
            y = box['y'] + box['height'] / 2 + random.uniform(-box['height'] * 0.2, box['height'] * 0.2)
            await page.touchscreen.tap(x, y)
            logger.info(f"   👆 [{label}] تپ لمسی ({x:.0f}, {y:.0f})")
            return True
    except Exception as e:
        logger.info(f"   ↳ [{label}] تپ لمسی ناموفق ({type(e).__name__})")
    try:
        await locator.click(timeout=4000)
        logger.info(f"   ↳ [{label}] کلیک عادی (جایگزین)")
        return True
    except Exception as e:
        logger.info(f"   ↳ [{label}] کلیک جایگزین هم ناموفق ({type(e).__name__})")
    return False


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


# ---------- آنلاین شدن ----------

async def read_status(page):
    """متن وضعیت اپ (ONLINE یا هر چیز دیگه) از المنت #miningStatusHeader؛ None اگه پیدا نشد"""
    try:
        loc = page.locator('#miningStatusHeader')
        if await loc.count() == 0:
            return None
        text = (await loc.first.text_content(timeout=2000)) or ''
        text = text.strip().upper()
        return text or None
    except Exception:
        return None


async def is_online(page):
    status = await read_status(page)
    if status is not None:
        return status == 'ONLINE'
    try:
        return await page.get_by_text('ONLINE', exact=True).first.is_visible(timeout=1000)
    except Exception:
        return False


async def browse_for_online(page, max_seconds, label):
    """گشت بین تب‌ها و برگشت به Mine تا وضعیت ONLINE بشه. True اگه ONLINE شد."""
    start = asyncio.get_event_loop().time()
    tabs = WARMUP_TABS[:]
    random.shuffle(tabs)
    tab_idx = 0

    while True:
        if await is_online(page):
            return True
        if asyncio.get_event_loop().time() - start >= max_seconds:
            return False

        status = await read_status(page)
        logger.info(f"🕐 [{label}] هنوز ONLINE نیست (وضعیت: {status})، گشت کوتاه...")
        tab = tabs[tab_idx % len(tabs)]
        tab_idx += 1
        await click_text(page, tab, timeout=5000)
        await asyncio.sleep(random.uniform(12, 25))
        await click_text(page, 'Mine', timeout=5000)
        await asyncio.sleep(random.uniform(8, 15))


async def wait_until_online(page, action_log):
    """اول کار: صبر تا ONLINE بشه"""
    if await is_online(page):
        logger.info("🟢 اپ از قبل ONLINE است")
        return True

    logger.info("🕐 اپ هنوز ONLINE نیست، شروع گشت اولیه...")
    start = asyncio.get_event_loop().time()
    ok = await browse_for_online(page, ONLINE_WAIT_MAX, 'شروع')
    elapsed = asyncio.get_event_loop().time() - start

    if ok:
        logger.info(f"🟢 اپ ONLINE شد (بعد از {elapsed:.0f} ثانیه)")
    else:
        logger.warning("⚠️ تا سقف زمان، ONLINE ظاهر نشد؛ ادامه با هشدار")
        await save_shot(page, '01b_not_online')

    action_log.append({
        'action': 'WAIT_ONLINE',
        'time': datetime.now().isoformat(),
        'seconds': round(elapsed),
        'success': ok,
    })
    return ok


async def ensure_online_before_exit(page, action_log):
    """آخر کار: مطمئن شو اپ ONLINE هست، بعد خارج شو. True اگه ONLINE تایید شد."""
    logger.info("🔎 بررسی نهایی وضعیت قبل از خروج...")
    await click_text(page, 'Mine', timeout=5000)
    await asyncio.sleep(2)

    start = asyncio.get_event_loop().time()
    ok = await browse_for_online(page, ONLINE_EXIT_WAIT, 'خروج')
    elapsed = asyncio.get_event_loop().time() - start
    status = await read_status(page)

    if ok:
        logger.info(f"🟢 قبل از خروج، وضعیت ONLINE تایید شد (وضعیت: {status})")
        await asyncio.sleep(random.uniform(4, 8))
        await save_shot(page, '07_final')
    else:
        logger.warning(f"⚠️ قبل از خروج هم ONLINE نشد (وضعیت: {status})")
        await save_shot(page, '07_final_not_online')

    action_log.append({
        'action': 'FINAL_ONLINE_CHECK',
        'time': datetime.now().isoformat(),
        'status': status,
        'seconds': round(elapsed),
        'success': ok,
    })
    return ok


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


# ---------- تسک‌ها (با تپ لمسی و id ثابت دکمه‌ها) ----------

def task_button(page, task_id):
    return page.locator(f'#btn-{task_id}').first


async def task_text(page, task_id):
    """متن فعلی دکمه‌ی تسک؛ None اگه دکمه توی صفحه نیست"""
    try:
        if await page.locator(f'#btn-{task_id}').count() == 0:
            return None
        return (await task_button(page, task_id).inner_text(timeout=2000)).strip()
    except Exception:
        return None


async def wait_text_change(page, task_id, old_text, timeout):
    """صبر تا متن دکمه از old_text عوض بشه (یا دکمه ناپدید بشه)"""
    end = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < end:
        now = await task_text(page, task_id)
        if now != old_text:
            return now
        await asyncio.sleep(1)
    return await task_text(page, task_id)


def is_finished_text(text):
    """بعد از Claim موفق: دکمه به تایمر (Next in ...) یا Done تبدیل می‌شه یا ناپدید می‌شه"""
    return text is None or text.startswith('Next in') or text == 'Done'


async def claim_task_button(page, task_id, label, action_log):
    """تپ روی Claim یک تسک و صبر تا نتیجه (Processing ← Next in ...)"""
    logger.info(f"💰 Claim تسک: {label}")
    ok = await tap_locator(page, task_button(page, task_id), f'Claim {task_id}')
    if not ok:
        action_log.append({
            'action': f'CLAIM_{task_id}',
            'time': datetime.now().isoformat(),
            'success': False,
        })
        return False

    end = asyncio.get_event_loop().time() + CLAIM_RESULT_WAIT
    final = await task_text(page, task_id)
    while asyncio.get_event_loop().time() < end:
        if is_finished_text(final):
            break
        await asyncio.sleep(1.5)
        final = await task_text(page, task_id)

    success = is_finished_text(final)
    logger.info(f"   {'✅ Claim شد' if success else '❓ نتیجه نامشخص'} | متن دکمه الان: «{final}»")
    action_log.append({
        'action': f'CLAIM_{task_id}',
        'time': datetime.now().isoformat(),
        'final_text': final,
        'success': success,
    })
    await save_shot(page, f'05_claim_{task_id}')
    return success


async def do_tasks(page, action_log):
    ok = await click_text(page, 'Tasks')
    action_log.append({
        'action': 'TASKS_TAB',
        'time': datetime.now().isoformat(),
        'success': ok,
    })
    await random_delay(3, 6)
    await save_shot(page, '03_tasks')

    # وضعیت فعلی همه‌ی تسک‌ها
    states = {}
    for task_id, label, _ in TASKS:
        states[task_id] = await task_text(page, task_id)
    logger.info(f"📋 وضعیت تسک‌ها: {states}")

    # ۱) Claim های آماده
    ready = [t for t in TASKS if states.get(t[0]) == 'Claim']
    random.shuffle(ready)
    for task_id, label, _ in ready:
        await claim_task_button(page, task_id, label, action_log)
        await random_delay(2, 4)

    # ۲) Go های موجود
    go_tasks = [t for t in TASKS if states.get(t[0]) == 'Go']
    random.shuffle(go_tasks)
    started = []
    for task_id, label, min_sec in go_tasks:
        logger.info(f"➡️ Go تسک: {label}")
        await tap_locator(page, task_button(page, task_id), f'Go {task_id}')
        new_text = await wait_text_change(page, task_id, 'Go', GO_SETTLE_WAIT)

        if new_text == 'Go':
            logger.info("   ❓ متن عوض نشد، یک بار دیگه تپ می‌زنم")
            await asyncio.sleep(random.uniform(1, 3))
            await tap_locator(page, task_button(page, task_id), f'Go {task_id} (تلاش دوم)')
            new_text = await wait_text_change(page, task_id, 'Go', GO_SETTLE_WAIT)

        links = []
        try:
            links = await page.evaluate(
                "() => (window.__openedLinks || []).map(u => { try { return new URL(u).host } catch (e) { return String(u).slice(0, 40) } })"
            )
        except Exception:
            pass
        success = new_text in ('Processing', 'Claim')
        logger.info(f"   {'✅' if success else '❌'} متن دکمه بعد از Go: «{new_text}» | لینک‌های ثبت‌شده: {links}")
        action_log.append({
            'action': f'GO_{task_id}',
            'time': datetime.now().isoformat(),
            'text_after': new_text,
            'success': success,
        })
        if success:
            started.append((task_id, label, min_sec))
        await random_delay(2, 5)

    # ۳) صبر تا Processing تموم بشه و اپ خودش دکمه رو به Claim تبدیل کنه
    if started:
        wait_total = max(m for _, _, m in started) + PROCESSING_MARGIN
        logger.info(f"⏳ صبر تا {wait_total} ثانیه برای تبدیل Processing به Claim...")
        end = asyncio.get_event_loop().time() + wait_total
        while asyncio.get_event_loop().time() < end:
            texts = [await task_text(page, tid) for tid, _, _ in started]
            if all(t == 'Claim' for t in texts):
                break
            await asyncio.sleep(2)
        await save_shot(page, '04_before_task_claims')

        # ۴) Claim همون تسک‌ها
        random.shuffle(started)
        for task_id, label, _ in started:
            text = await task_text(page, task_id)
            if text == 'Claim':
                await claim_task_button(page, task_id, label, action_log)
                await random_delay(2, 4)
            else:
                logger.warning(f"⚠️ «{label}» آماده‌ی Claim نشد (متن: «{text}»)")
                action_log.append({
                    'action': f'CLAIM_{task_id}',
                    'time': datetime.now().isoformat(),
                    'final_text': text,
                    'success': False,
                })

    await save_shot(page, '06_tasks_done')


async def do_mine_page(page, action_log, label):
    await click_text(page, 'Mine')
    await random_delay(2, 4)
    await tap_coin(page, action_log)
    await multi_claim(page, action_log)
    await random_delay(2, 4)
    await save_shot(page, f'02_{label.lower()}')


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
    """اجرای کامل داخل اپ. True برمی‌گردونه اگه موقع خروج ONLINE تایید شد."""
    online_ok = False
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
        # جلوگیری از باز شدن لینک‌های بیرونی (اپ لینک رو ثبت‌شده می‌بینه)
        await context.add_init_script(INIT_SCRIPT)
        page = await context.new_page()
        attach_diagnostics(page)

        try:
            logger.info("🌐 باز کردن وب‌اپ...")
            await page.goto(url, wait_until='networkidle', timeout=60000)
            await random_delay(3, 6)
            await save_shot(page, '01_mine')

            await wait_until_online(page, action_log)
            await click_text(page, 'Mine', timeout=5000)
            await random_delay(2, 4)

            mine_first = random.random() < 0.5
            logger.info(f"🎲 ترتیب این اجرا: {'Mine ← Tasks' if mine_first else 'Tasks ← Mine'}")

            if mine_first:
                await do_mine_page(page, action_log, 'MINE_1')
                await maybe_wander(page, action_log)
                await do_tasks(page, action_log)
                if random.random() < FINAL_MINE_CLAIM_CHANCE:
                    await do_mine_page(page, action_log, 'MINE_2')
            else:
                await do_tasks(page, action_log)
                await maybe_wander(page, action_log)
                await do_mine_page(page, action_log, 'MINE_1')

            # آخر کار: مطمئن شو ONLINE هست، بعد خارج شو
            online_ok = await ensure_online_before_exit(page, action_log)

        finally:
            await context.close()
            await browser.close()

    return online_ok


async def run_bot():
    logger.info("🚀 شروع ربات...")

    if FORCE_RUN:
        logger.info("⚡ اجرای دستی: زمان‌بندی نادیده گرفته شد")
    elif not is_due():
        return

    action_log = []
    success = False
    online_ok = True

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

        online_ok = await run_miner(webapp_url, action_log)

        logger.info("✅ چرخه کامل انجام شد")
        success = True

    except SessionPasswordNeededError:
        logger.error("❌ نیاز به رمز عبور دو مرحله‌ای!")

    except Exception as e:
        logger.error(f"❌ خطای کلی: {type(e).__name__}: {e}")

    finally:
        if success and (online_ok or not RETRY_IF_OFFLINE):
            schedule_next_run()
        elif success:
            logger.warning("⚠️ موقع خروج ONLINE نبود؛ اجرای بعدی زودتر زمان‌بندی می‌شه")
            schedule_retry('offline_retry')
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
