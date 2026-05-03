import asyncio
import json
import os
import re
import tempfile
import requests
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.sessions import MemorySession
from telethon.crypto import AuthKey

# ======================== خواندن از محیط ========================
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "").strip()
DC_ID = int(os.environ.get("DC_ID", 0))
AUTH_KEY_HEX = os.environ.get("AUTH_KEY_HEX", "").strip()
USER_ID = int(os.environ.get("USER_ID", 0))

SOURCE_CHANNELS_JSON = os.environ.get("SOURCE_CHANNELS", "[]")
try:
    SOURCE_CHANNELS = json.loads(SOURCE_CHANNELS_JSON)
except:
    SOURCE_CHANNELS = []

BALE_BOT_TOKEN = os.environ.get("BALE_BOT_TOKEN", "").strip()
BALE_CHANNEL_ID = int(os.environ.get("BALE_CHANNEL_ID", 0))

STATE_FILE = "state.json"
SLEEP_BETWEEN_MESSAGES = 1

# ======================== کلاس سشن سفارشی ========================
class CustomSession(MemorySession):
    def __init__(self, dc_id, auth_key_hex, user_id=None):
        super().__init__()
        self._dc_id = dc_id
        # تبدیل hex به bytes
        auth_key_bytes = bytes.fromhex(auth_key_hex)
        self._auth_key = AuthKey(data=auth_key_bytes)
        if user_id:
            self._user_id = user_id

# ======================== توابع کمکی ========================
def clean_telegram_mentions(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r'@[\w_]+', '', text)
    text = re.sub(r'https?://t\.me/\S+', '', text)
    text = re.sub(r'\[\d+\]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def send_text_to_bale(text: str) -> bool:
    url = f"https://tapi.bale.ai/bot{BALE_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": BALE_CHANNEL_ID, "text": text, "parse_mode": "HTML"}
    try:
        resp = requests.post(url, json=payload, timeout=15)
        return resp.status_code == 200 and resp.json().get("ok")
    except:
        return False

def send_photo_to_bale(photo_path: str, caption: str = "") -> bool:
    url = f"https://tapi.bale.ai/bot{BALE_BOT_TOKEN}/sendPhoto"
    try:
        with open(photo_path, 'rb') as f:
            files = {'photo': f}
            data = {'chat_id': BALE_CHANNEL_ID, 'caption': caption}
            resp = requests.post(url, data=data, files=files, timeout=30)
        return resp.status_code == 200 and resp.json().get("ok")
    except:
        return False

def send_video_to_bale(video_path: str, caption: str = "") -> bool:
    url = f"https://tapi.bale.ai/bot{BALE_BOT_TOKEN}/sendVideo"
    try:
        with open(video_path, 'rb') as f:
            files = {'video': f}
            data = {'chat_id': BALE_CHANNEL_ID, 'caption': caption}
            resp = requests.post(url, data=data, files=files, timeout=60)
        return resp.status_code == 200 and resp.json().get("ok")
    except:
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
    # اعتبارسنجی
    if not API_ID or not API_HASH or not DC_ID or not AUTH_KEY_HEX or not USER_ID:
        print("❌ یکی از متغیرهای API_ID, API_HASH, DC_ID, AUTH_KEY_HEX, USER_ID تنظیم نشده.")
        return
    if not BALE_BOT_TOKEN or not BALE_CHANNEL_ID:
        print("❌ BALE_BOT_TOKEN یا BALE_CHANNEL_ID تنظیم نشده.")
        return
    if not SOURCE_CHANNELS:
        print("⚠️ هیچ کانال منبعی تعریف نشده.")
        return

    # ایجاد session سفارشی
    session = CustomSession(DC_ID, AUTH_KEY_HEX, USER_ID)
    client = TelegramClient(session, API_ID, API_HASH)
    await client.connect()

    if not await client.is_user_authorized():
        print("❌ اطلاعات احراز هویت معتبر نیست. AUTH_KEY_HEX یا DC_ID اشتباه است.")
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

            async for msg in client.iter_messages(entity, min_id=last_id, reverse=True, limit=20):
                if msg.id <= last_id:
                    continue

                raw_text = msg.text or msg.caption or ""
                cleaned = clean_telegram_mentions(raw_text)

                if entity.username:
                    post_link = f"https://t.me/{entity.username}/{msg.id}"
                else:
                    post_link = f"https://t.me/c/{str(entity.id)[4:]}/{msg.id}"

                footer = f"\n\nمنبع(<a href='{post_link}'>لینک</a>)\n@CX_NEWS | اخبار اقتصادی"
                final_caption = (cleaned + footer) if cleaned else footer.strip()

                success = False
                temp_file = None

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
                    full_text = f"{raw_text}\n\n{footer}" if raw_text else footer
                    success = send_text_to_bale(full_text)

                if success:
                    print(f"✅ ارسال شد: {post_link}")
                    new_last_ids[chat_id] = msg.id
                else:
                    print(f"❌ ارسال نشد: {post_link}")

                await asyncio.sleep(SLEEP_BETWEEN_MESSAGES)

        except FloodWaitError as e:
            print(f"⚠️ محدودیت تلگرام: {e.seconds} ثانیه صبر.")
            await asyncio.sleep(e.seconds)
        except Exception as e:
            print(f"⚠️ خطا در کانال {channel_identifier}: {e}")

    state["last_message_ids"] = new_last_ids
    save_state(state)
    await client.disconnect()
    print("🏁 پایان اسکریپت.")

if __name__ == "__main__":
    asyncio.run(main())
