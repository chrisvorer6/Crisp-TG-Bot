import bot
import base64
import socketio
import requests
import logging
import asyncio
import time
from telegram.ext import ContextTypes
from telegram.error import BadRequest

config = bot.config
client = bot.client
openai = bot.openai
changeButton = bot.changeButton
groupId = config["bot"]["groupId"]
websiteId = config["crisp"]["website"]
payload = config.get("openai", {}).get("payload", "")
openai_model = config.get("openai", {}).get("model", "gpt-4o-mini")

logger = logging.getLogger(__name__)
session_locks = {}

def get_session_lock(session_id):
    lock = session_locks.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        session_locks[session_id] = lock
    return lock

def getKey(content: str):
    if len(config.get("autoreply", {})) > 0:
        for x in config["autoreply"]:
            keyword = x.split("|")
            for key in keyword:
                if key in content:
                    return True, config["autoreply"][x]
    return False, None

def getMetas(sessionId):
    try:
        metas = client.website.get_conversation_metas(websiteId, sessionId)
        flow = ['📠<b>Crisp消息推送</b>','']
        if metas.get("email"):
            flow.append(f'📧<b>电子邮箱</b>：{metas["email"]}')
        
        if metas.get("data"):
            meta_data = metas["data"]
            if "Plan" in meta_data:
                flow.append(f"🪪<b>使用套餐</b>：{meta_data['Plan']}")
            if "UsedTraffic" in meta_data and "AllTraffic" in meta_data:
                flow.append(f"🗒<b>流量信息</b>：{meta_data['UsedTraffic']} / {meta_data['AllTraffic']}")
        
        if len(flow) > 2:
            return '\n'.join(flow)
    except Exception as e:
        logger.error(f"Metas Error: {e}")
    
    return '无额外信息'

async def rebuildTopic(bot_obj, bot_data, session_id, session, nickname, metas):
    enableAI = session.get("enableAI", False if openai is None else True)
    topic = await bot_obj.create_forum_topic(groupId, nickname)
    msg = await bot_obj.send_message(
        groupId,
        metas,
        message_thread_id=topic.message_thread_id,
        reply_markup=changeButton(session_id, enableAI)
    )
    bot_data[session_id] = {
        **session,
        'topicId': topic.message_thread_id,
        'messageId': msg.message_id,
        'enableAI': enableAI,
        'nickname': nickname,
        'last_activity': time.time()
    }
    return bot_data[session_id]

async def createSession(context, data):
    bot_obj = context.bot
    bot_data = context.bot_data
    session_id = data["session_id"]
    session = bot_data.get(session_id)

    metas = await asyncio.to_thread(getMetas, session_id)
    if session is None:
        try:
            session = {}
            nickname = data.get("user", {}).get("nickname", "未知用户")
            await rebuildTopic(bot_obj, bot_data, session_id, session, nickname, metas)
        except Exception as e:
            logger.error(f"Create Topic Error: {e}")
    else:
        session.setdefault('last_activity', time.time())
        try:
            await bot_obj.edit_message_text(
                metas,
                groupId,
                session['messageId'],
                reply_markup=changeButton(session_id, session.get("enableAI", False if openai is None else True))
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                logger.error(f"Edit Message Error: {e}")
        except Exception as e:
            logger.warning(f"Edit Message Unknown Error: {e}")

async def sendMessage(context, data):
    bot_obj = context.bot
    bot_data = context.bot_data
    session_id = data["session_id"]
    session = bot_data.get(session_id)

    if not session:
        return
    session['last_activity'] = time.time()

    try:
        await asyncio.to_thread(
            client.website.mark_messages_read_in_conversation,
            websiteId,
            session_id,
            {"from": "user", "origin": "chat", "fingerprints": [data.get("fingerprint")]}
        )
    except Exception as e:
        logger.warning(f"Mark Crisp messages read failed: {e}")

    if data.get("type") == "text":
        flow = ['📠<b>消息推送</b>','']
        content = data.get("content", "")
        flow.append(f"🧾<b>消息内容</b>：{content}")

        result, autoreply = getKey(content)
        if result:
            flow.append(f"\n💡<b>自动回复</b>：{autoreply}")
        elif openai is not None and session.get("enableAI"):
            try:
                response = await asyncio.to_thread(
                    openai.chat.completions.create,
                    model=openai_model,
                    messages=[
                        {"role": "system", "content": payload},
                        {"role": "user", "content": content}
                    ],
                    timeout=15
                )
                autoreply = response.choices[0].message.content
                flow.append(f"\n💡<b>自动回复</b>：{autoreply}")
            except Exception as e:
                logger.error(f"AI Error: {e}")
                autoreply = None
        
        if autoreply:
            query = {
                "type": "text", "content": autoreply, "from": "operator", "origin": "chat",
                "user": {"nickname": '智能客服', "avatar": 'https://img.ixintu.com/download/jpg/20210125/8bff784c4e309db867d43785efde1daf_512_512.jpg'}
            }
            try:
                await asyncio.to_thread(client.website.send_message_in_conversation, websiteId, session_id, query)
            except Exception as e:
                logger.error(f"Push to Crisp Error: {e}")

        text_content = '\n'.join(flow)
        try:
            await bot_obj.send_message(
                groupId,
                text_content,
                message_thread_id=session["topicId"]
            )
        except BadRequest as e:
            if "Message thread not found" in str(e):
                try:
                    nickname = session.get('nickname') or data.get("user", {}).get("nickname", "未知用户")
                    metas = await asyncio.to_thread(getMetas, session_id)
                    session = await rebuildTopic(bot_obj, bot_data, session_id, session, nickname, metas)
                    await bot_obj.send_message(groupId, text_content, message_thread_id=session["topicId"])
                except Exception as ex:
                    logger.error(f"Rebuild Topic Error: {ex}")
            else:
                logger.error(f"Send Message Error: {e}")

    elif data.get("type") == "file" and "image" in str(data.get("content", {}).get("type", "")):
        photo_url = data["content"].get("url")
        if not photo_url:
            return
        try:
            await bot_obj.send_photo(
                groupId,
                photo_url,
                message_thread_id=session["topicId"]
            )
        except BadRequest as e:
            if "Message thread not found" in str(e):
                try:
                    nickname = session.get('nickname') or data.get("user", {}).get("nickname", "未知用户")
                    metas = await asyncio.to_thread(getMetas, session_id)
                    session = await rebuildTopic(bot_obj, bot_data, session_id, session, nickname, metas)
                    await bot_obj.send_photo(groupId, photo_url, message_thread_id=session["topicId"])
                except Exception as ex:
                    logger.error(f"Image Rebuild Error: {ex}")
            else:
                logger.error(f"Send Photo Error: {e}")
        except Exception as e:
            logger.error(f"Unknown Photo Error: {e}")

sio = socketio.AsyncClient(
    reconnection=True, 
    reconnection_attempts=0,     
    reconnection_delay=10,        
    reconnection_delay_max=60,    
    randomization_factor=0.5,     
    logger=False, 
    engineio_logger=False
)

@sio.on("connect")
async def connect():
    logger.info("Crisp RTM Connected")
    await sio.emit("authentication", {
        "tier": "plugin",
        "username": config["crisp"]["id"],
        "password": config["crisp"]["key"],
        "events": ["message:send", "session:set_data"]
    })

@sio.on("unauthorized")
async def unauthorized(data):
    logger.error(f'Auth Failed: {data}')

@sio.event
async def disconnect():
    logger.warning("RTM Disconnected")

@sio.on("message:send")
async def messageForward(data):
    if data.get("website_id") != websiteId:
        return
    if not data.get("session_id"):
        return
    async with get_session_lock(data["session_id"]):
        await createSession(callbackContext, data)
        await sendMessage(callbackContext, data)

def getCrispConnectEndpoints():
    try:
        url = "https://api.crisp.chat/v1/plugin/connect/endpoints"
        authtier = base64.b64encode(
            (config["crisp"]["id"] + ":" + config["crisp"]["key"]).encode("utf-8")
        ).decode("utf-8")
        headers = {"X-Crisp-Tier": "plugin", "Authorization": "Basic " + authtier}
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        return response.json().get("data").get("socket").get("app")
    except Exception as e:
        logger.error(f"Get Endpoints Error: {e}")
        return None

async def exec(context: ContextTypes.DEFAULT_TYPE):
    global callbackContext
    callbackContext = context
    
    logger.info("RTM Daemon Started")
    
    while True:
        try:
            endpoint = await asyncio.to_thread(getCrispConnectEndpoints)
            if endpoint:
                if not sio.connected:
                    await sio.connect(
                        endpoint,
                        transports="websocket",
                        wait_timeout=30,
                    )
                await sio.wait()
            else:
                await asyncio.sleep(30)
        except asyncio.CancelledError:
            logger.info("RTM Daemon Stopping")
            raise
        except Exception as e:
            logger.error(f"RTM Exec Error: {e}")
            await asyncio.sleep(20)

async def shutdown():
    if sio.connected:
        try:
            await asyncio.wait_for(sio.disconnect(), timeout=3)
            await asyncio.sleep(0.1)
        except asyncio.TimeoutError:
            logger.warning("RTM disconnect timed out")
        except Exception as e:
            logger.warning(f"RTM disconnect failed: {e}")
