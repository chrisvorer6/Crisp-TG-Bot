import os
import yaml
import logging
import requests
import socket

# ---------------------------------------------------------
# [补丁] 强制启用 IPv4，防止 IPv6 路由波动导致的 httpx.ReadError
# ---------------------------------------------------------
orig_getaddrinfo = socket.getaddrinfo
def getaddrinfo_ipv4(host, port, family=0, type=0, proto=0, flags=0):
    return orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
socket.getaddrinfo = getaddrinfo_ipv4

from openai import OpenAI
from crisp_api import Crisp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, Defaults, MessageHandler, filters, ContextTypes, CallbackQueryHandler

import handler

# 日志配置
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)

# 1. 加载配置
try:
    with open('config.yml', 'r') as f:
        config = yaml.safe_load(f)
except FileNotFoundError:
    logging.warning('没有找到 config.yml，请复制 config.yml.example 并重命名为 config.yml')
    exit(1)

# 2. 连接 Crisp
try:
    crispCfg = config['crisp']
    client = Crisp()
    client.set_tier("plugin")
    client.authenticate(crispCfg['id'], crispCfg['key'])
    client.plugin.get_connect_account()
    client.website.get_website(crispCfg['website'])
except Exception as error:
    logging.error(f'无法连接 Crisp 服务: {error}')
    exit(1)

# 3. 连接 OpenAI
try:
    openai = OpenAI(api_key=config['openai']['apiKey'], base_url='https://api.openai.com/v1')
    openai.models.list()
except Exception:
    logging.warning('无法连接 OpenAI 服务，智能化回复将不会使用')
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

async def onReply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg or msg.chat_id != config['bot']['groupId']:
        return

    for sessionId in context.bot_data:
        if context.bot_data[sessionId].get('topicId') == msg.message_thread_id:
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
            # Crisp 发送消息 (同步操作，需注意)
            try:
                client.website.send_message_in_conversation(
                    config['crisp']['website'],
                    sessionId,
                    query
                )
            except Exception as e:
                logging.error(f"发送消息到 Crisp 失败: {e}")
            return

# EasyImages 配置
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
        await msg.reply_text("请发送图片文件。")
        return

    try:
        # 获取文件下载 URL
        file = await context.bot.get_file(file_id)
        file_url = file.file_path

        # 上传图片 (增加超时控制，防止卡死)
        uploaded_url = upload_image_to_easyimages(file_url)

        markdown_link = f"![Image]({uploaded_url})"
        session_id = get_target_session_id(context, msg.message_thread_id)
        
        if session_id:
            send_markdown_to_client(session_id, markdown_link)
            await msg.reply_text("图片已成功发送给客户！")
        else:
            await msg.reply_text("未找到对应的 Crisp 会话，无法发送给客户。")

    except Exception as e:
        await msg.reply_text(f"图片处理失败: {e}")
        logging.error(f"图片上传错误: {e}")

def upload_image_to_easyimages(file_url):
    """带超时保护的上传函数"""
    try:
        # 下载图片，增加 timeout
        response = requests.get(file_url, stream=True, timeout=10)
        response.raise_for_status()

        files = {
            'image': ('image.jpg', response.raw, 'image/jpeg'),
            'token': (None, EASYIMAGES_API_TOKEN)
        }
        # 上传图片，增加 timeout
        res = requests.post(EASYIMAGES_API_URL, files=files, timeout=20)
        res.raise_for_status()
        res_data = res.json()

        if res_data.get("result") == "success":
            return res_data["url"]
        else:
            raise Exception(f"图床返回失败: {res_data.get('message')}")
    except requests.exceptions.RequestException as e:
        raise Exception(f"网络连接超时或错误: {e}")

def get_target_session_id(context, thread_id):
    for session_id, session_data in context.bot_data.items():
        if session_data.get('topicId') == thread_id:
            return session_id
    return None

def send_markdown_to_client(session_id, markdown_link):
    try:
        query = {
            "type": "text",
            "content": markdown_link,
            "from": "operator",
            "origin": "chat",
            "user": {
                "nickname": "人工客服",
                "avatar": "https://bpic.51yuansu.com/pic3/cover/03/47/92/65e3b3b1eb909_800.jpg"
            }
        }
        client.website.send_message_in_conversation(
            config['crisp']['website'],
            session_id,
            query
        )
    except Exception as e:
        logging.error(f"同步 Markdown 到 Crisp 失败: {e}")

async def onChange(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query: return

    if openai is None:
        await query.answer('无法设置此功能')
    else:
        data = query.data.split(',')
        session = context.bot_data.get(data[0])
        # 安全替换 eval(): 将 'True'/'False' 字符串转为布尔值
        is_ai_enabled = data[1].lower() == 'true'
        session["enableAI"] = not is_ai_enabled
        
        await query.answer()
        try:
             await query.edit_message_reply_markup(changeButton(data[0], session["enableAI"]))
        except Exception as error:
             logging.error(f"修改按钮失败: {error}")

def main():
    try:
        # 4. 构建 Application 并设置全局异步超时
        app = (
            Application.builder()
            .token(config['bot']['token'])
            .defaults(Defaults(parse_mode='HTML'))
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
        
        # 启动 RTM 任务
        app.job_queue.run_once(handler.exec, 5, name='RTM')
        
        # 5. 启动轮询，设置 API 超时为 10s
        logging.info("Crisp Telegram Bot 正在启动...")
        app.run_polling(drop_pending_updates=True, timeout=10)
        
    except Exception as error:
        logging.error(f'程序启动失败: {error}')
        exit(1)

if __name__ == "__main__":
    main()
