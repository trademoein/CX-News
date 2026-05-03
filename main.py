import asyncio
import json
import os
import re
import tempfile
import base64
from datetime import datetime

import requests
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument

# ======================== خواندن تنظیمات از environment variables ========================
# اطلاعات فایل session (base64)
SESSION_BASE64 = os.environ.get("SESSION_BASE64", "").strip()
# اطلاعات API
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "").strip()

# لیست کانال‌های مبدأ (JSON array)
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
SLEEP_BETWEEN_MESSAGES = 1  # ثانیه

# ====================================================================

def clean_telegram_mentions(text: str) -> str:
    """حذف شناسه‌های تلگرامی (@username، لینک‌های t.me، شناسه‌های عددی)"""
    if not text:
        return ""
    text = re.sub(r'@[\w_]+', '', text)
    text = re.sub(r'https?://t\.me/\S+', '', text)
    text = re.sub(r'\[\d+\]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def send_text_to_bale(text: str) -> bool:
    url = f"https://tapi.bale.ai/bot{BALE_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": BALE_CHANNEL_ID,
        "text": text,
        "parse_mode": "HTML"
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code == 200 and resp.json().get("ok"):
            return True
        else:
            print(f"⚠️ خطا در sendMessage: {resp.text}")
            return False
    except Exception as e:
        print(f"⚠️ خطا در ارسال متن: {e}")
        return False

def send_photo_to_bale(photo_path: str, caption: str = "") -> bool:
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
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except:
            return {"last_message_ids": {}}
    return {"last_message_ids": {}}

def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

async def main():
    # اعتبارسنجی اولیه
    if not SESSION_BASE64:
        print("❌ SESSION_BASE64 تنظیم نشده است.")
        return
    if not API_ID or not API_HASH:
        print("❌ API_ID یا API_HASH تنظیم نشده است.")
        return
    if not BALE_BOT_TOKEN or not BALE_CHANNEL_ID:
        print("❌ BALE_BOT_TOKEN یا BALE_CHANNEL_ID تنظیم نشده است.")
        return
    if not SOURCE_CHANNELS:
        print("⚠️ هیچ کانال منبعی تعریف نشده است.")
        return

    # بازسازی فایل session از base64
    session_bytes = base64.b64decode(SESSION_BASE64)
    session_file = "temp_session.session"
    with open(session_file, "wb") as f:
        f.write(session_bytes)
    
    # اتصال به تلگرام
    client = TelegramClient(session_file, API_ID, API_HASH)
    await client.connect()
    
    if not await client.is_user_authorized():
        print("❌ فایل session معتبر نیست. لطفاً دوباره آن را بسازید.")
        await client.disconnect()
        os.remove(session_file)
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

            async for msg in client.iter_messages(entity, min_id=last_id, reverse=True, limit=20):
                if msg.id <= last_id:
                    continue

                raw_text = msg.text or msg.caption or ""
                cleaned_caption = clean_telegram_mentions(raw_text)

                # ساخت لینک پست
                if entity.username:
                    post_link = f"https://t.me/{entity.username}/{msg.id}"
                else:
                    # برای کانال‌های بدون یوزرنیم (معمولاً id منفی با -100)
                    post_link = f"https://t.me/c/{str(entity.id)[4:]}/{msg.id}"

                footer = f"\n\nمنبع(<a href='{post_link}'>لینک</a>)\n@CX_NEWS | اخبار اقتصادی"
                final_caption = (cleaned_caption + footer) if cleaned_caption else footer.strip()

                success = False
                temp_file = None

                # تشخیص نوع رسانه
                if msg.photo:
                    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                        temp_file = tmp.name
                    await client.download_media(msg.photo, temp_file)
                    success = send_photo_to_bale(temp_file, final_caption)
                    os.unlink(temp_file)
                elif msg.video:
                    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
                        temp_file = tmp.name
                    await client.download_media(msg.video, temp_file)
                    success = send_video_to_bale(temp_file, final_caption)
                    os.unlink(temp_file)
                else:
                    # متن ساده
                    full_text = f"{raw_text}\n\n{footer}" if raw_text else footer
                    success = send_text_to_bale(full_text)

                if success:
                    print(f"✅ ارسال شد: {post_link}")
                    new_last_ids[chat_id] = msg.id
                else:
                    print(f"❌ ارسال نشد: {post_link}")

                await asyncio.sleep(SLEEP_BETWEEN_MESSAGES)

        except FloodWaitError as e:
            print(f"⚠️ محدودیت تلگرام: {e.seconds} ثانیه صبر کنید.")
            await asyncio.sleep(e.seconds)
        except Exception as e:
            print(f"⚠️ خطا در کانال {channel_identifier}: {e}")

    # ذخیره وضعیت
    state["last_message_ids"] = new_last_ids
    save_state(state)

    await client.disconnect()
    # پاک کردن فایل session موقت
    if os.path.exists(session_file):
        os.remove(session_file)
    print("🏁 پایان اسکریپت.")

if __name__ == "__main__":
    asyncio.run(main())
