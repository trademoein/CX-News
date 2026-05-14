import asyncio
import aiohttp
import json
import logging
import os
import re
import tempfile
import traceback
from contextlib import suppress
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from telethon import TelegramClient, errors
from telethon.sessions import MemorySession
from telethon.crypto import AuthKey
from telethon.tl.types import Message, MessageService

# ================================ راه‌اندازی لاگینگ حرفه‌ای ================================
logging.basicConfig(
    format="[%(asctime)s] [%(levelname)-5s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ================================ متغیرهای محیطی ================================
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "").strip()
DC_ID = int(os.environ.get("DC_ID", 0))
AUTH_KEY_HEX = os.environ.get("AUTH_KEY_HEX", "").strip()
USER_ID = int(os.environ.get("USER_ID", 0)) if os.environ.get("USER_ID") else None

SOURCE_CHANNELS_JSON = os.environ.get("SOURCE_CHANNELS", "[]")
try:
    SOURCE_CHANNELS = json.loads(SOURCE_CHANNELS_JSON)
except Exception:
    SOURCE_CHANNELS = []
    log.error("SOURCE_CHANNELS نامعتبر، از لیست خالی استفاده می‌شود.")

BALE_BOT_TOKEN = os.environ.get("BALE_BOT_TOKEN", "").strip()
BALE_CHANNEL_ID = int(os.environ.get("BALE_CHANNEL_ID", 0))

ADMIN_TELEGRAM_ID = int(os.environ.get("ADMIN_TELEGRAM_ID", 0)) if os.environ.get("ADMIN_TELEGRAM_ID") else None

SLEEP_BETWEEN_MESSAGES = 1.5
MAX_RETRIES = 3
RETRY_DELAY = 2
MAX_CAPTION_LENGTH = 4000   # حداکثر طول مجاز کپشن در بله (کمی کمتر از محدودیت واقعی)

STATE_FILE = "state.json"

# ================================ مدیریت state محلی ================================
class StateManager:
    def __init__(self):
        self.data = {
            "last_message_ids": {},
            "dead_letter": [],
            "stats": {"total_sent": 0, "total_failed": 0, "last_run": None},
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
            log.error(f"خطا در بارگذاری state: {e}")

    def save(self):
        with open(STATE_FILE, "w") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)

    def get_last_id(self, channel: str) -> int:
        return self.data["last_message_ids"].get(channel, 0)

    def set_last_id(self, channel: str, msg_id: int):
        self.data["last_message_ids"][channel] = msg_id
        self.save()

    def add_to_dead_letter(self, channel: str, msg_id: int):
        self.data["dead_letter"].append({"channel": channel, "msg_id": msg_id, "added_at": datetime.now().isoformat()})
        self.data["stats"]["total_failed"] += 1
        self.save()

    def get_dead_letter(self) -> List[Dict]:
        return self.data["dead_letter"]

    def inc_sent_count(self):
        self.data["stats"]["total_sent"] += 1
        self.save()

    def set_last_run(self):
        self.data["stats"]["last_run"] = datetime.now().isoformat()
        self.save()

    def get_stats(self) -> Dict:
        return self.data["stats"]

# ================================ سشن با آی‌پی ثابت ================================
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
        self._auth_key = AuthKey(data=bytes.fromhex(auth_key_hex))
        if user_id:
            self._user_id = user_id
        log.info(f"FixedIpSession: DC={dc_id} → {server_address}:{port}")

# ================================ ابزارهای کمکی ================================
def clean_lines_with_mentions(text: str) -> str:
    """حذف خطوط حاوی منشن، لینک تلگرام یا عدد بلند"""
    if not text:
        return ""
    lines = text.split('\n')
    new_lines = []
    for line in lines:
        if not re.search(r'@[\w_]+|https?://t\.me/\S+|\b\d{6,}\b', line):
            new_lines.append(line)
    return '\n'.join(new_lines).strip()

def split_long_text(text: str, max_len: int = MAX_CAPTION_LENGTH) -> List[str]:
    """تقسیم متن بلند به چند بخش با حفظ یکپارچگی خطوط"""
    if len(text) <= max_len:
        return [text]
    parts = []
    lines = text.split('\n')
    current_part = ""
    for line in lines:
        if len(current_part) + len(line) + 1 > max_len:
            if current_part:
                parts.append(current_part.strip())
            current_part = line
        else:
            if current_part:
                current_part += "\n" + line
            else:
                current_part = line
    if current_part:
        parts.append(current_part.strip())
    return parts

def build_footer(post_link: str) -> str:
    """ساخت فوتر با لینک مارکداون"""
    return f"\n\n[منبع]({post_link})\n@CX_NEWS | اخبار اقتصادی"

# ================================ کلاینت ناهمگام بله ================================
class BaleAsyncClient:
    def __init__(self, token: str):
        self.token = token
        self.base_url = f"https://tapi.bale.ai/bot{token}/"
        self._session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._session:
            await self._session.close()

    async def _request(self, method: str, payload: Dict, files: Optional[Dict] = None) -> bool:
        """ارسال درخواست با تلاش مجدد (exponential backoff)"""
        url = self.base_url + method
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                if files:
                    data = aiohttp.FormData()
                    for k, v in payload.items():
                        data.add_field(k, str(v))
                    for field, file_path in files.items():
                        data.add_field(field, open(file_path, "rb"), filename=os.path.basename(file_path))
                    async with self._session.post(url, data=data, timeout=60) as resp:
                        if resp.status == 429:
                            retry_after = int(resp.headers.get("Retry-After", 5))
                            log.warning(f"محدودیت نرخ بله: {retry_after} ثانیه صبر")
                            await asyncio.sleep(retry_after)
                            continue
                        result = await resp.json()
                        return result.get("ok", False)
                else:
                    async with self._session.post(url, json=payload, timeout=15) as resp:
                        if resp.status == 429:
                            retry_after = int(resp.headers.get("Retry-After", 5))
                            await asyncio.sleep(retry_after)
                            continue
                        result = await resp.json()
                        return result.get("ok", False)
            except Exception as e:
                log.warning(f"خطا در تلاش {attempt} برای {method}: {e}")
            if attempt < MAX_RETRIES:
                wait = RETRY_DELAY * (2 ** (attempt - 1))
                await asyncio.sleep(wait)
        return False

    async def send_message(self, chat_id: int, text: str) -> bool:
        if not self.token:
            return False
        # تقسیم متن بلند به چند پیام
        parts = split_long_text(text)
        success = True
        for part in parts:
            payload = {"chat_id": chat_id, "text": part, "parse_mode": "Markdown"}
            ok = await self._request("sendMessage", payload)
            if not ok:
                success = False
            await asyncio.sleep(0.5)  # فاصله بین بخش‌ها
        return success

    async def send_media(self, chat_id: int, caption: str, file_path: str, media_type: str) -> bool:
        method_map = {
            "photo": "sendPhoto", "video": "sendVideo", "voice": "sendVoice",
            "audio": "sendAudio", "sticker": "sendSticker",
            "animation": "sendAnimation", "document": "sendDocument"
        }
        method = method_map.get(media_type)
        if not method:
            return False
        payload = {"chat_id": chat_id}
        if media_type != "sticker":
            # اگر کپشن بلند است، باید تقسیم شود – اما برای رسانه فقط یک کپشن می‌توان فرستاد
            # در صورت بلندی بیش از حد، از ارسال کپشن صرف‌نظر می‌کنیم و هشدار می‌دهیم
            if len(caption) > MAX_CAPTION_LENGTH:
                log.warning(f"کپشن رسانه {media_type} طولانی است، کوتاه می‌شود.")
                caption = caption[:MAX_CAPTION_LENGTH-10] + "..."
            payload["caption"] = caption
            payload["parse_mode"] = "Markdown"
        # فایل را موقت باز می‌کنیم و می‌فرستیم
        with open(file_path, "rb") as f:
            files = {media_type: f}
            # چون _request فایل را می‌بندد، باید دقت شود – در اینجا با باز کردن دستی، فایل در انتهای بلاک بسته می‌شود
            # اما _request به صورت async کار می‌کند و ممکن است فایل قبل از اتمام بسته شود. برای سادگی، فایل را داخل _request باز می‌کنیم.
            # بهتر است باز کردن فایل را به _request بسپاریم. برای این کار، files را به صورت دیکشنری با مسیر فایل می‌دهیم و داخل _request باز شود.
            # در _request فعلاً فایل را باز می‌کند. بنابراین اینجا فقط مسیر را پاس می‌دهیم.
            pass
        # روش صحیح: مسیر فایل را به _request بدهیم تا خودش باز کند
        return await self._request_with_file(method, payload, file_path, media_type)

    async def _request_with_file(self, method: str, payload: Dict, file_path: str, file_field: str) -> bool:
        url = self.base_url + method
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                data = aiohttp.FormData()
                for k, v in payload.items():
                    data.add_field(k, str(v))
                with open(file_path, "rb") as f:
                    data.add_field(file_field, f, filename=os.path.basename(file_path))
                    async with self._session.post(url, data=data, timeout=60) as resp:
                        if resp.status == 429:
                            retry_after = int(resp.headers.get("Retry-After", 5))
                            await asyncio.sleep(retry_after)
                            continue
                        result = await resp.json()
                        return result.get("ok", False)
            except Exception as e:
                log.warning(f"خطا در تلاش {attempt} ارسال فایل: {e}")
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY * (2 ** (attempt - 1)))
        return False

# ================================ ربات اصلی ================================
class TelegramToBaleBot:
    def __init__(self, state_manager: StateManager, bale_client: BaleAsyncClient):
        self.state = state_manager
        self.bale = bale_client
        self.client: Optional[TelegramClient] = None
        self.errors_during_run = []  # جمع‌آوری خطاها برای گزارش به ادمین

    async def notify_admin(self, error_text: str):
        """ارسال پیام خطا به ادمین تلگرام (در صورت وجود)"""
        if ADMIN_TELEGRAM_ID and self.client and await self.client.is_user_authorized():
            try:
                await self.client.send_message(ADMIN_TELEGRAM_ID, f"⚠️ **خطا در ربات:**\n{error_text[:400]}")
                log.info(f"خطا به ادمین ارسال شد: {error_text[:100]}")
            except Exception as e:
                log.error(f"نتوانستیم خطا را به ادمین ارسال کنیم: {e}")
        else:
            log.error(f"ادمین تنظیم نشده یا کلاینت آماده نیست: {error_text[:200]}")

    async def send_final_report(self):
        """ارسال خلاصه آمار و خطاها به ادمین در پایان اجرا"""
        if not ADMIN_TELEGRAM_ID or not self.client:
            return
        stats = self.state.get_stats()
        dead_count = len(self.state.get_dead_letter())
        msg = (
            f"✅ **گزارش پایان اجرای ربات**\n"
            f"📤 ارسال موفق: {stats['total_sent']}\n"
            f"❌ پیام‌های ناموفق (dead letter): {dead_count}\n"
            f"🕒 آخرین اجرا: {stats.get('last_run', 'ندارد')}\n"
        )
        if self.errors_during_run:
            msg += f"\n⚠️ **خطاهای رخ داده:**\n" + "\n".join(f"- {e[:100]}" for e in self.errors_during_run[-5:])
        await self.client.send_message(ADMIN_TELEGRAM_ID, msg)
        log.info("گزارش نهایی به ادمین ارسال شد.")

    async def connect_telegram(self) -> bool:
        try:
            session = FixedIpSession(DC_ID, AUTH_KEY_HEX, USER_ID)
            self.client = TelegramClient(session, API_ID, API_HASH)
            await self.client.connect()
            if not await self.client.is_user_authorized():
                log.error("احراز هویت نشد. اطلاعات نشست معتبر نیست.")
                await self.notify_admin("اتصال به تلگرام ناموفق: احراز هویت نشد.")
                return False
            log.info("✅ اتصال و احراز هویت موفق")
            return True
        except Exception as e:
            log.error(f"خطا در اتصال به تلگرام: {e}")
            await self.notify_admin(f"خطای اتصال به تلگرام: {str(e)[:200]}")
            return False

    async def download_media_safe(self, msg: Message) -> Optional[Tuple[str, str]]:
        """دانلود رسانه و برگرداندن (مسیر_فایل, نوع_رسانه)"""
        media_attr = None
        media_type = None
        suffix = ".bin"
        if msg.photo:
            media_attr = msg.photo
            media_type = "photo"
            suffix = ".jpg"
        elif msg.video:
            media_attr = msg.video
            media_type = "video"
            suffix = ".mp4"
        elif msg.voice:
            media_attr = msg.voice
            media_type = "voice"
            suffix = ".ogg"
        elif msg.audio:
            media_attr = msg.audio
            media_type = "audio"
            suffix = ".mp3"
        elif msg.sticker:
            media_attr = msg.sticker
            media_type = "sticker"
            suffix = ".webp"
        elif getattr(msg, 'animation', None):
            media_attr = msg.animation
            media_type = "animation"
            suffix = ".mp4"
        elif msg.document:
            media_attr = msg.document
            media_type = "document"
            for attr in getattr(msg.document, 'attributes', []):
                if hasattr(attr, 'file_name') and attr.file_name:
                    suffix = os.path.splitext(attr.file_name)[1]
                    break
            else:
                suffix = ".bin"
        else:
            return None, None

        fd, temp_path = tempfile.mkstemp(suffix=suffix)
        os.close(fd)
        try:
            await self.client.download_media(media_attr, temp_path)
            return temp_path, media_type
        except Exception as e:
            log.error(f"خطا در دانلود رسانه: {e}")
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            return None, None

    async def send_one_message_to_bale(self, chat_id: int, caption: str, file_path: Optional[str] = None, media_type: str = "") -> bool:
        if file_path and media_type:
            return await self.bale.send_media(chat_id, caption, file_path, media_type)
        else:
            return await self.bale.send_message(chat_id, caption)

    async def process_single_message(self, msg: Message, channel_entity, channel_key: str) -> bool:
        raw_text = msg.text or ""
        cleaned_text = clean_lines_with_mentions(raw_text)

        # لینک استاندارد با استفاده از خود Telethon
        try:
            post_link = await self.client.get_message_link(msg)
        except Exception:
            # fallback به روش دستی
            if hasattr(channel_entity, 'username') and channel_entity.username:
                post_link = f"https://t.me/{channel_entity.username}/{msg.id}"
            else:
                entity_id = str(channel_entity.id).lstrip('-100')
                post_link = f"https://t.me/c/{entity_id}/{msg.id}"

        footer = build_footer(post_link)
        final_caption = (cleaned_text + footer) if cleaned_text else footer

        # دانلود رسانه در صورت وجود
        file_path, media_type = await self.download_media_safe(msg)
        success = await self.send_one_message_to_bale(BALE_CHANNEL_ID, final_caption, file_path, media_type)

        if file_path and os.path.exists(file_path):
            with suppress(Exception):
                os.unlink(file_path)

        if success:
            self.state.inc_sent_count()
        return success

    async def process_channel(self, channel_identifier: str):
        log.info(f"--- شروع پردازش کانال: {channel_identifier} ---")
        try:
            entity = await self.client.get_entity(channel_identifier)
            key = f"@{entity.username}" if entity.username else str(entity.id)
            last_id = self.state.get_last_id(key)

            # اولین اجرا: ذخیره آخرین id
            if last_id == 0:
                last_msg = await self.client.get_messages(entity, limit=1)
                if last_msg:
                    new_last = last_msg[0].id
                    self.state.set_last_id(key, new_last)
                    log.info(f"اولین اجرا: آخرین پیام id={new_last} ذخیره شد.")
                else:
                    log.warning(f"کانال {channel_identifier} پیامی ندارد.")
                return

            # دریافت پیام‌های جدید (با پشتیبانی از آلبوم)
            # پیام‌ها را بر اساس grouped_id دسته‌بندی می‌کنیم
            messages = []
            async for msg in self.client.iter_messages(entity, min_id=last_id, reverse=True):
                if msg.id <= last_id:
                    continue
                messages.append(msg)
                if len(messages) >= 200:  # هر بار حداکثر ۲۰۰ پیام بگیر تا از محدودیت خارج نشویم
                    break

            if not messages:
                log.info(f"پیام جدیدی در {channel_identifier} یافت نشد.")
                return

            # دسته‌بندی بر اساس grouped_id
            groups: Dict[int, List[Message]] = {}
            solo_messages = []
            for msg in messages:
                if isinstance(msg, MessageService):
                    continue
                gid = getattr(msg, 'grouped_id', None)
                if gid:
                    groups.setdefault(gid, []).append(msg)
                else:
                    solo_messages.append(msg)

            # پردازش پیام‌های تکی
            for msg in solo_messages:
                if await self._process_and_update(msg, entity, key):
                    continue  # موفقیت یا شکست در داخل تابع مدیریت می‌شود

            # پردازش گروه‌ها (آلبوم)
            for gid, group_msgs in groups.items():
                # ارسال هر یک از اعضای گروه به صورت جداگانه با فاصله
                log.info(f"آلبوم با {len(group_msgs)} رسانه یافت شد (grouped_id={gid})")
                for msg in sorted(group_msgs, key=lambda m: m.id):
                    await self._process_and_update(msg, entity, key)
                    await asyncio.sleep(SLEEP_BETWEEN_MESSAGES)

        except errors.FloodWaitError as e:
            log.warning(f"FloodWait در تلگرام: {e.seconds} ثانیه صبر")
            await self.notify_admin(f"FloodWait در کانال {channel_identifier}: {e.seconds} ثانیه")
            await asyncio.sleep(e.seconds)
        except errors.RPCError as e:
            log.error(f"خطای RPC در کانال {channel_identifier}: {e}")
            await self.notify_admin(f"خطای RPC در {channel_identifier}: {str(e)[:200]}")
        except Exception as e:
            log.error(f"خطای غیرمنتظره در کانال {channel_identifier}: {e}")
            traceback.print_exc()
            self.errors_during_run.append(f"{channel_identifier}: {str(e)[:100]}")
            await self.notify_admin(f"خطا در {channel_identifier}: {str(e)[:200]}")

    async def _process_and_update(self, msg: Message, entity, key: str) -> bool:
        """پردازش یک پیام و به‌روزرسانی last_id در صورت موفقیت/شکست"""
        try:
            # بررسی وجود محتوا
            has_media = bool(msg.photo or msg.video or msg.voice or msg.audio or msg.sticker or
                             getattr(msg, 'animation', None) or msg.document)
            text_content = (msg.text or "").strip()
            if not text_content and not has_media:
                log.debug(f"پیام خالی id={msg.id} رد شد")
                self.state.set_last_id(key, msg.id)
                return True

            success = await self.process_single_message(msg, entity, key)
            if success:
                log.info(f"✅ پیام {msg.id} ارسال شد")
                self.state.set_last_id(key, msg.id)
            else:
                log.error(f"❌ ارسال پیام {msg.id} ناموفق – اضافه شدن به Dead Letter")
                self.state.add_to_dead_letter(key, msg.id)
                self.state.set_last_id(key, msg.id)   # رد کردن برای جلوگیری از تکرار
            return success
        except Exception as e:
            log.error(f"خطا در پردازش پیام {msg.id}: {e}")
            self.state.set_last_id(key, msg.id)
            return False

    async def run(self):
        log.info("=== راه‌اندازی ربات حرفه‌ای انتقال تلگرام به بله ===")
        if not await self.connect_telegram():
            return

        async with self.bale:
            # پردازش کانال‌های منبع
            for chan in SOURCE_CHANNELS:
                await self.process_channel(chan)

            self.state.set_last_run()
            # ارسال گزارش نهایی به ادمین
            await self.send_final_report()

        await self.client.disconnect()
        log.info("=== پایان اجرا ===")

# ================================ اجرای اصلی ================================
async def main():
    if not all([API_ID, API_HASH, DC_ID, AUTH_KEY_HEX]):
        log.error("API_ID, API_HASH, DC_ID, AUTH_KEY_HEX الزامی هستند")
        return
    if not BALE_BOT_TOKEN or BALE_CHANNEL_ID == 0:
        log.error("BALE_BOT_TOKEN یا BALE_CHANNEL_ID تنظیم نشده")
        return
    if not SOURCE_CHANNELS:
        log.warning("هیچ کانال منبعی تعریف نشده")

    state = StateManager()
    bale = BaleAsyncClient(BALE_BOT_TOKEN)
    bot = TelegramToBaleBot(state, bale)
    await bot.run()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("اسکریپت توسط کاربر متوقف شد")
    except Exception as e:
        log.error(f"خطای سطح بالا: {e}")
        traceback.print_exc()
