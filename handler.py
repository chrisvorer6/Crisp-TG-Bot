import asyncio
import base64
import html
import logging
import time

import requests
import socketio
from telegram.error import BadRequest
from telegram.ext import ContextTypes

import session_store
from shared import ai_user, change_ai_button, client, config, openai


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


def forget_sessions(session_ids):
    for session_id in session_ids:
        session_locks.pop(session_id, None)


def get_topic_index(bot_data):
    return bot_data.setdefault("_topic_session_index", {})


def get_content_dict(data):
    content = data.get("content")
    return content if isinstance(content, dict) else {}


def get_user_nickname(data):
    user = data.get("user")
    if not isinstance(user, dict):
        return "未知用户"
    return str(user.get("nickname") or "未知用户")[:128]


def getKey(content: str):
    for keyword_group, reply in config.get("autoreply", {}).items():
        for keyword in keyword_group.split("|"):
            if keyword and keyword in content:
                return True, reply
    return False, None


def getMetas(sessionId):
    try:
        metas = client.website.get_conversation_metas(websiteId, sessionId) or {}
        flow = ["📠<b>Crisp 消息推送</b>", ""]

        if metas.get("email"):
            flow.append(f"📧<b>电子邮箱</b>：{html.escape(str(metas['email']))}")

        meta_data = metas.get("data")
        if isinstance(meta_data, dict):
            if "Plan" in meta_data:
                flow.append(f"🪪<b>使用套餐</b>：{html.escape(str(meta_data['Plan']))}")
            if "UsedTraffic" in meta_data and "AllTraffic" in meta_data:
                used_traffic = html.escape(str(meta_data["UsedTraffic"]))
                all_traffic = html.escape(str(meta_data["AllTraffic"]))
                flow.append(f"🗒<b>流量信息</b>：{used_traffic} / {all_traffic}")

        if len(flow) > 2:
            return "\n".join(flow)
    except Exception as exc:
        logger.error("Metas Error: %s", exc)

    return "无额外信息"


async def rebuildTopic(bot_obj, bot_data, session_id, session, nickname, metas):
    enableAI = session.get("enableAI", False if openai is None else True)
    topic = await bot_obj.create_forum_topic(groupId, str(nickname or "未知用户")[:128])
    msg = await bot_obj.send_message(
        groupId,
        metas,
        message_thread_id=topic.message_thread_id,
        reply_markup=change_ai_button(session_id, enableAI),
    )

    now = time.time()
    bot_data[session_id] = {
        **session,
        "topicId": topic.message_thread_id,
        "messageId": msg.message_id,
        "enableAI": enableAI,
        "nickname": nickname,
        "last_activity": now,
    }

    topic_index = get_topic_index(bot_data)
    old_topic_id = session.get("topicId")
    if old_topic_id is not None:
        topic_index.pop(str(old_topic_id), None)
    topic_index[str(topic.message_thread_id)] = session_id

    await asyncio.to_thread(
        session_store.upsert_session,
        session_id,
        topic.message_thread_id,
        msg.message_id,
        enableAI,
        nickname,
        now,
    )
    return bot_data[session_id]


async def createSession(context, data):
    bot_obj = context.bot
    bot_data = context.bot_data
    session_id = data["session_id"]
    session = bot_data.get(session_id)
    metas = await asyncio.to_thread(getMetas, session_id)

    if session is None:
        try:
            return await rebuildTopic(bot_obj, bot_data, session_id, {}, get_user_nickname(data), metas)
        except Exception as exc:
            logger.error("Create Topic Error: %s", exc)
            return None

    now = time.time()
    session.setdefault("last_activity", now)
    if session.get("topicId") is not None:
        get_topic_index(bot_data)[str(session["topicId"])] = session_id

    if not session.get("messageId"):
        try:
            return await rebuildTopic(
                bot_obj,
                bot_data,
                session_id,
                session,
                session.get("nickname") or get_user_nickname(data),
                metas,
            )
        except Exception as exc:
            logger.error("Rebuild Topic Metadata Error: %s", exc)
            return session

    try:
        await bot_obj.edit_message_text(
            metas,
            groupId,
            session["messageId"],
            reply_markup=change_ai_button(session_id, session.get("enableAI", False if openai is None else True)),
        )
    except BadRequest as exc:
        if "Message is not modified" not in str(exc):
            logger.error("Edit Message Error: %s", exc)
    except Exception as exc:
        logger.warning("Edit Message Unknown Error: %s", exc)

    return session


async def sendMessage(context, data):
    bot_obj = context.bot
    bot_data = context.bot_data
    session_id = data["session_id"]
    session = bot_data.get(session_id)

    if not session:
        return

    now = time.time()
    session["last_activity"] = now
    await asyncio.to_thread(session_store.touch_session, session_id, now)

    try:
        await asyncio.to_thread(
            client.website.mark_messages_read_in_conversation,
            websiteId,
            session_id,
            {"from": "user", "origin": "chat", "fingerprints": [data.get("fingerprint")]},
        )
    except Exception as exc:
        logger.warning("Mark Crisp messages read failed: %s", exc)

    if data.get("type") == "text":
        await sendTextMessage(bot_obj, bot_data, session_id, session, data)
    elif data.get("type") == "file" and "image" in str(get_content_dict(data).get("type", "")):
        await sendImageMessage(bot_obj, bot_data, session_id, session, data)


async def sendTextMessage(bot_obj, bot_data, session_id, session, data):
    flow = ["📠<b>消息推送</b>", ""]
    content = str(data.get("content") or "")
    flow.append(f"💬<b>消息内容</b>：{html.escape(content)}")
    autoreply = None

    result, autoreply = getKey(content)
    if result:
        flow.append(f"\n💡<b>自动回复</b>：{html.escape(str(autoreply))}")
    elif openai is not None and session.get("enableAI"):
        try:
            response = await asyncio.to_thread(
                openai.chat.completions.create,
                model=openai_model,
                messages=[
                    {"role": "system", "content": payload},
                    {"role": "user", "content": content},
                ],
                timeout=15,
            )
            autoreply = response.choices[0].message.content
            flow.append(f"\n💡<b>自动回复</b>：{html.escape(str(autoreply))}")
        except Exception as exc:
            logger.error("AI Error: %s", exc)
            autoreply = None

    if autoreply:
        query = {
            "type": "text",
            "content": autoreply,
            "from": "operator",
            "origin": "chat",
            "user": ai_user(),
        }
        try:
            await asyncio.to_thread(client.website.send_message_in_conversation, websiteId, session_id, query)
        except Exception as exc:
            logger.error("Push to Crisp Error: %s", exc)

    text_content = "\n".join(flow)
    try:
        await bot_obj.send_message(groupId, text_content, message_thread_id=session["topicId"])
    except BadRequest as exc:
        if "Message thread not found" in str(exc):
            await rebuildAndSendText(bot_obj, bot_data, session_id, session, data, text_content)
        else:
            logger.error("Send Message Error: %s", exc)
    except Exception as exc:
        logger.error("Unknown Message Error: %s", exc)


async def rebuildAndSendText(bot_obj, bot_data, session_id, session, data, text_content):
    try:
        nickname = session.get("nickname") or get_user_nickname(data)
        metas = await asyncio.to_thread(getMetas, session_id)
        session = await rebuildTopic(bot_obj, bot_data, session_id, session, nickname, metas)
        await bot_obj.send_message(groupId, text_content, message_thread_id=session["topicId"])
    except Exception as exc:
        logger.error("Rebuild Topic Error: %s", exc)


async def sendImageMessage(bot_obj, bot_data, session_id, session, data):
    photo_url = get_content_dict(data).get("url")
    if not photo_url:
        return

    try:
        await bot_obj.send_photo(groupId, photo_url, message_thread_id=session["topicId"])
    except BadRequest as exc:
        if "Message thread not found" in str(exc):
            await rebuildAndSendPhoto(bot_obj, bot_data, session_id, session, data, photo_url)
        else:
            logger.error("Send Photo Error: %s", exc)
    except Exception as exc:
        logger.error("Unknown Photo Error: %s", exc)


async def rebuildAndSendPhoto(bot_obj, bot_data, session_id, session, data, photo_url):
    try:
        nickname = session.get("nickname") or get_user_nickname(data)
        metas = await asyncio.to_thread(getMetas, session_id)
        session = await rebuildTopic(bot_obj, bot_data, session_id, session, nickname, metas)
        await bot_obj.send_photo(groupId, photo_url, message_thread_id=session["topicId"])
    except Exception as exc:
        logger.error("Image Rebuild Error: %s", exc)


def getCrispConnectEndpoints():
    try:
        url = "https://api.crisp.chat/v1/plugin/connect/endpoints"
        authtier = base64.b64encode(
            (config["crisp"]["id"] + ":" + config["crisp"]["key"]).encode("utf-8")
        ).decode("utf-8")
        headers = {"X-Crisp-Tier": "plugin", "Authorization": "Basic " + authtier}
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json().get("data") or {}
        socket_data = data.get("socket") or {}
        return socket_data.get("app")
    except Exception as exc:
        logger.error("Get Endpoints Error: %s", exc)
        return None


class RTMDaemon:
    def __init__(self, context):
        self.context = context
        self.stopping = False
        self.sio = socketio.AsyncClient(
            reconnection=True,
            reconnection_attempts=0,
            reconnection_delay=10,
            reconnection_delay_max=60,
            randomization_factor=0.5,
            logger=False,
            engineio_logger=False,
        )
        self.sio.on("connect", handler=self.connect)
        self.sio.on("unauthorized", handler=self.unauthorized)
        self.sio.on("message:send", handler=self.messageForward)
        self.sio.on("disconnect", handler=self.disconnect)

    async def connect(self):
        logger.info("Crisp RTM Connected")
        try:
            await self.sio.emit(
                "authentication",
                {
                    "tier": "plugin",
                    "username": config["crisp"]["id"],
                    "password": config["crisp"]["key"],
                    "events": ["message:send"],
                },
            )
        except Exception as exc:
            logger.error("RTM authentication emit failed: %s", exc)

    async def unauthorized(self, data):
        logger.error("Auth Failed: %s", data)

    async def disconnect(self):
        if self.stopping:
            logger.info("RTM Disconnected")
        else:
            logger.warning("RTM Disconnected")

    async def messageForward(self, data):
        try:
            if data.get("website_id") != websiteId:
                return
            session_id = data.get("session_id")
            if not session_id:
                return
            async with get_session_lock(session_id):
                if await createSession(self.context, data) is None:
                    return
                await sendMessage(self.context, data)
        except Exception as exc:
            logger.exception("Forward Crisp message failed: %s", exc)

    async def run(self):
        logger.info("RTM Daemon Started")
        while True:
            try:
                endpoint = await asyncio.to_thread(getCrispConnectEndpoints)
                if endpoint:
                    if not self.sio.connected:
                        await self.sio.connect(endpoint, transports="websocket", wait_timeout=30)
                    await self.sio.wait()
                else:
                    await asyncio.sleep(30)
            except asyncio.CancelledError:
                self.stopping = True
                logger.info("RTM Daemon Stopping")
                raise
            except Exception as exc:
                logger.error("RTM Exec Error: %s", exc)
                await asyncio.sleep(20)

    async def shutdown(self):
        self.stopping = True
        if self.sio.connected:
            try:
                await asyncio.wait_for(self.sio.disconnect(), timeout=3)
                await asyncio.sleep(0.1)
            except asyncio.TimeoutError:
                logger.warning("RTM disconnect timed out")
            except Exception as exc:
                logger.warning("RTM disconnect failed: %s", exc)


rtm_daemon = None


async def exec(context: ContextTypes.DEFAULT_TYPE):
    global rtm_daemon
    rtm_daemon = RTMDaemon(context)
    await rtm_daemon.run()


async def shutdown():
    if rtm_daemon is not None:
        await rtm_daemon.shutdown()
