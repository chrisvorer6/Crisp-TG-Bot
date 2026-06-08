import os
import sys
import asyncio
import yaml
import logging
import requests
import socket
import time

sys.modules.setdefault("bot", sys.modules[__name__])

from openai import OpenAI
from crisp_api import Crisp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, Defaults, MessageHandler, filters, ContextTypes, CallbackQueryHandler, PicklePersistence

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)
background_tasks = []

try:
    with open('config.yml', 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f) or {}
except FileNotFoundError:
    logger.error("config.yml not found. Copy config.yml.example to config.yml and update it.")
    exit(1)
except yaml.YAMLError as exc:
    logger.error("config.yml is invalid: %s", exc)
    exit(1)

required_config = {
    "bot": ("token", "groupId"),
    "crisp": ("id", "key", "website"),
}
missing_config = [
    f"{section}.{key}"
    for section, keys in required_config.items()
    for key in keys
    if not config.get(section, {}).get(key)
]
if missing_config:
    logger.error("Missing required config values: %s", ", ".join(missing_config))
    exit(1)

if config.get('network', {}).get('preferIPv4', True):
    orig_getaddrinfo = socket.getaddrinfo

    def getaddrinfo_prefer_ipv4(host, port, family=0, type=0, proto=0, flags=0):
        addresses = orig_getaddrinfo(host, port, family, type, proto, flags)
        return sorted(addresses, key=lambda item: item[0] != socket.AF_INET)

    socket.getaddrinfo = getaddrinfo_prefer_ipv4

try:
    crispCfg = config['crisp']
    client = Crisp()
    client.set_tier("plugin")
    client.authenticate(crispCfg['id'], crispCfg['key'])
    client.plugin.get_connect_account()
    client.website.get_website(crispCfg['website'])
except Exception as exc:
    logger.error("Crisp authentication failed: %s", exc)
    exit(1)

try:
    openai_api_key = config.get('openai', {}).get('apiKey')
    if not openai_api_key:
        raise ValueError("openai.apiKey is not configured")
    openai_base_url = config.get('openai', {}).get('baseUrl', 'https://api.openai.com/v1')
    openai = OpenAI(api_key=openai_api_key, base_url=openai_base_url)
    openai.models.list()
except Exception as exc:
    logger.warning("OpenAI auto-reply disabled: %s", exc)
    openai = None

def changeButton(sessionId, boolean):
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(
                text='关闭 AI 回复' if boolean else '打开 AI 回复',
                callback_data=f'{sessionId},{boolean}'
                )
            ]
        ]
    )

async def cleanup_sessions(context: ContextTypes.DEFAULT_TYPE):
    now = time.time()
    expiry_seconds = 7 * 24 * 3600 
    sessions_to_delete = []
    for session_id, data in list(context.bot_data.items()):
        if not isinstance(data, dict):
            continue
        if 'last_activity' not in data:
            data['last_activity'] = now
            continue
        if now - data['last_activity'] > expiry_seconds:
            sessions_to_delete.append(session_id)
    if sessions_to_delete:
        for sid in sessions_to_delete:
            del context.bot_data[sid]

async def log_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled Telegram update error: %s", context.error)

class RuntimeContext:
    def __init__(self, application):
        self.application = application
        self.bot = application.bot
        self.bot_data = application.bot_data

async def cleanup_sessions_loop(context: RuntimeContext):
    await asyncio.sleep(60)
    while True:
        await cleanup_sessions(context)
        await asyncio.sleep(86400)

async def start_background_tasks(application):
    import handler

    application.bot_data.pop('_background_tasks', None)
    context = RuntimeContext(application)
    global background_tasks
    background_tasks = [
        asyncio.create_task(handler.exec(context), name='RTM'),
        asyncio.create_task(cleanup_sessions_loop(context), name='cleanup_sessions'),
    ]

async def stop_background_tasks(application):
    try:
        import handler
        await asyncio.wait_for(handler.shutdown(), timeout=5)
    except Exception as exc:
        logger.warning("RTM shutdown failed: %s", exc)

    global background_tasks
    tasks = background_tasks
    background_tasks = []
    for task in tasks:
        task.cancel()
    if tasks:
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=5)
        except asyncio.TimeoutError:
            logger.warning("Timed out waiting for background tasks to stop")

async def onReply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg or msg.chat_id != config['bot']['groupId']:
        return
    if msg.from_user and msg.from_user.is_bot:
        return
    if not msg.text:
        return
    for sessionId, session in list(context.bot_data.items()):
        if not isinstance(session, dict):
            continue
        if session.get('topicId') == msg.message_thread_id:
            session['last_activity'] = time.time()
            query = {
                "type": "text",
                "content": msg.text,
                "from": "operator",
                "origin": "chat",
                "user": {
                    "nickname": '人工客服',
                    "avatar": 'https://bpic.51yuansu.com/pic3/cover/03/47/92/65e3b3b1eb909_800.jpg'
                }
            }
            try:
                await asyncio.to_thread(
                    client.website.send_message_in_conversation,
                    config['crisp']['website'],
                    sessionId,
                    query
                )
                logger.info("Telegram reply forwarded to Crisp session %s", sessionId)
            except Exception as exc:
                logger.error("Send operator reply to Crisp failed: %s", exc)
            return
    logger.warning("No Crisp session matched Telegram topic %s", msg.message_thread_id)

EASYIMAGES_API_URL = config.get('easyimages', {}).get('apiUrl', '')
EASYIMAGES_API_TOKEN = config.get('easyimages', {}).get('apiToken', '')

async def handleImage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg: return
    if msg.photo:
        file_id = msg.photo[-1].file_id
    elif msg.document and msg.document.mime_type and msg.document.mime_type.startswith('image/'):
        file_id = msg.document.file_id
    else:
        return
    if not EASYIMAGES_API_URL or not EASYIMAGES_API_TOKEN:
        logger.warning("EasyImages is not configured; image reply ignored.")
        return
    try:
        file = await context.bot.get_file(file_id)
        uploaded_url = await asyncio.to_thread(upload_image_to_easyimages, file.file_path)
        markdown_link = f"![Image]({uploaded_url})"
        session_id = get_target_session_id(context, msg.message_thread_id)
        if session_id:
            context.bot_data[session_id]['last_activity'] = time.time()
            await asyncio.to_thread(send_markdown_to_client, session_id, markdown_link)
    except Exception as exc:
        logger.error("Handle Telegram image failed: %s", exc)

def upload_image_to_easyimages(file_url):
    try:
        response = requests.get(file_url, stream=True, timeout=10)
        response.raise_for_status()
        files = {'image': ('image.jpg', response.raw, 'image/jpeg'), 'token': (None, EASYIMAGES_API_TOKEN)}
        res = requests.post(EASYIMAGES_API_URL, files=files, timeout=20)
        res_data = res.json()
        if res_data.get("result") == "success":
            return res_data["url"]
        raise RuntimeError(f"EasyImages upload failed: {res_data}")
    except Exception:
        raise

def get_target_session_id(context, thread_id):
    for session_id, session_data in context.bot_data.items():
        if not isinstance(session_data, dict):
            continue
        if session_data.get('topicId') == thread_id:
            return session_id
    return None

def send_markdown_to_client(session_id, markdown_link):
    try:
        query = {
            "type": "text", "content": markdown_link, "from": "operator", "origin": "chat",
            "user": {"nickname": "人工客服", "avatar": "https://bpic.51yuansu.com/pic3/cover/03/47/92/65e3b3b1eb909_800.jpg"}
        }
        client.website.send_message_in_conversation(config['crisp']['website'], session_id, query)
    except Exception as exc:
        logger.error("Send image markdown to Crisp failed: %s", exc)

async def onChange(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query: return
    if openai is None:
        await query.answer('无法设置此功能')
    else:
        data = query.data.split(',')
        session = context.bot_data.get(data[0])
        if session:
            session['last_activity'] = time.time()
            is_ai_enabled = data[1].lower() == 'true'
            session["enableAI"] = not is_ai_enabled
            await query.answer()
            try:
                 await query.edit_message_reply_markup(changeButton(data[0], session["enableAI"]))
            except Exception as exc:
                 logger.warning("Edit AI toggle button failed: %s", exc)

def main():
    try:
        asyncio.set_event_loop(asyncio.new_event_loop())

        persistence = PicklePersistence(filepath="bot_persistence.pickle")
        app = (
            Application.builder()
            .token(config['bot']['token'])
            .defaults(Defaults(parse_mode='HTML'))
            .persistence(persistence)
            .job_queue(None)
            .post_init(start_background_tasks)
            .post_shutdown(stop_background_tasks)
            .read_timeout(20)
            .connect_timeout(20)
            .write_timeout(20)
            .build()
        )
        if os.getenv('RUNNER_NAME') is not None:
            return
        app.add_handler(MessageHandler(filters.TEXT, onReply))
        app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handleImage))
        app.add_handler(CallbackQueryHandler(onChange))
        app.add_error_handler(log_error)
        app.run_polling(drop_pending_updates=True, timeout=10)
    except Exception as exc:
        logger.exception("Bot stopped unexpectedly: %s", exc)
        exit(1)

if __name__ == "__main__":
    main()
