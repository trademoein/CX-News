import asyncio
import json
import os
import re
from datetime import datetime

import requests
from telethon import TelegramClient
from telethon.sessions import MemorySession
from telethon.crypto import AuthKey
from telethon.errors import FloodWaitError

# ======================== تنظیمات - این قسمت را با مقادیر واقعی پر کنید ========================
# ---------- تنظیمات تلگرام ----------
API_ID = 12345                     # از my.telegram.org
API_HASH = "your_api_hash"
DC_ID = 2                          # مثلاً 2 یا 4
AUTH_KEY_HEX = "your_auth_key_hex"  # بدون هیچ پیشوندی، فقط هگز
USER_ID = 123456789                # آیدی عددی خودتان (اختیاری)

# نام کاربری یا آیدی عددی کانال‌های مبدأ (لیست دو کانال)
SOURCE_CHANNELS = [
    "@source_channel_1",           # مثال: "@economic_news"
    "@source_channel_2"            # مثال: "@tech_news"
]

# ---------- تنظیمات بله ----------
BALE_BOT_TOKEN = "your_bale_bot_token"   # توکن ربات بله
BALE_CHANNEL_ID = 4713554010             # آیدی عددی کانال مقصد در بله (همان که قبلاً پیدا کردید)

# ---------- فایل ذخیره وضعیت (برای جلوگیری از ارسال تکراری) ----------
STATE_FILE = "state.json"

# ====================================================================

# کلاس سشن سفارشی برای استفاده از AUTH_KEY_HEX
class CustomSession(MemorySession):
    def __init__(self, dc_id, auth_key, user_id=None):
        super().__init__()
        self._dc_id = dc_id
        self._auth_key = AuthKey(data=bytes.fromhex(auth_key))
        if user_id:
            self._user_id = user_id

# ابزار حذف شناسه‌های تلگرامی از متن
def clean_telegram_mentions(text):
    if not text:
        return ""
    # حذف @username
    text = re.sub(r'@[\w_]+', '', text)
    # حذف لینک‌های t.me/... (فقط خود لینک، نه کل متن)
    text = re.sub(r'https?://t\.me/\S+', '', text)
    # حذف شناسه‌های عددی (مثل [123456789])
    text = re.sub(r'\[\d+\]', '', text)
    # حذف فاصله‌های اضافی
    text = re.sub(r'\s+', ' ', text).strip()
    return text

# ارسال پیام به کانال بله
def send_to_bale(text):
    url = f"https://tapi.bale.ai/bot{BALE_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": BALE_CHANNEL_ID,
        "text": text,
        "parse_mode": "HTML"
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("ok"):
                return True
            else:
                print(f"⚠️ خطا از سمت بله: {data}")
                return False
        else:
            print(f"⚠️ خطای HTTP {resp.status_code}: {resp.text}")
            return False
    except Exception as e:
        print(f"⚠️ خطا در ارسال به بله: {e}")
        return False

# بارگذاری آخرین پیام‌های پردازش شده
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"last_message_ids": {}}  # key: chat_id, value: last_processed_message_id

# ذخیره وضعیت
def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

async def main():
    # ساخت کلاینت تلگرام با سشن سفارشی
    session = CustomSession(DC_ID, AUTH_KEY_HEX, USER_ID)
    client = TelegramClient(session, API_ID, API_HASH)
    
    await client.connect()
    
    if not await client.is_user_authorized():
        print("❌ اتصال به تلگرام انجام نشد. اطلاعات را بررسی کنید.")
        return
    
    print("✅ متصل به تلگرام شد.")
    
    # بارگذاری وضعیت قبلی
    state = load_state()
    last_ids = state.get("last_message_ids", {})
    new_last_ids = last_ids.copy()
    
    for channel_identifier in SOURCE_CHANNELS:
        try:
            # دریافت entity کانال
            entity = await client.get_entity(channel_identifier)
            chat_id = entity.id
            last_id = last_ids.get(str(chat_id), 0)
            
            # دریافت پیام‌های جدید از آخرین id ذخیره شده به بعد (حداکثر 20 پیام)
            messages = []
            async for msg in client.iter_messages(entity, min_id=last_id, reverse=True, limit=20):
                if msg.id > last_id:
                    messages.append(msg)
            
            if not messages:
                print(f"📭 کانال {channel_identifier}: پیام جدیدی یافت نشد.")
                continue
            
            # پردازش هر پیام به ترتیب قدیم به جدید
            for msg in messages:
                original_text = msg.text or msg.caption or ""
                cleaned_text = clean_telegram_mentions(original_text)
                if not cleaned_text:
                    # اگر بعد از پاک کردن چیزی نماند، فقط عنوان بفرستیم؟
                    cleaned_text = "(پیام بدون متن)"
                
                # ساخت لینک مستقیم به پست در تلگرام (با استفاده از نام کاربری کانال یا id)
                if entity.username:
                    post_link = f"https://t.me/{entity.username}/{msg.id}"
                else:
                    # برای کانال‌های بدون یوزرنیم، از لینک با id استفاده می‌شود (احتیاط: ممکن است کار نکند)
                    post_link = f"https://t.me/c/{str(chat_id)[4:]}/{msg.id}"  # برای ایدی‌های -100...
                
                # ساخت پیام نهایی با قالب مورد نظر
                final_message = f"{cleaned_text}\n\nمنبع(<a href='{post_link}'>لینک</a>)\n@CX_NEWS | اخبار اقتصادی"
                
                # ارسال به بله
                success = send_to_bale(final_message)
                if success:
                    print(f"✅ ارسال شد: {post_link}")
                else:
                    print(f"❌ ارسال نشد: {post_link}")
                
                # به‌روزرسانی آخرین id پردازش شده
                new_last_ids[str(chat_id)] = msg.id
                
                # کمی تأخیر برای جلوگیری از محدودیت نرخ در بله
                await asyncio.sleep(1)
        
        except FloodWaitError as e:
            print(f"⚠️ محدودیت تلگرام: باید {e.seconds} ثانیه صبر کرد.")
            await asyncio.sleep(e.seconds)
        except Exception as e:
            print(f"⚠️ خطا در کانال {channel_identifier}: {e}")
    
    # ذخیره وضعیت جدید
    state["last_message_ids"] = new_last_ids
    save_state(state)
    await client.disconnect()
    print("🏁 پایان اسکریپت.")

if __name__ == "__main__":
    asyncio.run(main())
