import os
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
SCREENSHOT_DIR = 'screenshots'

# --- زمان‌بندی: تقریباً هر ساعت ---
STATE_FILE = 'schedule_state.json'
MIN_GAP_MIN = 55    # حداقل فاصله بین اجراها (دقیقه)
MAX_GAP_MIN = 65    # حداکثر فاصله (دقیقه)

# فاصله‌ی تلاش مجدد بعد از شکست (دقیقه)
RETRY_GAP_MIN = (20, 40)

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


async def click_text(page, text, exact=True, timeout=15000):
    """کلیک روی المانی که متنش مطابقه"""
    try:
        locator = page.get_by_text(text, exact=exact).first
        await locator.wait_for(state='visible', timeout=timeout)
        await locator.click(timeout=timeout)
        logger.info(f"✅ کلیک شد: {text}")
        return True
    except Exception as e:
        logger.warning(f"⚠️ کلیک روی '{text}' ناموفق: {type(e).__name__}")
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
    if page.url and 'atfminers.asloni.online' not in page.url:
        try:
            await page.go_back(timeout=15000)
            logger.info("↩️ برگشت به اپ")
        except Exception:
            pass


async def click_visible(page, text, randomize=False):
    """کلیک روی یکی از دکمه‌های قابل‌دیدن و فعال با متن مشخص (اولی، یا رندم)"""
    try:
        items = page.get_by_text(text, exact=True)
        count = await items.count()
        indexes = list(range(count))
        if randomize:
            random.shuffle(indexes)
        for idx in indexes:
            btn = items.nth(idx)
            try:
                if not await btn.is_visible():
                    continue
                if not await btn.is_enabled():
                    continue
                await btn.click(timeout=5000)
                logger.info(f"✅ کلیک شد: {text} (شماره {idx+1} از {count})")
                return True
            except Exception:
                continue
    except Exception as e:
        logger.warning(f"⚠️ کلیک '{text}' ناموفق: {type(e).__name__}")
    return False


# ---------- صفحه‌ی اول: دایره‌ی ATF و CLAIM ----------

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

    # فقط اونایی که تقریباً وسط افقی صفحه و توی بخش میانی عمودی هستن
    good = [
        c for c in candidates
        if abs(c['x'] - vw / 2) < vw * 0.15 and vh * 0.30 < c['y'] < vh * 0.80
    ]
    if good:
        # اولویت با img و canvas، بعد نزدیک‌ترین به مرکز افقی
        good.sort(key=lambda c: (c['tag'] not in ('IMG', 'CANVAS'), abs(c['x'] - vw / 2)))
        c = good[0]
        logger.info(f"🪙 دایره پیدا شد ({c['tag']}, عرض {c['w']:.0f}) در ({c['x']:.0f}, {c['y']:.0f})")
        return c['x'], c['y'], min(c['w'], 300)

    # روش جایگزین: نقطه‌ی تقریبی وسط صفحه
    fx, fy = vw * 0.5, vh * 0.52
    logger.warning(f"⚠️ دایره پیدا نشد، استفاده از نقطه‌ی تقریبی ({fx:.0f}, {fy:.0f})")
    return fx, fy, 200


async def tap_coin(page, action_log):
    """چند بار تپ روی دایره‌ی ATF"""
    x, y, size = await find_coin_center(page)
    taps = random.randint(*COIN_TAPS)
    spread = max(10, size * 0.12)
    logger.info(f"👆 شروع {taps} تپ روی دایره‌ی ATF")

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

    logger.info(f"🎯 تعداد تپ انجام‌شده روی دایره: {done} از {taps}")
    action_log.append({
        'action': 'COIN_TAPS',
        'time': datetime.now().isoformat(),
        'count': done,
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
        ok = await click_text(page, 'CLAIM', timeout=5000)
        if ok:
            done += 1
        await random_delay(*CLAIM_CLICK_DELAY)

    logger.info(f"🎯 تعداد کلیک CLAIM: {done} از {clicks}")
    action_log.append({
        'action': 'MINE_CLAIMS',
        'time': datetime.now().isoformat(),
        'count': done,
        'success': done > 0,
    })
    return done


async def claim_ready_tasks(page, action_log):
    """کلیک روی همه‌ی Claim های آماده‌ی تسک‌ها (تسک‌هایی که Go شون قبلاً خورده)"""
    claimed = 0
    for n in range(MAX_TASKS):
        count = await page.get_by_text('Claim', exact=True).count()
        logger.info(f"🔎 تعداد دکمه‌ی Claim تسک: {count}")
        if count == 0:
            break

        ok = await click_visible(page, 'Claim', randomize=True)
        action_log.append({
            'action': f'READY_CLAIM_{n+1}',
            'time': datetime.now().isoformat(),
            'success': ok,
        })
        if not ok:
            break

        claimed += 1
        await random_delay(3, 5)
        await save_shot(page, f'04_ready_claim_{n+1}')

    logger.info(f"🎯 تعداد Claim های انجام‌شده: {claimed}")
    return claimed


async def process_go_tasks(context, page, action_log):
    """تسک‌هایی که هنوز Go دارن: Go ← برگشت ← صبر ← Claim"""
    go_count = await page.get_by_text('Go', exact=True).count()
    logger.info(f"📋 تعداد دکمه‌ی Go: {go_count}")

    for i in range(min(go_count, MAX_TASKS)):
        logger.info(f"📋 پردازش تسک Go شماره {i+1}")

        # الف) کلیک روی یکی از Go ها (رندم)
        go_ok = await click_visible(page, 'Go', randomize=True)
        action_log.append({
            'action': f'GO_{i+1}',
            'time': datetime.now().isoformat(),
            'success': go_ok,
        })
        if not go_ok:
            break

        # ب) چند ثانیه روی صفحه‌ی خارجی، بعد بستن و برگشت
        await random_delay(4, 7)
        await close_extra_tabs(context, page)

        # پ) صبر تا دکمه‌ی Claim تسک فعال بشه
        await random_delay(*CLAIM_WAIT)
        await save_shot(page, f'05_go{i+1}_before_claim')

        # ت) کلیک روی Claim تسک
        claim_ok = await click_visible(page, 'Claim', randomize=True)
        action_log.append({
            'action': f'GO_CLAIM_{i+1}',
            'time': datetime.now().isoformat(),
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
