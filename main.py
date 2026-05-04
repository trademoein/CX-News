import asyncio
import json
import os
import re
import tempfile
import traceback
from contextlib import suppress
from datetime import datetime
from typing import Dict, List, Optional

import requests
from telethon import TelegramClient
from telethon.errors import FloodWaitError, RPCError
from telethon.sessions import MemorySession
from telethon.crypto import AuthKey
from telethon.tl.types import Message, MessageService

# ================================ لاگینگ حرفه‌ای ================================
def log(msg: str, level: str = "INFO") -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [{level:5}] {msg}", flush=True)

# ================================ خواندن متغیرهای محیطی ================================
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "").strip()
DC_ID = int(os.environ.get("DC_ID", 0))
AUTH_KEY_HEX = os.environ.get("AUTH_KEY_HEX", "").strip()
USER_ID = int(os.environ.get("USER_ID", 0))

SOURCE_CHANNELS_JSON = os.environ.get("SOURCE_CHANNELS", "[]")
try:
    SOURCE_CHANNELS = json.loads(SOURCE_CHANNELS_JSON)
except Exception:
    SOURCE_CHANNELS = []
    log("SOURCE_CHANNELS نامعتبر، از آرایه‌ی خالی استفاده می‌شود.", "ERROR")

BALE_BOT_TOKEN = os.environ.get("BALE_BOT_TOKEN", "").strip()
BALE_CHANNEL_ID = int(os.environ.get("BALE_CHANNEL_ID", 0))

STATE_FILE = "state.json"
SLEEP_BETWEEN_MESSAGES = 1.5          # ثانیه، برای جلوگیری از spam در بله
MAX_RETRIES = 3                        # تعداد تلاش برای ارسال به بله
RETRY_DELAY = 2                        # ثانیه اولیه برای بک‌آف

# ================================ کلاس سشن با IP ثابت (مثل ربات موفق) ================================
class FixedIpSession(MemorySession):
    """نشستی که آدرس IP سرور را بر اساس DC ID مستقیماً ست می‌کند (بدون DNS)"""
    def __init__(self, dc_id: int, auth_key_hex: str, user_id: Optional[int] = None):
        super().__init__()
        # نگاشت dc_id به IP و پورت معروف تلگرام
        servers = {
            1: ("149.154.175.59", 443),
            2: ("149.154.167.51", 443),
            3: ("149.154.175.100", 443),
            4: ("149.154.167.91", 443),
            5: ("149.154.171.5", 443),
        }
        if dc_id not in servers:
            raise ValueError(f"DC_ID {dc_id} پشتیبانی نمی‌شود (فقط 1-5)")
        server_address, port = servers[dc_id]
        self._dc_id = dc_id
        self._server_address = server_address
        self._port = port
        auth_key_bytes = bytes.fromhex(auth_key_hex)
        self._auth_key = AuthKey(data=auth_key_bytes)
        if user_id:
            self._user_id = user_id
        log(f"FixedIpSession ساخته شد: DC={dc_id} → {server_address}:{port}")

# ================================ توابع کمکی ================================
def clean_mentions(text: str) -> str:
    """حذف ممشن‌های تلگرامی (@username, t.me links, numeric IDs)"""
    if not text:
        return ""
    text = re.sub(r'@[\w_]+', '', text)
    text = re.sub(r'https?://t\.me/\S+', '', text)
    text = re.sub(r'\[\d+\]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def load_state() -> Dict:
    """بارگذاری آخرین شناسه پیام پردازش شده برای هر کانال"""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {"last_message_ids": {}}
    return {"last_message_ids": {}}

def save_state(state: Dict) -> None:
    """ذخیره وضعیت با atomic write (از طریق write to temp سپس جایگزینی)"""
    temp_file = STATE_FILE + ".tmp"
    with open(temp_file, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(temp_file, STATE_FILE)  # atomic on Unix

# ================================ ارسال به پیام‌رسان بله (با retry و backoff) ================================
def send_to_bale_with_retry(chat_id: int, text: str = None, photo_path: str = None, video_path: str = None) -> bool:
    """ارسال متن، عکس یا ویدیو با حداکثر MAX_RETRIES تلاش و تاخیر تصاعدی"""
    if not BALE_BOT_TOKEN:
        log("BALE_BOT_TOKEN تنظیم نشده", "ERROR")
        return False
    url_base = f"https://tapi.bale.ai/bot{BALE_BOT_TOKEN}/"
    data = {"chat_id": chat_id}
    files = None
    method = "sendMessage"
    
    if text is not None:
        data["text"] = text
        data["parse_mode"] = "HTML"
        method = "sendMessage"
    elif photo_path is not None:
        method = "sendPhoto"
        data["caption"] = text or ""
        data["parse_mode"] = "HTML"
        files = {"photo": open(photo_path, "rb")}
    elif video_path is not None:
        method = "sendVideo"
        data["caption"] = text or ""
        data["parse_mode"] = "HTML"
        files = {"video": open(video_path, "rb")}
    else:
        log("هیچ محتوایی برای ارسال وجود ندارد", "ERROR")
        return False

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(url_base + method, data=data, files=files, timeout=30)
            if files:
                # بستن فایل بعد از درخواست
                for f in files.values():
                    f.close()
            if resp.status_code == 200 and resp.json().get("ok"):
                return True
            else:
                error_text = resp.text[:200]
                log(f"خطا در ارسال (تلاش {attempt}/{MAX_RETRIES}): {error_text}", "WARNING")
        except Exception as e:
            log(f"استثنا در ارسال (تلاش {attempt}/{MAX_RETRIES}): {e}", "WARNING")
        
        if attempt < MAX_RETRIES:
            delay = RETRY_DELAY * (2 ** (attempt - 1))  # 2,4,8
            log(f"تلاش مجدد بعد از {delay} ثانیه...")
            time.sleep(delay)   # note: time.sleep (non-async) مشکلی ندارد چون داخل تابع sync است
    return False

# ================================ تابع اصلی (Async) ================================
async def main():
    log("=== راه‌اندازی ربات انتقال تلگرام به بله ===")
    
    # اعتبارسنجی اولیه
    if not all([API_ID, API_HASH, DC_ID, AUTH_KEY_HEX, USER_ID]):
        log("متغیرهای API_ID, API_HASH, DC_ID, AUTH_KEY_HEX, USER_ID همگی الزامی هستند", "ERROR")
        return
    if not BALE_BOT_TOKEN or BALE_CHANNEL_ID == 0:
        log("BALE_BOT_TOKEN یا BALE_CHANNEL_ID تنظیم نشده", "ERROR")
        return
    if not SOURCE_CHANNELS:
        log("هیچ کانال منبعی تعریف نشده (SOURCE_CHANNELS خالی)", "WARNING")
        return
    
    log(f"تنظیمات: API_ID={API_ID}, DC_ID={DC_ID}, USER_ID={USER_ID}")
    log(f"کانال‌های مقصد بله: {BALE_CHANNEL_ID}")
    log(f"کانال‌های منبع تلگرام: {SOURCE_CHANNELS}")
    
    # ساخت نشست و کلاینت
    try:
        session = FixedIpSession(DC_ID, AUTH_KEY_HEX, USER_ID)
        client = TelegramClient(session, API_ID, API_HASH)
        log("TelegramClient ساخته شد. در حال اتصال...")
    except Exception as e:
        log(f"خطا در ساخت کلاینت: {e}", "ERROR")
        traceback.print_exc()
        return
    
    # اتصال با retry
    connected = False
    for attempt in range(1, 4):
        try:
            await client.connect()
            if await client.is_user_authorized():
                connected = True
                log("✅ اتصال و احراز هویت موفق")
                break
            else:
                log("❌ نشست معتبر نیست (احراز هویت نشد)", "ERROR")
                await client.disconnect()
                return
        except Exception as e:
            log(f"خطا در اتصال (تلاش {attempt}/3): {e}", "WARNING")
            await asyncio.sleep(2 ** attempt)
    if not connected:
        log("امکان اتصال به تلگرام وجود ندارد. برنامه خاتمه می‌یابد.", "ERROR")
        return
    
    # بارگذاری state
    state = load_state()
    last_ids = state.get("last_message_ids", {})
    new_last_ids = last_ids.copy()
    log(f"وضعیت قبلی بارگذاری شد. last_ids: {last_ids}")
    
    # پردازش هر کانال
    for idx, chan in enumerate(SOURCE_CHANNELS, 1):
        log(f"--- کانال {idx}: {chan} ---")
        try:
            entity = await client.get_entity(chan)
            chat_id_str = str(entity.id)
            last_id = last_ids.get(chat_id_str, 0)
            log(f"دریافت entity: chat_id={chat_id_str}, last_id={last_id}")
        except Exception as e:
            log(f"خطا در دریافت entity کانال {chan}: {e}", "ERROR")
            continue
        
        try:
            msg_count = 0
            async for msg in client.iter_messages(entity, min_id=last_id, reverse=True, limit=30):
                if msg.id <= last_id:
                    continue
                # ---------- رد کردن پیام‌های سرویس (MessageService) ----------
                if isinstance(msg, MessageService):
                    log(f"پیام سرویس id={msg.id} نادیده گرفته شد", "DEBUG")
                    new_last_ids[chat_id_str] = msg.id
                    continue
                
                # استخراج متن یا کپشن
                raw_text = ""
                if hasattr(msg, 'text') and msg.text:
                    raw_text = msg.text
                elif hasattr(msg, 'caption') and msg.caption:
                    raw_text = msg.caption
                
                # اگر پیام خالی و بدون رسانه است، فقط last_id را به‌روز کن
                if not raw_text and not msg.photo and not msg.video and not msg.document:
                    log(f"پیام خالی (id={msg.id}) بدون محتوا، فقط به‌روزرسانی last_id", "DEBUG")
                    new_last_ids[chat_id_str] = msg.id
                    continue
                
                msg_count += 1
                log(f"پردازش پیام id={msg.id} از {chan}")
                cleaned_text = clean_mentions(raw_text)
                
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
                
                # ارسال بر اساس نوع رسانه (اولویت: عکس و ویدیو)
                try:
                    if msg.photo:
                        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                            temp_file = tmp.name
                        await client.download_media(msg.photo, temp_file)
                        success = await asyncio.to_thread(
                            send_to_bale_with_retry, BALE_CHANNEL_ID, final_caption, photo_path=temp_file
                        )
                    elif msg.video:
                        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
                            temp_file = tmp.name
                        await client.download_media(msg.video, temp_file)
                        success = await asyncio.to_thread(
                            send_to_bale_with_retry, BALE_CHANNEL_ID, final_caption, video_path=temp_file
                        )
                    else:
                        # پیام متنی (متن خالص یا سند/صوتی که نمی‌خواهیم پشتیبانی کنیم)
                        full_text = f"{raw_text}\n\n{footer}" if raw_text else footer
                        success = await asyncio.to_thread(
                            send_to_bale_with_retry, BALE_CHANNEL_ID, text=full_text
                        )
                except Exception as e:
                    log(f"خطا در دانلود یا ارسال رسانه پیام {msg.id}: {e}", "ERROR")
                finally:
                    if temp_file and os.path.exists(temp_file):
                        with suppress(Exception):
                            os.unlink(temp_file)
                
                if success:
                    log(f"✅ پیام {msg.id} با موفقیت به بله ارسال شد")
                    new_last_ids[chat_id_str] = msg.id
                    # ذخیره state بعد از هر پیام موفق (برای جلوگیری از تکرار در crash)
                    state["last_message_ids"] = new_last_ids
                    save_state(state)
                else:
                    log(f"❌ ارسال پیام {msg.id} ناموفق بود، last_id به‌روز نمی‌شود", "ERROR")
                    # ما last_id را به‌روز نمی‌کنیم تا تلاش مجدد در اجرای بعدی انجام شود
                
                await asyncio.sleep(SLEEP_BETWEEN_MESSAGES)
            
            log(f"کانال {chan}: {msg_count} پیام جدید پردازش شد.")
        except FloodWaitError as e:
            log(f"FloodWait در تلگرام: باید {e.seconds} ثانیه صبر کرد", "WARNING")
            await asyncio.sleep(e.seconds)
        except RPCError as e:
            log(f"خطای RPC در کانال {chan}: {e}", "ERROR")
        except Exception as e:
            log(f"خطای غیرمنتظره در کانال {chan}: {e}", "ERROR")
            traceback.print_exc()
    
    # ذخیره نهایی state (غیر ضروری چون هر پیام ذخیره شد، اما برای اطمینان)
    state["last_message_ids"] = new_last_ids
    save_state(state)
    await client.disconnect()
    log("=== اسکریپت پایان یافت ===")

# ================================ اجرا ================================
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("اسکریپت توسط کاربر متوقف شد", "WARNING")
    except Exception as e:
        log(f"خطای سطح بالا: {e}", "ERROR")
        traceback.print_exc()
