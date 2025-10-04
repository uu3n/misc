# bot.py
import os
import sys
import json
import asyncio
import aiohttp
import shutil
import logging
import datetime
from telethon import TelegramClient, events, errors

# -------------- Logging to console & file --------------
LOG_FILENAME = "forward_bot_local.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILENAME, encoding="utf-8"),
        logging.StreamHandler()
    ]
)

# -------------- Paths / files --------------
CONFIG_FILE = "config.json"
LOCAL_CONFIG_FILE = "local_config.json"
BOT_FILE = os.path.basename(__file__)
BOT_TMP = BOT_FILE + ".new"
BOT_BAK = BOT_FILE + ".bak"

# -------------- Load local host name (create if missing) --------------
if not os.path.exists(LOCAL_CONFIG_FILE):
    host_name = input("Enter host name (for this machine): ").strip() or "host"
    with open(LOCAL_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({"host_name": host_name}, f, ensure_ascii=False, indent=4)
    logging.info(f"Created {LOCAL_CONFIG_FILE} with host_name={host_name}")

with open(LOCAL_CONFIG_FILE, "r", encoding="utf-8") as f:
    local_cfg = json.load(f)
HOST_NAME = local_cfg.get("host_name", "host")

# -------------- Load central config (must exist locally first time) --------------
if not os.path.exists(CONFIG_FILE):
    logging.error(f"{CONFIG_FILE} not found. Please create it (see README) and restart.")
    sys.exit(1)

with open(CONFIG_FILE, "r", encoding="utf-8") as f:
    config = json.load(f)

# required fields in config.json (fill these before running)
API_ID = config.get("api_id")
API_HASH = config.get("api_hash")
SESSION_NAME = config.get("session_name", "forward_session")
SOURCE_CHANNEL = config.get("source_channel")
TARGET_CHANNEL = config.get("target_channel")
LOG_CHANNEL = config.get("log_channel")
CONFIG_RAW_URL = config.get("config_raw_url", "")   # raw URL to config.json on GitHub
BOT_RAW_URL = config.get("bot_raw_url", "")         # raw URL to bot.py on GitHub
AUTO_UPDATE_HOURS = config.get("auto_update_hours", 3)
ADMIN_USER_IDS = config.get("admin_user_ids", [])   # list of admin telegram user ids allowed to run update/upgrade
VIDEOS = config.get("youtube_links", [])
RETRY_MAX = config.get("max_retry", 3)

# sanity checks
if not API_ID or not API_HASH:
    logging.error("api_id / api_hash missing in config.json. Fill them and restart.")
    sys.exit(1)
if SOURCE_CHANNEL is None or TARGET_CHANNEL is None or LOG_CHANNEL is None:
    logging.error("source_channel / target_channel / log_channel must be set in config.json.")
    sys.exit(1)

# -------------- Telethon client --------------
client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

# -------------- Helper: send log to log channel and local file --------------
async def send_log_to_channel(text: str):
    try:
        await client.send_message(LOG_CHANNEL, text)
    except Exception as e:
        logging.warning(f"Failed to push log to log_channel: {e}")

def local_log(text: str):
    logging.info(text)

async def combined_log(text: str):
    local_log(text)
    # try send to channel but don't crash on failure
    try:
        await send_log_to_channel(text)
    except Exception as e:
        local_log(f"(log->channel failed) {e}")

# -------------- Forward with retry --------------
async def forward_with_retry(message_event):
    msg = message_event.message
    msg_id = getattr(msg, "id", "unknown")
    for attempt in range(1, RETRY_MAX + 1):
        try:
            # forward the message preserving media/text
            await client.forward_messages(TARGET_CHANNEL, msg)
            timestamp = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
            text = (f"[{timestamp}] ✅ Message Sent\n"
                    f"Source: {SOURCE_CHANNEL} | Target: {TARGET_CHANNEL}\n"
                    f"Message ID: {msg_id}\n"
                    f"Host: {HOST_NAME}")
            await combined_log(text)
            return
        except errors.FloodWaitError as e:
            text = (f"[{datetime.datetime.utcnow()}] ❌ FloodWait on attempt {attempt} for msg {msg_id}: wait {e.seconds}s")
            await combined_log(text)
            await asyncio.sleep(e.seconds + 1)
        except Exception as e:
            text = (f"[{datetime.datetime.utcnow()}] ❌ Forward attempt {attempt} failed for msg {msg_id}: {type(e).__name__}: {e}")
            await combined_log(text)
            await asyncio.sleep(2 ** attempt)  # exponential backoff
    # after retries failed
    text = (f"[{datetime.datetime.utcnow()}] ❌ Message permanently failed after {RETRY_MAX} attempts. Message ID: {msg_id}\nHost: {HOST_NAME}")
    await combined_log(text)

# -------------- Event: new message in source channel --------------
@client.on(events.NewMessage(chats=SOURCE_CHANNEL))
async def on_new_source_message(event):
    # quick delegation to not block event loop
    asyncio.create_task(forward_with_retry(event))

# -------------- Command: /start --------------
@client.on(events.NewMessage(pattern=r"^/start$"))
async def cmd_start(event):
    start_text = config.get("welcome_message", "Welcome! This bot forwards messages.")
    await event.respond(start_text)

# -------------- Command: /about --------------
@client.on(events.NewMessage(pattern=r"^/about$"))
async def cmd_about(event):
    about_text = config.get("about_message", "Forward Bot")
    await event.respond(about_text)

# -------------- Command: /ping --------------
@client.on(events.NewMessage(pattern=r"^/ping$"))
async def cmd_ping(event):
    await event.respond(f"Pong! Host: {HOST_NAME} ✅")

# -------------- Command: /hosts --------------
@client.on(events.NewMessage(pattern=r"^/hosts$"))
async def cmd_hosts(event):
    # local-only host status (we don't have global registry here)
    await event.respond(f"Host: {HOST_NAME} 🟢 ")

# -------------- Command: /videos --------------
@client.on(events.NewMessage(pattern=r"^/videos$"))
async def cmd_videos(event):
    if not VIDEOS:
        await event.respond("No videos configured.")
        return
    text = "🎬 Videos:\n" + "\n".join(f"{i+1}. {v}" for i, v in enumerate(VIDEOS))
    await event.respond(text)

# -------------- Helper: fetch text from URL (aiohttp) --------------
async def fetch_text(url: str):
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(url, timeout=30) as resp:
                if resp.status == 200:
                    return await resp.text()
                else:
                    raise RuntimeError(f"HTTP {resp.status}")
    except Exception as e:
        raise

# -------------- Command: /update (update config.json from GitHub raw) --------------
@client.on(events.NewMessage(pattern=r"^/update$"))
async def cmd_update(event):
    sender = await event.get_sender()
    sender_id = getattr(sender, "id", None)
    if ADMIN_USER_IDS and sender_id not in ADMIN_USER_IDS:
        await event.reply("❌ Not authorized.")
        return
    if not CONFIG_RAW_URL:
        await event.reply("❌ config_raw_url not configured.")
        return
    await event.reply("🔄 Fetching config from GitHub...")
    try:
        text = await fetch_text(CONFIG_RAW_URL)
        # validate JSON
        new_cfg = json.loads(text)
        # write locally
        with open(CONFIG_FILE + ".tmp", "w", encoding="utf-8") as f:
            json.dump(new_cfg, f, ensure_ascii=False, indent=4)
        os.replace(CONFIG_FILE + ".tmp", CONFIG_FILE)
        # apply changes in-memory
        global config, SOURCE_CHANNEL, TARGET_CHANNEL, LOG_CHANNEL, VIDEOS, RETRY_MAX, AUTO_UPDATE_HOURS
        config = new_cfg
        SOURCE_CHANNEL = config.get("source_channel", SOURCE_CHANNEL)
        TARGET_CHANNEL = config.get("target_channel", TARGET_CHANNEL)
        LOG_CHANNEL = config.get("log_channel", LOG_CHANNEL)
        VIDEOS = config.get("youtube_links", VIDEOS)
        RETRY_MAX = config.get("max_retry", RETRY_MAX)
        AUTO_UPDATE_HOURS = config.get("auto_update_hours", AUTO_UPDATE_HOURS)
        await event.reply("✅ Config updated and applied.")
        await combined_log(f"[{datetime.datetime.utcnow()}] 🔄 Config updated via /update by {sender_id} on host {HOST_NAME}")
    except Exception as e:
        await event.reply(f"❌ Update failed: {e}")

# -------------- Safe upgrade (bot.py) --------------
async def safe_upgrade_from_raw(url: str):
    if not url:
        raise RuntimeError("No BOT_RAW_URL configured.")
    # download to tmp
    try:
        text = await fetch_text(url)
    except Exception as e:
        raise RuntimeError(f"Download failed: {e}")
    if not text or len(text) < 20:
        raise RuntimeError("Downloaded content too small / invalid.")
    # write to tmp file
    with open(BOT_TMP, "w", encoding="utf-8") as f:
        f.write(text)
    # backup current and replace
    try:
        if os.path.exists(BOT_BAK):
            os.remove(BOT_BAK)
        os.replace(BOT_FILE, BOT_BAK)
        os.replace(BOT_TMP, BOT_FILE)
    except Exception as e:
        # attempt rollback
        if os.path.exists(BOT_BAK) and not os.path.exists(BOT_FILE):
            os.replace(BOT_BAK, BOT_FILE)
        raise RuntimeError(f"Replace failed: {e}")
    return True

# -------------- Command: /upgrade (update bot.py from GitHub raw) --------------
@client.on(events.NewMessage(pattern=r"^/upgrade$"))
async def cmd_upgrade(event):
    sender = await event.get_sender()
    sender_id = getattr(sender, "id", None)
    if ADMIN_USER_IDS and sender_id not in ADMIN_USER_IDS:
        await event.reply("❌ Not authorized.")
        return
    if not BOT_RAW_URL:
        await event.reply("❌ bot_raw_url not configured.")
        return
    await event.reply("🔄 Fetching new bot code from GitHub...")
    try:
        await combined_log(f"🔄 Upgrade requested by {sender_id} on {HOST_NAME}")
        await safe_upgrade_from_raw(BOT_RAW_URL)
        await event.reply("✅ Bot code upgraded. Restarting process to apply changes...")
        await combined_log("🔄 Restarting process after upgrade...")
        # restart process
        python = sys.executable
        os.execv(python, [python] + sys.argv)
    except Exception as e:
        await event.reply(f"❌ Upgrade failed: {e}")
        await combined_log(f"❌ Upgrade failed: {e}")

# -------------- Background auto-check tasks (config + optional auto-upgrade) --------------
async def auto_tasks():
    # check config every AUTO_UPDATE_HOURS hours
    while True:
        try:
            await asyncio.sleep(AUTO_UPDATE_HOURS * 3600)
            if CONFIG_RAW_URL:
                try:
                    text = await fetch_text(CONFIG_RAW_URL)
                    new_cfg = json.loads(text)
                    # if different from local, replace local and apply
                    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                        local_cfg_text = f.read()
                    if json.dumps(new_cfg, sort_keys=True) != json.dumps(json.loads(local_cfg_text), sort_keys=True):
                        with open(CONFIG_FILE + ".tmp", "w", encoding="utf-8") as f:
                            json.dump(new_cfg, f, ensure_ascii=False, indent=4)
                        os.replace(CONFIG_FILE + ".tmp", CONFIG_FILE)
                        # apply new values
                        global config, SOURCE_CHANNEL, TARGET_CHANNEL, LOG_CHANNEL, VIDEOS, RETRY_MAX
                        config = new_cfg
                        SOURCE_CHANNEL = config.get("source_channel", SOURCE_CHANNEL)
                        TARGET_CHANNEL = config.get("target_channel", TARGET_CHANNEL)
                        LOG_CHANNEL = config.get("log_channel", LOG_CHANNEL)
                        VIDEOS = config.get("youtube_links", VIDEOS)
                        RETRY_MAX = config.get("max_retry", RETRY_MAX)
                        await combined_log(f"[{datetime.datetime.utcnow()}] 🔄 Auto-applied new config from GitHub on host {HOST_NAME}")
                except Exception as e:
                    await combined_log(f"[{datetime.datetime.utcnow()}] ❌ Auto-config check failed: {e}")
            # optionally check bot code auto-upgrade if desired: commented out by default
            # if BOT_RAW_URL:
            #     try:
            #         # implement safe auto-upgrade logic if you want (be careful)
            #         pass
            #     except Exception as e:
            #         await combined_log(f"Auto-upgrade failed: {e}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            await combined_log(f"[{datetime.datetime.utcnow()}] Auto-tasks outer exception: {e}")
            await asyncio.sleep(60)

# -------------- Graceful shutdown via admin command /stop --------------
@client.on(events.NewMessage(pattern=r"^/stop$"))
async def cmd_stop(event):
    sender = await event.get_sender()
    sender_id = getattr(sender, "id", None)
    if ADMIN_USER_IDS and sender_id not in ADMIN_USER_IDS:
        await event.reply("❌ Not authorized.")
        return
    await event.reply("🛑 Shutting down gracefully...")
    await combined_log(f"[{datetime.datetime.utcnow()}] Shutdown requested by {sender_id} on host {HOST_NAME}")
    await client.disconnect()
    # allow process to exit
    asyncio.get_event_loop().stop()

# -------------- Main run --------------
async def main():
    try:
        await client.start()
        await combined_log(f"[{datetime.datetime.utcnow()}] 🟢 Bot started on host {HOST_NAME}")
        # start auto tasks
        asyncio.create_task(auto_tasks())
        # run until disconnected
        await client.run_until_disconnected()
    except Exception as e:
        logging.exception(f"Fatal error in main: {e}")
        raise

if __name__ == "__main__":
    asyncio.run(main())
