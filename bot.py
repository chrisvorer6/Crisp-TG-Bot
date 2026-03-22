import os
import yaml
import logging
import requests
import socket
import time

orig_getaddrinfo = socket.getaddrinfo
def getaddrinfo_ipv4(host, port, family=0, type=0, proto=0, flags=0):
    return orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
socket.getaddrinfo = getaddrinfo_ipv4

from openai import OpenAI
from crisp_api import Crisp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, Defaults, MessageHandler, filters, ContextTypes, CallbackQueryHandler, PicklePersistence

import handler

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)

try:
    with open('config.yml', 'r') as f:
        config = yaml.safe_load(f)
except FileNotFoundError:
    exit(1)

try:
    crispCfg = config['crisp']
    client = Crisp()
    client.set_tier("plugin")
    client.authenticate(crispCfg['id'], crispCfg['key'])
    client.plugin.get_connect_account()
    client.website.get_website(crispCfg['website'])
except Exception:
    exit(1)

try:
    openai = OpenAI(api_key=config['openai']['apiKey'], base_url='https://api.openai.com/v1')
    openai.models.list()
except Exception:
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
    for session_id, data in context.bot_data.items():
        if now - data.get('last_activity', 0) > expiry_seconds:
            sessions_to_delete.append(session_id)
    if sessions_to_delete:
        for sid in sessions_to_delete:
            del context.bot_data[sid]

async def onReply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg or msg.chat_id != config['bot']['groupId']:
        return
    for sessionId in context.bot_data:
        if context.bot_data[sessionId].get('topicId') == msg.message_thread_id:
            context.bot_data[sessionId]['last_activity'] = time.time()
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
                client.website.send_message_in_conversation(config['crisp']['website'], sessionId, query)
            except Exception:
                pass
            return

EASYIMAGES_API_URL = config.get('easyimages', {}).get('apiUrl', '')
EASYIMAGES_API_TOKEN = config.get('easyimages', {}).get('apiToken', '')

async def handleImage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg: return
    if msg.photo:
        file_id = msg.photo[-1].file_id
    elif msg.document and msg.document.mime_type.startswith('image/'):
        file_id = msg.document.file_id
    else:
        return
    try:
        file = await context.bot.get_file(file_id)
        uploaded_url = upload_image_to_easyimages(file.file_path)
        markdown_link = f"![Image]({uploaded_url})"
        session_id = get_target_session_id(context, msg.message_thread_id)
        if session_id:
            context.bot_data[session_id]['last_activity'] = time.time()
            send_markdown_to_client(session_id, markdown_link)
    except Exception:
        pass

def upload_image_to_easyimages(file_url):
    try:
        response = requests.get(file_url, stream=True, timeout=10)
        response.raise_for_status()
        files = {'image': ('image.jpg', response.raw, 'image/jpeg'), 'token': (None, EASYIMAGES_API_TOKEN)}
        res = requests.post(EASYIMAGES_API_URL, files=files, timeout=20)
        res_data = res.json()
        if res_data.get("result") == "success":
            return res_data["url"]
        raise Exception()
    except Exception:
        raise

def get_target_session_id(context, thread_id):
    for session_id, session_data in context.bot_data.items():
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
    except Exception:
        pass

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
            except Exception:
                 pass

def main():
    try:
        persistence = PicklePersistence(filepath="bot_persistence.pickle")
        app = (
            Application.builder()
            .token(config['bot']['token'])
            .defaults(Defaults(parse_mode='HTML'))
            .persistence(persistence)
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
        app.job_queue.run_once(handler.exec, 5, name='RTM')
        app.job_queue.run_repeating(cleanup_sessions, interval=86400, first=60)
        app.run_polling(drop_pending_updates=True, timeout=10)
    except Exception:
        exit(1)

if __name__ == "__main__":
    main()
