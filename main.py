import asyncio
import json
import os
import re
import tempfile
import traceback
from contextlib import suppress
from datetime import datetime
from typing import Dict, Optional

import requests
from telethon import TelegramClient
from telethon.errors import FloodWaitError, RPCError
from telethon.sessions import MemorySession
from telethon.crypto import AuthKey
from telethon.tl.types import MessageService

# ================================ لاگینگ ================================
def log(msg: str, level: str = "INFO") -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [{level:5}] {msg}", flush=True)

# ================================ متغیرهای محیطی ================================
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
    log("SOURCE_CHANNELS نامعتبر، از لیست خالی استفاده می‌شود.", "ERROR")

BALE_BOT_TOKEN = os.environ.get("BALE_BOT_TOKEN", "").strip()
BALE_CHANNEL_ID = int(os.environ.get("BALE_CHANNEL_ID", 0))

STATE_FILE = "state.json"
SLEEP_BETWEEN_MESSAGES = 1.5
MAX_RETRIES = 3
RETRY_DELAY = 2

# ================================ کلاس سشن با IP ثابت ================================
class FixedIpSession(MemorySession):
    def __init__(self, dc_id: int, auth_key_hex: str, user_id: Optional[int] = None):
        super().__init__()
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
        log(f"FixedIpSession: DC={dc_id} → {server_address}:{port}")

# ================================ توابع کمکی ================================
def clean_lines_with_mentions(text: str) -> str:
    if not text:
        return ""
    lines = text.split('\n')
    new_lines = []
    for line in lines:
        if not re.search(r'@[\w_]+|https?://t\.me/\S+|\b\d{6,}\b', line):
            new_lines.append(line)
    return '\n'.join(new_lines).strip()

def load_state() -> Dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except:
            return {"last_message_ids": {}}
    return {"last_message_ids": {}}

def save_state(state: Dict) -> None:
    temp_file = STATE_FILE + ".tmp"
    with open(temp_file, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(temp_file, STATE_FILE)

def send_to_bale_with_retry(chat_id: int, text: str = None,
                             photo_path: str = None, video_path: str = None,
                             voice_path: str = None, audio_path: str = None,
                             sticker_path: str = None, animation_path: str = None,
                             document_path: str = None) -> bool:
    if not BALE_BOT_TOKEN:
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
    elif voice_path is not None:
        method = "sendVoice"
        data["caption"] = text or ""
        data["parse_mode"] = "HTML"
        files = {"voice": open(voice_path, "rb")}
    elif audio_path is not None:
        method = "sendAudio"
        data["caption"] = text or ""
        data["parse_mode"] = "HTML"
        files = {"audio": open(audio_path, "rb")}
    elif sticker_path is not None:
        method = "sendSticker"
        files = {"sticker": open(sticker_path, "rb")}
    elif animation_path is not None:
        method = "sendAnimation"
        data["caption"] = text or ""
        data["parse_mode"] = "HTML"
        files = {"animation": open(animation_path, "rb")}
    elif document_path is not None:
        method = "sendDocument"
        data["caption"] = text or ""
        data["parse_mode"] = "HTML"
        files = {"document": open(document_path, "rb")}
    else:
        return False

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(url_base + method, data=data, files=files, timeout=60)
            if files:
                for f in files.values():
                    f.close()
            if resp.status_code == 200 and resp.json().get("ok"):
                return True
        except Exception:
            pass
        if attempt < MAX_RETRIES:
            import time
            time.sleep(RETRY_DELAY * (2 ** (attempt - 1)))
    return False

# ================================ تابع اصلی ================================
async def main():
    log("=== راه‌اندازی ربات انتقال تلگرام به بله (نسخه نهایی) ===")

    if not all([API_ID, API_HASH, DC_ID, AUTH_KEY_HEX, USER_ID]):
        log("متغیرهای API_ID, API_HASH, DC_ID, AUTH_KEY_HEX, USER_ID الزامی هستند", "ERROR")
        return
    if not BALE_BOT_TOKEN or BALE_CHANNEL_ID == 0:
        log("BALE_BOT_TOKEN یا BALE_CHANNEL_ID تنظیم نشده", "ERROR")
        return
    if not SOURCE_CHANNELS:
        log("هیچ کانال منبعی تعریف نشده", "WARNING")
        return

    try:
        session = FixedIpSession(DC_ID, AUTH_KEY_HEX, USER_ID)
        client = TelegramClient(session, API_ID, API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            log("احراز هویت نشد. اطلاعات نشست معتبر نیست.", "ERROR")
            return
        log("✅ اتصال و احراز هویت موفق")
    except Exception as e:
        log(f"خطا در اتصال به تلگرام: {e}", "ERROR")
        traceback.print_exc()
        return

    state = load_state()
    last_ids = state.get("last_message_ids", {})
    new_last_ids = last_ids.copy()

    for chan in SOURCE_CHANNELS:
        log(f"--- کانال {chan} ---")
        try:
            entity = await client.get_entity(chan)
            chat_id_str = str(entity.id)
            last_id = last_ids.get(chat_id_str, 0)

            # اولین اجرا: فقط آخرین شناسه را ذخیره کن (هیچ پیامی ارسال نشود)
            if last_id == 0:
                last_msg = await client.get_messages(entity, limit=1)
                if last_msg:
                    last_id = last_msg[0].id
                    new_last_ids[chat_id_str] = last_id
                    state["last_message_ids"] = new_last_ids
                    save_state(state)
                    log(f"اولین اجرا: آخرین پیام id={last_id} ذخیره شد. از اجرای بعد پیام‌های جدید ارسال می‌شوند.")
                else:
                    log(f"کانال {chan} پیامی ندارد.")
                continue  # مهم: از این کانال خارج شو و هیچ ارسالی انجام نده

            # واکشی پیام‌های جدیدتر از last_id
            async for msg in client.iter_messages(entity, min_id=last_id, reverse=True, limit=30):
                if msg.id <= last_id:
                    continue

                if isinstance(msg, MessageService):
                    log(f"پیام سرویس id={msg.id} رد شد", "DEBUG")
                    new_last_ids[chat_id_str] = msg.id
                    continue

                # دریافت متن یا کپشن (بدون خطا)
                raw_text = ""
                if hasattr(msg, 'text') and msg.text:
                    raw_text = msg.text
                elif hasattr(msg, 'caption') and msg.caption:
                    raw_text = msg.caption

                cleaned_text = clean_lines_with_mentions(raw_text)

                # اگر پس از پاکسازی متنی نماند و رسانه هم نداشته باشد
                if not cleaned_text and not any([msg.photo, msg.video, msg.voice, msg.audio, msg.sticker, msg.animation, msg.document]):
                    log(f"پیام خالی id={msg.id} – فقط به‌روزرسانی last_id", "DEBUG")
                    new_last_ids[chat_id_str] = msg.id
                    continue

                # ساخت لینک و فوتر
                if entity.username:
                    post_link = f"https://t.me/{entity.username}/{msg.id}"
                else:
                    post_link = f"https://t.me/c/{str(entity.id)[4:]}/{msg.id}"
                footer = f"\n\n<a href='{post_link}'>منبع</a>\n@CX_NEWS | اخبار اقتصادی"
                final_caption = (cleaned_text + footer) if cleaned_text else footer

                success = False
                temp_file = None

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
                    elif msg.voice:
                        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
                            temp_file = tmp.name
                        await client.download_media(msg.voice, temp_file)
                        success = await asyncio.to_thread(
                            send_to_bale_with_retry, BALE_CHANNEL_ID, final_caption, voice_path=temp_file
                        )
                    elif msg.audio:
                        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
                            temp_file = tmp.name
                        await client.download_media(msg.audio, temp_file)
                        success = await asyncio.to_thread(
                            send_to_bale_with_retry, BALE_CHANNEL_ID, final_caption, audio_path=temp_file
                        )
                    elif msg.sticker:
                        with tempfile.NamedTemporaryFile(suffix=".webp", delete=False) as tmp:
                            temp_file = tmp.name
                        await client.download_media(msg.sticker, temp_file)
                        success = await asyncio.to_thread(
                            send_to_bale_with_retry, BALE_CHANNEL_ID, sticker_path=temp_file
                        )
                    elif msg.animation:
                        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
                            temp_file = tmp.name
                        await client.download_media(msg.animation, temp_file)
                        success = await asyncio.to_thread(
                            send_to_bale_with_retry, BALE_CHANNEL_ID, final_caption, animation_path=temp_file
                        )
                    elif msg.document:
                        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as tmp:
                            temp_file = tmp.name
                        await client.download_media(msg.document, temp_file)
                        success = await asyncio.to_thread(
                            send_to_bale_with_retry, BALE_CHANNEL_ID, final_caption, document_path=temp_file
                        )
                    else:
                        # پیام متنی ساده
                        success = await asyncio.to_thread(
                            send_to_bale_with_retry, BALE_CHANNEL_ID, text=final_caption
                        )
                except Exception as e:
                    log(f"خطا در دانلود/ارسال پیام {msg.id}: {e}", "ERROR")
                finally:
                    if temp_file and os.path.exists(temp_file):
                        with suppress(Exception):
                            os.unlink(temp_file)

                if success:
                    log(f"✅ پیام {msg.id} با موفقیت به بله ارسال شد")
                    new_last_ids[chat_id_str] = msg.id
                    state["last_message_ids"] = new_last_ids
                    save_state(state)
                else:
                    log(f"❌ ارسال پیام {msg.id} ناموفق بود (last_id به‌روز نمی‌شود)", "ERROR")

                await asyncio.sleep(SLEEP_BETWEEN_MESSAGES)

        except FloodWaitError as e:
            log(f"محدودیت تلگرام: باید {e.seconds} ثانیه صبر کرد", "WARNING")
            await asyncio.sleep(e.seconds)
        except RPCError as e:
            log(f"خطای RPC در کانال {chan}: {e}", "ERROR")
        except Exception as e:
            log(f"خطای غیرمنتظره در کانال {chan}: {e}", "ERROR")
            traceback.print_exc()

    state["last_message_ids"] = new_last_ids
    save_state(state)
    await client.disconnect()
    log("=== اسکریپت پایان یافت ===")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("اسکریپت توسط کاربر متوقف شد", "WARNING")
    except Exception as e:
        log(f"خطای سطح بالا: {e}", "ERROR")
        traceback.print_exc()
