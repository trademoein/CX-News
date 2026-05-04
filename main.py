#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ربات انتقال تلگرام به بله – نسخه پایدار (رفع خطای caption)
"""

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

SLEEP_BETWEEN_MESSAGES = 1.5
MAX_RETRIES = 3
RETRY_DELAY = 2

STATE_FILE = "state.json"

# ================================ مدیریت state محلی ================================
class StateManager:
    def __init__(self):
        self.data = {
            "last_message_ids": {},
            "dead_letter": [],
            "admin_id": None,
            "stats": {"total_sent": 0, "total_failed": 0, "last_run": None},
            "bale_last_update_id": 0,
            "retry_dead_letter": False,
            "skip_current_channel": False,
        }
        self.load()

    def load(self):
        if not os.path.exists(STATE_FILE):
            self.save()
            return
        try:
            with open(STATE_FILE, "r") as f:
                loaded = json.load(f)
                self.data.update(loaded)
        except Exception as e:
            log(f"خطا در بارگذاری state: {e}", "ERROR")

    def save(self):
        with open(STATE_FILE, "w") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)

    def get_last_id(self, channel: str) -> int:
        return self.data["last_message_ids"].get(channel, 0)

    def set_last_id(self, channel: str, msg_id: int):
        self.data["last_message_ids"][channel] = msg_id

    def add_to_dead_letter(self, channel: str, msg_id: int):
        self.data["dead_letter"].append({"channel": channel, "msg_id": msg_id, "added_at": datetime.now().isoformat()})
        self.data["stats"]["total_failed"] += 1
        self.save()

    def get_dead_letter(self) -> List[Dict]:
        return self.data["dead_letter"]

    def clear_dead_letter(self):
        self.data["dead_letter"] = []
        self.save()

    def set_admin_id(self, admin_id: int):
        self.data["admin_id"] = admin_id
        self.save()

    def get_admin_id(self) -> Optional[int]:
        return self.data.get("admin_id")

    def inc_sent_count(self):
        self.data["stats"]["total_sent"] += 1
        self.save()

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

# ================================ ابزارهای کمکی ================================
def clean_lines_with_mentions(text: str) -> str:
    if not text:
        return ""
    lines = text.split('\n')
    new_lines = []
    for line in lines:
        if not re.search(r'@[\w_]+|https?://t\.me/\S+|\b\d{6,}\b', line):
            new_lines.append(line)
    return '\n'.join(new_lines).strip()

def build_footer(post_link: str) -> str:
    return f"\n\n<a href='{post_link}'>منبع</a>\n@CX_NEWS | اخبار اقتصادی"

# ================================ کلاس ارتباط با بله ================================
class BaleClient:
    def __init__(self, token: str, state_manager: StateManager):
        self.token = token
        self.base_url = f"https://tapi.bale.ai/bot{token}/"
        self.state = state_manager
        self.last_update_id = self.state.data.get("bale_last_update_id", 0)

    def _save_offset(self):
        self.state.data["bale_last_update_id"] = self.last_update_id
        self.state.save()

    def send_message(self, chat_id: int, text: str) -> bool:
        if not self.token:
            return False
        url = self.base_url + "sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML"
        }
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = requests.post(url, json=payload, timeout=15)
                if resp.status_code == 200 and resp.json().get("ok"):
                    return True
            except Exception:
                pass
            if attempt < MAX_RETRIES:
                import time
                time.sleep(RETRY_DELAY * (2 ** (attempt - 1)))
        return False

    def send_media(self, chat_id: int, caption: str, file_path: str, media_type: str) -> bool:
        method_map = {
            "photo": "sendPhoto", "video": "sendVideo", "voice": "sendVoice",
            "audio": "sendAudio", "sticker": "sendSticker",
            "animation": "sendAnimation", "document": "sendDocument"
        }
        method = method_map.get(media_type)
        if not method:
            return False
        url = self.base_url + method
        data = {"chat_id": chat_id}
        if media_type != "sticker":
            data["caption"] = caption
            data["parse_mode"] = "HTML"
        files = {media_type: open(file_path, "rb")}
        try:
            resp = requests.post(url, data=data, files=files, timeout=60)
            return resp.status_code == 200 and resp.json().get("ok")
        except Exception:
            return False
        finally:
            files[media_type].close()

    def process_admin_commands(self):
        if not self.token:
            return
        url = self.base_url + "getUpdates"
        params = {"offset": self.last_update_id + 1, "timeout": 2, "limit": 10}
        try:
            resp = requests.get(url, params=params, timeout=15)
            if resp.status_code != 200:
                return
            result = resp.json()
            if not result.get("ok"):
                return
            for update in result.get("result", []):
                self.last_update_id = update["update_id"]
                msg = update.get("message")
                if not msg:
                    continue
                chat_id = msg["chat"]["id"]
                text = msg.get("text", "")
                admin_id = self.state.get_admin_id()
                if admin_id is None:
                    self.state.set_admin_id(chat_id)
                    self.send_message(chat_id, "✅ شما به عنوان ادمین ربات ثبت شدید. ارسال /help برای راهنما.")
                    admin_id = chat_id
                elif chat_id != admin_id:
                    self.send_message(chat_id, "⛔ شما دسترسی ادمین ندارید.")
                    continue

                if text == "/status":
                    stats = self.state.data["stats"]
                    dead_len = len(self.state.get_dead_letter())
                    last_id_info = "\n".join([f"{ch}: {last_id}" for ch, last_id in self.state.data["last_message_ids"].items()])
                    status_msg = (
                        f"📊 **آمار ربات**\n"
                        f"✅ ارسال موفق: {stats['total_sent']}\n"
                        f"❌ ناموفق در صف: {dead_len}\n"
                        f"🕒 آخرین اجرا: {stats.get('last_run', 'ندارد')}\n"
                        f"📌 **آخرین شناسه کانال‌ها:**\n{last_id_info}"
                    )
                    self.send_message(admin_id, status_msg)
                elif text == "/retry":
                    self.send_message(admin_id, "🔄 در حال تلاش مجدد برای پیام‌های ناموفق...")
                    self.state.data["retry_dead_letter"] = True
                    self.state.save()
                elif text == "/skip":
                    self.state.data["skip_current_channel"] = True
                    self.state.save()
                    self.send_message(admin_id, "⏭️ کانال فعلی در اجرای بعدی رد می‌شود.")
                elif text == "/help":
                    help_msg = (
                        "🔧 **دستورات ادمین:**\n"
                        "/status - وضعیت ربات\n"
                        "/retry - تلاش مجدد برای پیام‌های شکست خورده\n"
                        "/skip - رد شدن از کانال گیر کرده\n"
                        "/help - این راهنما"
                    )
                    self.send_message(admin_id, help_msg)
                else:
                    self.send_message(admin_id, "دستور نامعتبر. /help برای راهنما.")
            self._save_offset()
        except Exception as e:
            log(f"خطا در دریافت دستورات ادمین: {e}", "ERROR")

# ================================ ربات اصلی ================================
class TelegramToBaleBot:
    def __init__(self, state_manager: StateManager, bale_client: BaleClient):
        self.state = state_manager
        self.bale = bale_client
        self.client: Optional[TelegramClient] = None

    async def connect_telegram(self) -> bool:
        try:
            session = FixedIpSession(DC_ID, AUTH_KEY_HEX, USER_ID)
            self.client = TelegramClient(session, API_ID, API_HASH)
            await self.client.connect()
            if not await self.client.is_user_authorized():
                log("احراز هویت نشد. اطلاعات نشست معتبر نیست.", "ERROR")
                return False
            log("✅ اتصال و احراز هویت موفق")
            return True
        except Exception as e:
            log(f"خطا در اتصال به تلگرام: {e}", "ERROR")
            return False

    async def download_media_safe(self, msg: Message) -> Optional[str]:
        media_attr = None
        suffix = ".bin"
        if msg.photo:
            media_attr = msg.photo
            suffix = ".jpg"
        elif msg.video:
            media_attr = msg.video
            suffix = ".mp4"
        elif msg.voice:
            media_attr = msg.voice
            suffix = ".ogg"
        elif msg.audio:
            media_attr = msg.audio
            suffix = ".mp3"
        elif msg.sticker:
            media_attr = msg.sticker
            suffix = ".webp"
        elif getattr(msg, 'animation', None):
            media_attr = msg.animation
            suffix = ".mp4"
        elif msg.document:
            media_attr = msg.document
            doc_name = getattr(msg.document, 'attributes', [])
            for attr in doc_name:
                if hasattr(attr, 'file_name') and attr.file_name:
                    suffix = os.path.splitext(attr.file_name)[1]
                    break
            else:
                suffix = ".bin"
        else:
            return None

        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            temp_path = tmp.name
        try:
            await self.client.download_media(media_attr, temp_path)
            return temp_path
        except Exception as e:
            log(f"خطا در دانلود رسانه: {e}", "ERROR")
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            return None

    async def send_to_bale(self, chat_id: int, caption: str, file_path: Optional[str] = None, media_type: str = "") -> bool:
        if file_path and media_type:
            return self.bale.send_media(chat_id, caption, file_path, media_type)
        else:
            return self.bale.send_message(chat_id, caption)

    async def process_message(self, msg: Message, channel_entity, channel_key: str) -> bool:
        # اصلاح: استفاده از msg.text به جای msg.caption
        raw_text = msg.text or ""
        cleaned_text = clean_lines_with_mentions(raw_text)

        if hasattr(channel_entity, 'username') and channel_entity.username:
            post_link = f"https://t.me/{channel_entity.username}/{msg.id}"
        else:
            post_link = f"https://t.me/c/{str(channel_entity.id)[4:]}/{msg.id}"
        footer = build_footer(post_link)
        final_caption = (cleaned_text + footer) if cleaned_text else footer

        media_type = None
        file_path = None
        if msg.photo:
            media_type = "photo"
        elif msg.video:
            media_type = "video"
        elif msg.voice:
            media_type = "voice"
        elif msg.audio:
            media_type = "audio"
        elif msg.sticker:
            media_type = "sticker"
        elif getattr(msg, 'animation', None):
            media_type = "animation"
        elif msg.document:
            media_type = "document"

        if media_type:
            file_path = await self.download_media_safe(msg)
            if not file_path:
                log(f"دانلود رسانه پیام {msg.id} ناموفق", "ERROR")
                return False

        success = await self.send_to_bale(BALE_CHANNEL_ID, final_caption, file_path, media_type if media_type else "")

        if file_path and os.path.exists(file_path):
            with suppress(Exception):
                os.unlink(file_path)

        if success:
            self.state.inc_sent_count()
        return success

    async def process_channel(self, channel_identifier: str):
        log(f"--- کانال {channel_identifier} ---")
        try:
            entity = await self.client.get_entity(channel_identifier)
            key = f"@{entity.username}" if entity.username else str(entity.id)
            last_id = self.state.get_last_id(key)

            if last_id == 0:
                last_msg = await self.client.get_messages(entity, limit=1)
                if last_msg:
                    new_last = last_msg[0].id
                    self.state.set_last_id(key, new_last)
                    self.state.save()
                    log(f"اولین اجرا: آخرین پیام id={new_last} ذخیره شد.")
                else:
                    log(f"کانال {channel_identifier} پیامی ندارد.")
                return

            async for msg in self.client.iter_messages(entity, min_id=last_id, reverse=True, limit=50):
                if msg.id <= last_id:
                    continue
                if isinstance(msg, MessageService):
                    log(f"پیام سرویس id={msg.id} رد شد", "DEBUG")
                    self.state.set_last_id(key, msg.id)
                    continue

                # بررسی وجود رسانه یا متن (با استفاده از msg.text)
                has_media = any([
                    msg.photo, msg.video, msg.voice, msg.audio, msg.sticker,
                    getattr(msg, 'animation', None), msg.document
                ])
                text_content = (msg.text or "").strip()
                if not text_content and not has_media:
                    log(f"پیام کاملاً خالی id={msg.id}", "DEBUG")
                    self.state.set_last_id(key, msg.id)
                    continue

                success = await self.process_message(msg, entity, key)
                if success:
                    log(f"✅ پیام {msg.id} ارسال شد")
                    self.state.set_last_id(key, msg.id)
                else:
                    log(f"❌ ارسال پیام {msg.id} ناموفق – اضافه شدن به Dead Letter")
                    self.state.add_to_dead_letter(key, msg.id)

                await asyncio.sleep(SLEEP_BETWEEN_MESSAGES)

        except FloodWaitError as e:
            log(f"محدودیت تلگرام: {e.seconds} ثانیه صبر", "WARNING")
            await asyncio.sleep(e.seconds)
        except RPCError as e:
            log(f"خطای RPC در کانال {channel_identifier}: {e}", "ERROR")
        except Exception as e:
            log(f"خطای غیرمنتظره در کانال {channel_identifier}: {e}", "ERROR")
            traceback.print_exc()

    async def process_dead_letter(self):
        dead_list = self.state.get_dead_letter()
        if not dead_list:
            return
        log(f"🔁 تلاش مجدد برای {len(dead_list)} پیام ناموفق")
        new_dead = []
        for item in dead_list:
            channel = item["channel"]
            msg_id = item["msg_id"]
            try:
                entity = await self.client.get_entity(channel)
                msg = await self.client.get_messages(entity, ids=msg_id)
                if msg:
                    success = await self.process_message(msg, entity, channel)
                    if success:
                        log(f"🔁 پیام ناموفق {msg_id} مجدداً ارسال شد")
                        continue
            except Exception as e:
                log(f"خطا در بازیابی پیام {msg_id}: {e}", "ERROR")
            new_dead.append(item)
        self.state.data["dead_letter"] = new_dead
        self.state.save()

    async def run(self):
        log("=== راه‌اندازی ربات حرفه‌ای انتقال تلگرام به بله ===")
        if not await self.connect_telegram():
            return
        self.bale.process_admin_commands()

        if self.state.data.get("retry_dead_letter", False):
            await self.process_dead_letter()
            self.state.data["retry_dead_letter"] = False
            self.state.save()

        for chan in SOURCE_CHANNELS:
            if self.state.data.get("skip_current_channel", False):
                self.state.data["skip_current_channel"] = False
                self.state.save()
                log("اسکیپ کانال فعلی درخواست شده، ادامه می‌دهیم...")
                continue
            await self.process_channel(chan)

        self.state.data["stats"]["last_run"] = datetime.now().isoformat()
        self.state.save()
        await self.client.disconnect()
        log("=== پایان اجرا ===")

# ================================ اجرای اصلی ================================
async def main():
    if not all([API_ID, API_HASH, DC_ID, AUTH_KEY_HEX]):
        log("API_ID, API_HASH, DC_ID, AUTH_KEY_HEX الزامی هستند", "ERROR")
        return
    if not BALE_BOT_TOKEN or BALE_CHANNEL_ID == 0:
        log("BALE_BOT_TOKEN یا BALE_CHANNEL_ID تنظیم نشده", "ERROR")
        return
    if not SOURCE_CHANNELS:
        log("هیچ کانال منبعی تعریف نشده", "WARNING")
        return

    state = StateManager()
    bale = BaleClient(BALE_BOT_TOKEN, state)
    bot = TelegramToBaleBot(state, bale)
    await bot.run()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("اسکریپت توسط کاربر متوقف شد", "WARNING")
    except Exception as e:
        log(f"خطای سطح بالا: {e}", "ERROR")
        traceback.print_exc()
