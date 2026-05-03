import asyncio
import json
import os
import re
import tempfile
from datetime import datetime

import requests
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument

# ======================== خواندن تنظیمات از environment variables ========================
STRING_SESSION = os.environ.get("STRING_SESSION", "").strip()
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "").strip()

# لیست کانال‌های مبدأ (به صورت JSON آرایه‌ای از رشته‌ها)
SOURCE_CHANNELS_JSON = os.environ.get("SOURCE_CHANNELS", "[]")
try:
    SOURCE_CHANNELS = json.loads(SOURCE_CHANNELS_JSON)
except json.JSONDecodeError:
    SOURCE_CHANNELS = []
    print("⚠️ SOURCE_CHANNELS معتبر نیست. از مقدار پیش‌فرض [] استفاده می‌شود.")

# تنظیمات پیام‌رسان بله
BALE_BOT_TOKEN = os.environ.get("BALE_BOT_TOKEN", "").strip()
BALE_CHANNEL_ID = int(os.environ.get("BALE_CHANNEL_ID", 0))

# فایل ذخیره آخرین شناسه پیام‌های پردازش شده
STATE_FILE = "state.json"

# تأخیر بین ارسال پیام‌ها به بله (برای جلوگیری از محدودیت نرخ)
SLEEP_BETWEEN_MESSAGES = 1  # ثانیه

# ====================================================================

def clean_telegram_mentions(text: str) -> str:
    """حذف شناسه‌های تلگرامی (@username، لینک‌های t.me، شناسه‌های عددی) از متن"""
    if not text:
        return ""
    # حذف @username
    text = re.sub(r'@[\w_]+', '', text)
    # حذف لینک‌های t.me
    text = re.sub(r'https?://t\.me/\S+', '', text)
    # حذف شناسه‌های عددی داخل کروشه یا بدون آن (مثلاً [123456] یا 123456 به تنهایی؟ فقط کروشه‌دار برای احتیاط)
    text = re.sub(r'\[\d+\]', '', text)
    # حذف فاصله‌های اضافی
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def send_text_to_bale(text: str) -> bool:
    """ارسال متن ساده به کانال بله (با قالب HTML)"""
    url = f"https://tapi.bale.ai/bot{BALE_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": BALE_CHANNEL_ID,
        "text": text,
        "parse_mode": "HTML"
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("ok"):
                return True
            else:
                print(f"⚠️ خطا از سمت بله (sendMessage): {data}")
                return False
        else:
            print(f"⚠️ خطای HTTP {resp.status_code}: {resp.text}")
            return False
    except Exception as e:
        print(f"⚠️ خطا در ارسال متن به بله: {e}")
        return False

def send_photo_to_bale(photo_path: str, caption: str = "") -> bool:
    """ارسال عکس به کانال بله همراه با کپشن"""
    url = f"https://tapi.bale.ai/bot{BALE_BOT_TOKEN}/sendPhoto"
    try:
        with open(photo_path, 'rb') as f:
            files = {'photo': f}
            data = {'chat_id': BALE_CHANNEL_ID, 'caption': caption}
            resp = requests.post(url, data=data, files=files, timeout=30)
        if resp.status_code == 200 and resp.json().get("ok"):
            return True
        else:
            print(f"⚠️ خطا در sendPhoto: {resp.text}")
            return False
    except Exception as e:
        print(f"⚠️ خطا در ارسال عکس: {e}")
        return False

def send_video_to_bale(video_path: str, caption: str = "") -> bool:
    """ارسال ویدیو به کانال بله همراه با کپشن"""
    url = f"https://tapi.bale.ai/bot{BALE_BOT_TOKEN}/sendVideo"
    try:
        with open(video_path, 'rb') as f:
            files = {'video': f}
            data = {'chat_id': BALE_CHANNEL_ID, 'caption': caption}
            resp = requests.post(url, data=data, files=files, timeout=60)
        if resp.status_code == 200 and resp.json().get("ok"):
            return True
        else:
            print(f"⚠️ خطا در sendVideo: {resp.text}")
            return False
    except Exception as e:
        print(f"⚠️ خطا در ارسال ویدیو: {e}")
        return False

def load_state() -> dict:
    """بارگذاری آخرین شناسه پیام‌های پردازش شده"""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except:
            return {"last_message_ids": {}}
    return {"last_message_ids": {}}

def save_state(state: dict):
    """ذخیره وضعیت"""
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

async def main():
    # بررسی وجود تنظیمات ضروری
    if not STRING_SESSION or not API_ID or not API_HASH:
        print("❌ STRING_SESSION، API_ID یا API_HASH تنظیم نشده است.")
        return
    if not BALE_BOT_TOKEN or not BALE_CHANNEL_ID:
        print("❌ BALE_BOT_TOKEN یا BALE_CHANNEL_ID تنظیم نشده است.")
        return
    if not SOURCE_CHANNELS:
        print("⚠️ هیچ کانال منبعی تعریف نشده است.")
        return

    # اتصال به تلگرام با StringSession
    client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
    await client.connect()

    if not await client.is_user_authorized():
        print("❌ StringSession معتبر نیست. لطفاً یک جلسه جدید ایجاد کنید.")
        await client.disconnect()
        return

    print("✅ اتصال به تلگرام برقرار شد.")

    state = load_state()
    last_ids = state.get("last_message_ids", {})
    new_last_ids = last_ids.copy()

    for channel_identifier in SOURCE_CHANNELS:
        try:
            entity = await client.get_entity(channel_identifier)
            chat_id = str(entity.id)
            last_id = last_ids.get(chat_id, 0)

            # واکشی پیام‌های جدید (حداکثر 20 عدد)
            async for msg in client.iter_messages(entity, min_id=last_id, reverse=True, limit=20):
                if msg.id <= last_id:
                    continue

                # استخراج متن یا کپشن
                raw_text = msg.text or msg.caption or ""
                cleaned_caption = clean_telegram_mentions(raw_text)

                # ساخت لینک پست در تلگرام
                if entity.username:
                    post_link = f"https://t.me/{entity.username}/{msg.id}"
                else:
                    # برای کانال‌های بدون یوزرنیم (معمولاً id منفی است)
                    post_link = f"https://t.me/c/{str(entity.id)[4:]}/{msg.id}"  # حذف -100

                # قالب نهایی پیام (برای متن یا کپشن)
                footer = "\n\nمنبع(<a href='{}'>لینک</a>)\n@CX_NEWS | اخبار اقتصادی".format(post_link)
                final_caption = (cleaned_caption + footer) if cleaned_caption else footer.strip()

                # ارسال بر اساس نوع رسانه
                success = False
                temp_file = None

                if msg.photo:
                    # دانلود عکس در فایل موقت
                    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                        temp_file = tmp.name
                    await client.download_media(msg.photo, temp_file)
                    success = send_photo_to_bale(temp_file, final_caption)
                    os.unlink(temp_file)  # حذف فایل موقت
                elif msg.video:
                    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
                        temp_file = tmp.name
                    await client.download_media(msg.video, temp_file)
                    success = send_video_to_bale(temp_file, final_caption)
                    os.unlink(temp_file)
                else:
                    # پیام متنی ساده
                    full_text = f"{raw_text}\n\n{footer}" if raw_text else footer
                    success = send_text_to_bale(full_text)

                if success:
                    print(f"✅ ارسال شد: {post_link}")
                    new_last_ids[chat_id] = msg.id
                else:
                    print(f"❌ ارسال نشد: {post_link}")

                # تأخیر بین پیام‌ها
                await asyncio.sleep(SLEEP_BETWEEN_MESSAGES)

        except FloodWaitError as e:
            print(f"⚠️ محدودیت تلگرام: باید {e.seconds} ثانیه صبر کرد.")
            await asyncio.sleep(e.seconds)
        except Exception as e:
            print(f"⚠️ خطا در پردازش کانال {channel_identifier}: {e}")

    # ذخیره وضعیت جدید
    state["last_message_ids"] = new_last_ids
    save_state(state)

    await client.disconnect()
    print("🏁 اسکریپت با موفقیت پایان یافت.")

if __name__ == "__main__":
    asyncio.run(main())
