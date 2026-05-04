import asyncio
import json
import os
import re
import tempfile
import sys
import traceback
from datetime import datetime

# کتابخانه‌های اصلی
import requests
from telethon import TelegramClient
from telethon.errors import FloodWaitError, RPCError
from telethon.sessions import MemorySession
from telethon.crypto import AuthKey

# ================================ لاگینگ ساده اما دقیق ================================
def log(msg: str, level: str = "INFO"):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [{level}] {msg}", flush=True)

# ================================ خواندن متغیرهای محیطی ================================
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "").strip()
DC_ID = int(os.environ.get("DC_ID", 0))
AUTH_KEY_HEX = os.environ.get("AUTH_KEY_HEX", "").strip()
USER_ID = int(os.environ.get("USER_ID", 0))

SOURCE_CHANNELS_JSON = os.environ.get("SOURCE_CHANNELS", "[]")
try:
    SOURCE_CHANNELS = json.loads(SOURCE_CHANNELS_JSON)
except Exception as e:
    log(f"خطا در parsing SOURCE_CHANNELS: {e}", "ERROR")
    SOURCE_CHANNELS = []

BALE_BOT_TOKEN = os.environ.get("BALE_BOT_TOKEN", "").strip()
BALE_CHANNEL_ID = int(os.environ.get("BALE_CHANNEL_ID", 0))

STATE_FILE = "state.json"
SLEEP_BETWEEN_MESSAGES = 1

# ================================ کلاس نشست دستی (مثل روش ربات دیگر) ================================
class CustomSession(MemorySession):
    def __init__(self, dc_id: int, auth_key_hex: str, user_id: int = None):
        super().__init__()
        # نگاشت dc_id به آدرس IP و پورت معروف تلگرام
        if dc_id == 1:
            server_address = "149.154.175.59"
            port = 443
        elif dc_id == 2:
            server_address = "149.154.167.51"
            port = 443
        elif dc_id == 3:
            server_address = "149.154.175.100"
            port = 443
        elif dc_id == 4:
            server_address = "149.154.167.91"
            port = 443
        elif dc_id == 5:
            server_address = "149.154.171.5"
            port = 443
        else:
            raise ValueError(f"dc_id {dc_id} پشتیبانی نمی‌شود. فقط 1 تا 5 معتبر است.")
        
        self._dc_id = dc_id
        self._server_address = server_address
        self._port = port
        
        # ساخت AuthKey از رشته hex
        try:
            auth_key_bytes = bytes.fromhex(auth_key_hex)
            self._auth_key = AuthKey(data=auth_key_bytes)
        except Exception as e:
            log(f"خطا در تبدیل AUTH_KEY_HEX به bytes: {e}", "ERROR")
            raise
        
        if user_id:
            self._user_id = user_id
        
        log(f"CustomSession ساخته شد: dc_id={dc_id}, server={server_address}:{port}, user_id={user_id}")

# ================================ توابع پاکسازی متن ================================
def clean_telegram_mentions(text: str) -> str:
    if not text:
        return ""
    # حذف @username
    text = re.sub(r'@[\w_]+', '', text)
    # حذف لینک‌های t.me
    text = re.sub(r'https?://t\.me/\S+', '', text)
    # حذف شناسه‌های عددی داخل کروشه
    text = re.sub(r'\[\d+\]', '', text)
    # حذف فاصله‌های اضافی
    text = re.sub(r'\s+', ' ', text).strip()
    return text

# ================================ توابع ارسال به پیام‌رسان بله ================================
def send_text_to_bale(text: str) -> bool:
    if not BALE_BOT_TOKEN or not BALE_CHANNEL_ID:
        return False
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
            log(f"خطا در sendMessage: {resp.text}", "ERROR")
            return False
    except Exception as e:
        log(f"استثنا در ارسال متن به بله: {e}", "ERROR")
        return False

def send_photo_to_bale(photo_path: str, caption: str = "") -> bool:
    url = f"https://tapi.bale.ai/bot{BALE_BOT_TOKEN}/sendPhoto"
    try:
        with open(photo_path, 'rb') as f:
            files = {'photo': f}
            data = {'chat_id': BALE_CHANNEL_ID, 'caption': caption}
            resp = requests.post(url, data=data, files=files, timeout=30)
        return resp.status_code == 200 and resp.json().get("ok")
    except Exception as e:
        log(f"خطا در ارسال عکس: {e}", "ERROR")
        return False

def send_video_to_bale(video_path: str, caption: str = "") -> bool:
    url = f"https://tapi.bale.ai/bot{BALE_BOT_TOKEN}/sendVideo"
    try:
        with open(video_path, 'rb') as f:
            files = {'video': f}
            data = {'chat_id': BALE_CHANNEL_ID, 'caption': caption}
            resp = requests.post(url, data=data, files=files, timeout=60)
        return resp.status_code == 200 and resp.json().get("ok")
    except Exception as e:
        log(f"خطا در ارسال ویدیو: {e}", "ERROR")
        return False

# ================================ مدیریت فایل state (برای جلوگیری از ارسال تکراری) ================================
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

# ================================ تابع اصلی ================================
async def main():
    log("=== شروع اسکریپت ===")
    
    # بررسی وجود متغیرهای ضروری
    if not API_ID or not API_HASH:
        log("API_ID یا API_HASH تنظیم نشده است.", "ERROR")
        return
    if not DC_ID or not AUTH_KEY_HEX or not USER_ID:
        log("DC_ID یا AUTH_KEY_HEX یا USER_ID تنظیم نشده است.", "ERROR")
        return
    if not BALE_BOT_TOKEN or not BALE_CHANNEL_ID:
        log("BALE_BOT_TOKEN یا BALE_CHANNEL_ID تنظیم نشده است.", "ERROR")
        return
    if not SOURCE_CHANNELS:
        log("SOURCE_CHANNELS خالی است. لیست کانال‌ها را مشخص کنید.", "WARNING")
        return
    
    log(f"API_ID = {API_ID}")
    log(f"DC_ID = {DC_ID}")
    log(f"USER_ID = {USER_ID}")
    log(f"source_channels = {SOURCE_CHANNELS}")
    log(f"bale_channel_id = {BALE_CHANNEL_ID}")
    
    # ساخت نشست سفارشی
    try:
        session = CustomSession(DC_ID, AUTH_KEY_HEX, USER_ID)
    except Exception as e:
        log(f"ساخت CustomSession با خطا مواجه شد: {e}", "ERROR")
        traceback.print_exc()
        return
    
    # ساخت کلاینت تلگرام
    try:
        client = TelegramClient(session, API_ID, API_HASH)
        log("TelegramClient ساخته شد. در حال اتصال...")
    except Exception as e:
        log(f"خطا در ساخت TelegramClient: {e}", "ERROR")
        traceback.print_exc()
        return
    
    # اتصال به تلگرام
    try:
        await client.connect()
        log("اتصال برقرار شد. در حال بررسی احراز هویت...")
    except Exception as e:
        log(f"خطا در اتصال: {e}", "ERROR")
        traceback.print_exc()
        try:
            await client.disconnect()
        except:
            pass
        return
    
    # بررسی اعتبار نشست
    try:
        is_auth = await client.is_user_authorized()
        if is_auth:
            log("✅ احراز هویت موفق. کاربر مجاز است.")
        else:
            log("❌ احراز هویت ناموفق. اطلاعات نشست صحیح نیست.", "ERROR")
            await client.disconnect()
            return
    except Exception as e:
        log(f"خطا در بررسی is_user_authorized: {e}", "ERROR")
        traceback.print_exc()
        await client.disconnect()
        return
    
    # بارگذاری وضعیت قدیمی
    state = load_state()
    last_ids = state.get("last_message_ids", {})
    new_last_ids = last_ids.copy()
    log(f"وضعیت بارگذاری شد: last_ids = {last_ids}")
    
    # پردازش هر کانال
    for idx, channel_identifier in enumerate(SOURCE_CHANNELS, 1):
        log(f"--- کانال {idx}: {channel_identifier} ---")
        try:
            entity = await client.get_entity(channel_identifier)
            chat_id = str(entity.id)
            last_id = last_ids.get(chat_id, 0)
            log(f"دریافت entity برای {channel_identifier} -> chat_id={chat_id}, last_id={last_id}")
        except Exception as e:
            log(f"خطا در دریافت entity کانال {channel_identifier}: {e}", "ERROR")
            continue
        
        # واکشی پیام‌های جدید
        try:
            message_count = 0
            async for msg in client.iter_messages(entity, min_id=last_id, reverse=True, limit=20):
                if msg.id <= last_id:
                    continue
                message_count += 1
                log(f"پیام جدید id={msg.id} در {channel_identifier}")
                
                # متن اصلی
                raw_text = msg.text or msg.caption or ""
                cleaned_text = clean_telegram_mentions(raw_text)
                
                # ساخت لینک پست
                if entity.username:
                    post_link = f"https://t.me/{entity.username}/{msg.id}"
                else:
                    # برای کانال‌های بدون یوزرنیم (معمولاً id منفی)
                    post_link = f"https://t.me/c/{str(entity.id)[4:]}/{msg.id}"
                
                footer = f"\n\nمنبع(<a href='{post_link}'>لینک</a>)\n@CX_NEWS | اخبار اقتصادی"
                final_caption = (cleaned_text + footer) if cleaned_text else footer.strip()
                
                success = False
                temp_file = None
                
                # تشخیص نوع رسانه
                try:
                    if msg.photo:
                        log(f"دانلود عکس پیام {msg.id}...")
                        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                            temp_file = tmp.name
                        await client.download_media(msg.photo, temp_file)
                        log("ارسال عکس به بله...")
                        success = send_photo_to_bale(temp_file, final_caption)
                    elif msg.video:
                        log(f"دانلود ویدیو پیام {msg.id}...")
                        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
                            temp_file = tmp.name
                        await client.download_media(msg.video, temp_file)
                        log("ارسال ویدیو به بله...")
                        success = send_video_to_bale(temp_file, final_caption)
                    else:
                        # پیام متنی
                        full_text = f"{raw_text}\n\n{footer}" if raw_text else footer
                        log(f"ارسال متن پیام {msg.id} به بله...")
                        success = send_text_to_bale(full_text)
                except Exception as e:
                    log(f"خطا در پردازش رسانه پیام {msg.id}: {e}", "ERROR")
                finally:
                    if temp_file and os.path.exists(temp_file):
                        try:
                            os.unlink(temp_file)
                        except:
                            pass
                
                if success:
                    log(f"✅ پیام {msg.id} با موفقیت ارسال شد.")
                    new_last_ids[chat_id] = msg.id
                else:
                    log(f"❌ ارسال پیام {msg.id} ناموفق بود.", "ERROR")
                
                await asyncio.sleep(SLEEP_BETWEEN_MESSAGES)
            
            if message_count == 0:
                log(f"کانال {channel_identifier}: پیام جدیدی یافت نشد.")
            else:
                log(f"کانال {channel_identifier}: {message_count} پیام جدید پردازش شد.")
                
        except FloodWaitError as e:
            log(f"محدودیت تلگرام: باید {e.seconds} ثانیه صبر کرد.", "WARNING")
            await asyncio.sleep(e.seconds)
        except RPCError as e:
            log(f"خطای RPC در کانال {channel_identifier}: {e}", "ERROR")
        except Exception as e:
            log(f"خطای غیرمنتظره در کانال {channel_identifier}: {e}", "ERROR")
            traceback.print_exc()
    
    # ذخیره وضعیت جدید
    state["last_message_ids"] = new_last_ids
    save_state(state)
    log("وضعیت ذخیره شد.")
    
    await client.disconnect()
    log("=== پایان اسکریپت ===")

# ================================ نقطه ورود ================================
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("اسکریپت توسط کاربر متوقف شد.", "WARNING")
    except Exception as e:
        log(f"خطای سطح بالا: {e}", "ERROR")
        traceback.print_exc()
