import os
import re
import asyncio
import logging
import random
import json
from datetime import datetime, timedelta
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
MIN_GAP_MIN = 55    # حداقل فاصله بین اجراها (دقیقه)
MAX_GAP_MIN = 65    # حداکثر فاصله (دقیقه)

# فاصله‌ی تلاش مجدد بعد از شکست (دقیقه)
RETRY_GAP_MIN = (20, 40)

# --- آنلاین شدن اپ ---
ONLINE_WAIT_MAX = 180         # حداکثر صبر برای ظاهر شدن ONLINE (ثانیه)
WARMUP_TABS = ['Miners', 'Friends', 'Profile']  # تب‌هایی که توی گشت اولیه می‌گرده

# --- صفحه‌ی اول (Mine) ---
COIN_TAPS = (8, 15)           # تعداد تپ روی دایره‌ی ATF در هر اجرا
COIN_TAP_DELAY = (0.3, 1.2)   # فاصله‌ی بین تپ‌ها (ثانیه)
CLAIM_CLICKS = (3, 5)         # تعداد کلیک روی CLAIM صفحه‌ی اول در هر اجرا
CLAIM_CLICK_DELAY = (2, 5)    # فاصله‌ی بین کلیک‌های CLAIM (ثانیه)

# --- رفتار نامنظم داخل اپ ---
START_JITTER = (0, 90)        # تاخیر رندم قبل از شروع (ثانیه)
WANDER_CHANCE = 0.30          # احتمال سر زدن به یه تب اضافه
EXTRA_PAUSE_CHANCE = 0.15     # احتمال مکث اضافه بین کارها
FINAL_MINE_CLAIM_CHANCE = 1.0 # احتمال Mine نهایی بعد از Tasks (وقتی اول Mine رو زده)
WANDER_TABS = ['Miners', 'Friends', 'Profile']

# صبر بعد از Go تا دکمه‌ی Claim تسک فعال بشه (ثانیه، بازه‌ی تصادفی)
CLAIM_WAIT = (12, 18)

# حداکثر تعداد تسک‌ها برای پردازش
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


# ---------- زمان‌بندی ----------

def load_next_run():
    """خواندن زمان اجرای بعدی از فایل وضعیت (None اگه نبود یا خراب بود)"""
    try:
        with open(STATE_FILE, 'r') as f:
            data = json.load(f)
        return datetime.fromisoformat(data['next_run'])
    except Exception:
        return None


def save_next_run(gap, reason):
    """ذخیره‌ی زمان اجرای بعدی بعد از gap دقیقه"""
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
    """بعد از اجرای موفق: تقریباً یک ساعت بعد"""
    save_next_run(random.randint(MIN_GAP_MIN, MAX_GAP_MIN), 'success')


def schedule_retry():
    """بعد از شکست: تلاش مجدد با فاصله‌ی کوتاه‌تر (ولی نه هر ۱۰ دقیقه)"""
    save_next_run(random.randint(*RETRY_GAP_MIN), 'retry')


def is_due():
    """آیا وقت اجرای بعدی رسیده؟"""
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
    """تاخیر تصادفی مثل انسان (گاهی با مکث اضافه)"""
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

    # پیدا کردن دکمه‌ی وب‌اپ از پیام /start
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
    """ذخیره‌ی اسکرین‌شات برای دیباگ"""
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

    # روش ۱: کلیک عادی
    try:
        await locator.click(timeout=4000)
        logger.info(f"   ↳ [{label}] کلیک عادی انجام شد")
        return 'normal'
    except Exception as e:
        logger.info(f"   ↳ [{label}] کلیک عادی ناموفق ({type(e).__name__})")

    # روش ۲: کلیک با مختصات وسط المان
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

    # روش ۳: dispatch_event
    try:
        await locator.dispatch_event('click')
        logger.info(f"   ↳ [{label}] dispatch_event انجام شد")
        return 'dispatch'
    except Exception as e:
        logger.info(f"   ↳ [{label}] dispatch_event ناموفق ({type(e).__name__})")

    return None


def clickable_locator(page, text):
    """دکمه‌های کلیک‌پذیر با متن دقیق؛ اگه نبود get_by_text"""
    pattern = re.compile(rf'^\s*{re.escape(text)}\s*$')
    primary = page.locator('button, [role="button"], a').filter(has_text=pattern)
    return primary


async def list_buttons(page, text):
    """لیست دکمه‌های قابل‌دیدن و فعال با متن مشخص (ابتدا کلیک‌پذیرها، بعد get_by_text)"""
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
    """کلیک روی اولین دکمه‌ی قابل‌دیدن با متن مشخص (با انتظار)"""
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
    """کلیک روی یکی از دکمه‌های قابل‌دیدن (اولی، یا رندم)"""
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
    """بستن تب‌های خارجی (یوتیوب، X، سایت) و برگشت به اپ"""
    for extra in list(context.pages):
        if extra != page:
            try:
                await extra.close()
                logger.info("🗙 تب خارجی بسته شد")
            except Exception:
                pass

    # اگه صفحه‌ی خود اپ به جای دیگه رفته، برگرد
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
    """گشت بین تب‌ها تا وضعیت ONLINE ظاهر بشه (حداکثر ONLINE_WAIT_MAX ثانیه)"""
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
    """خواندن عدد +x.xxxx بالای دایره (برای تایید اثر تپ و Claim)"""
    try:
        body = await page.inner_text('body', timeout=3000)
        m = re.search(r'\+\s*(\d+(?:\.\d+)?)', body)
        if m:
            return float(m.group(1))
    except Exception:
        pass
    return None


async def find_coin_center(page):
    """پیدا کردن مرکز دایره‌ی ATF (المان تقریباً مربعی و بزرگ نزدیک مرکز صفحه)"""
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
    """چند بار تپ روی دایره‌ی ATF و ثبت تغییر عدد بالای دایره"""
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
    """چند بار کلیک روی CLAIM صفحه‌ی اول"""
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


# ---------- تسک‌ها ----------

async def claim_ready_tasks(page, action_log):
    """کلیک روی همه‌ی Claim های آماده‌ی تسک‌ها با تایید اثر (کم شدن تعداد Claim)"""
    claimed = 0
    for n in range(MAX_TASKS):
        before = await count_buttons(page, 'Claim')
        logger.info(f"🔎 تعداد دکمه‌ی Claim تسک (قابل‌کلیک): {before}")
        if before == 0:
            break

        await save_shot(page, f'04_claim{n+1}_before')
        ok = await click_visible(page, 'Claim', randomize=False)
        await asyncio.sleep(3)
        after = await count_buttons(page, 'Claim')
        effect = after < before
        logger.info(
            f"   Claim {n+1}: کلیک={'✅' if ok else '❌'} | تعداد قبل: {before} ← بعد: {after} "
            f"| {'✅ اثر داشت' if effect else '❓ اثری دیده نشد'}"
        )
        action_log.append({
            'action': f'READY_CLAIM_{n+1}',
            'time': datetime.now().isoformat(),
            'before': before,
            'after': after,
            'effect_seen': effect,
            'success': ok,
        })
        await save_shot(page, f'04_claim{n+1}_after')

        if not ok:
            break
        if effect:
            claimed += 1
        else:
            # اثر نداشت؛ احتمالاً دکمه‌ی دیگه‌ای هست یا اپ قبول نکرده. یه بار دیگه سعی نکن.
            break

        await random_delay(3, 5)

    logger.info(f"🎯 تعداد Claim های موفق (با اثر): {claimed}")
    return claimed


async def process_go_tasks(context, page, action_log):
    """تسک‌هایی که هنوز Go دارن: Go ← بررسی تب جدید ← برگشت ← صبر ← Claim"""
    go_count = await count_buttons(page, 'Go')
    logger.info(f"📋 تعداد دکمه‌ی Go (قابل‌کلیک): {go_count}")

    for i in range(min(go_count, MAX_TASKS)):
        logger.info(f"📋 پردازش تسک Go شماره {i+1}")
        pages_before = len(context.pages)
        url_before = page.url
        await save_shot(page, f'05_go{i+1}_before')

        go_ok = await click_visible(page, 'Go', randomize=True)
        await asyncio.sleep(3)

        opened_tab = len(context.pages) > pages_before
        url_changed = page.url != url_before
        effect = opened_tab or url_changed
        logger.info(
            f"   Go {i+1}: کلیک={'✅' if go_ok else '❌'} | تب جدید: {opened_tab} "
            f"| تغییر آدرس: {url_changed} | {'✅ اثر داشت' if effect else '❓ اثری دیده نشد'}"
        )
        action_log.append({
            'action': f'GO_{i+1}',
            'time': datetime.now().isoformat(),
            'opened_tab': opened_tab,
            'url_changed': url_changed,
            'success': go_ok,
        })
        if not go_ok:
            break

        # چند ثانیه روی صفحه‌ی خارجی، بعد بستن و برگشت
        await random_delay(4, 7)
        await close_extra_tabs(context, page)

        # صبر تا دکمه‌ی Claim تسک فعال بشه
        await random_delay(*CLAIM_WAIT)
        await save_shot(page, f'05_go{i+1}_before_claim')

        claims_before = await count_buttons(page, 'Claim')
        claim_ok = await click_visible(page, 'Claim', randomize=False)
        await asyncio.sleep(3)
        claims_after = await count_buttons(page, 'Claim')
        claim_effect = claims_after < claims_before
        logger.info(
            f"   Claim بعد از Go {i+1}: کلیک={'✅' if claim_ok else '❌'} "
            f"| قبل: {claims_before} ← بعد: {claims_after} "
            f"| {'✅ اثر داشت' if claim_effect else '❓ اثری دیده نشد'}"
        )
        action_log.append({
            'action': f'GO_CLAIM_{i+1}',
            'time': datetime.now().isoformat(),
            'before': claims_before,
            'after': claims_after,
            'effect_seen': claim_effect,
            'success': claim_ok,
        })

        await random_delay(2, 4)
        await save_shot(page, f'06_go{i+1}_after_claim')


async def do_mine_page(page, action_log, label):
    """رفتن به تب Mine، چند تپ روی دایره‌ی ATF و چند کلیک روی CLAIM"""
    await click_text(page, 'Mine')
    await random_delay(2, 4)
    await tap_coin(page, action_log)
    await multi_claim(page, action_log)
    await random_delay(2, 4)
    await save_shot(page, f'02_{label.lower()}')


async def do_tasks(context, page, action_log):
    """رفتن به تب Tasks و انجام Claim ها و Go ها"""
    ok = await click_text(page, 'Tasks')
    action_log.append({
        'action': 'TASKS_TAB',
        'time': datetime.now().isoformat(),
        'success': ok,
    })
    await random_delay(3, 6)
    await save_shot(page, '03_tasks')

    # اول Claim های آماده، بعد تسک‌هایی که هنوز Go دارن
    await claim_ready_tasks(page, action_log)
    await process_go_tasks(context, page, action_log)


async def maybe_wander(page, action_log):
    """گاهی یه تب اضافه رو باز کن و برگرد (رفتار طبیعی‌تر)"""
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
    """باز کردن وب‌اپ با مرورگر و انجام Mine و Tasks (با ترتیب رندم)"""
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
        page = await context.new_page()

        try:
            logger.info("🌐 باز کردن وب‌اپ...")
            await page.goto(url, wait_until='networkidle', timeout=60000)
            await random_delay(3, 6)
            await save_shot(page, '01_mine')

            # صبر تا اپ ONLINE بشه (گشت اولیه)
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
    """تابع اصلی اجرای ربات"""
    logger.info("🚀 شروع ربات...")

    # بررسی زمان‌بندی قبل از هر کاری (بدون اتصال به تلگرام)
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

        # تاخیر رندم قبل از شروع (توی اجرای دستی رد می‌شه)
        if not FORCE_RUN:
            await random_delay(*START_JITTER)

        # گرفتن URL احراز هویت‌شده
        webapp_url = await get_webapp_url()
        logger.info("🔗 URL وب‌اپ گرفته شد")

        # اجرای مرورگر
        await run_miner(webapp_url, action_log)

        logger.info("✅ چرخه کامل انجام شد")
        success = True

    except SessionPasswordNeededError:
        logger.error("❌ نیاز به رمز عبور دو مرحله‌ای!")

    except Exception as e:
        logger.error(f"❌ خطای کلی: {type(e).__name__}: {e}")

    finally:
        # همیشه یه زمان بعدی ذخیره کن تا فایل وضعیت ساخته بشه و تلاش پشت‌سرهم نشه
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
