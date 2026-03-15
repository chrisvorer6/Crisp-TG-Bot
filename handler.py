import bot
import json
import base64
import socketio
import requests
import logging
import asyncio
from telegram.ext import ContextTypes

# 继承 bot.py 的配置和对象
config = bot.config
client = bot.client
openai = bot.openai
changeButton = bot.changeButton
groupId = config["bot"]["groupId"]
websiteId = config["crisp"]["website"]
payload = config["openai"]["payload"]

# 日志配置
logger = logging.getLogger(__name__)

def getKey(content: str):
    if len(config.get("autoreply", {})) > 0:
        for x in config["autoreply"]:
            keyword = x.split("|")
            for key in keyword:
                if key in content:
                    return True, config["autoreply"][x]
    return False, None

def getMetas(sessionId):
    """获取会话元数据，增加异常保护"""
    try:
        # Crisp SDK 是同步请求，在此处捕获潜在的超时或网络错误
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
        logger.error(f"获取 Metas 失败 (Session: {sessionId}): {e}")
    
    return '无额外信息'

async def createSession(data):
    bot_obj = callbackContext.bot
    bot_data = callbackContext.bot_data
    session_id = data["session_id"]
    session = bot_data.get(session_id)

    metas = getMetas(session_id)
    if session is None:
        enableAI = False if openai is None else True
        try:
            # 在 Telegram 中创建话题
            topic = await bot_obj.create_forum_topic(groupId, data["user"]["nickname"])
            msg = await bot_obj.send_message(
                groupId,
                metas,
                message_thread_id=topic.message_thread_id,
                reply_markup=changeButton(session_id, enableAI)
            )
            bot_data[session_id] = {
                'topicId': topic.message_thread_id,
                'messageId': msg.message_id,
                'enableAI': enableAI
            }
        except Exception as e:
            logger.error(f"创建 Telegram 话题失败: {e}")
    else:
        try:
            # 更新已存在的话题信息
            await bot_obj.edit_message_text(metas, groupId, session['messageId'])
        except Exception:
            pass # 消息内容未变化时 edit 会报错，直接忽略

async def sendMessage(data):
    bot_obj = callbackContext.bot
    bot_data = callbackContext.bot_data
    session_id = data["session_id"]
    session = bot_data.get(session_id)

    if not session:
        return

    # 尝试标记已读
    try:
        client.website.mark_messages_read_in_conversation(websiteId, session_id,
            {"from": "user", "origin": "chat", "fingerprints": [data["fingerprint"]]}
        )
    except:
        pass

    if data["type"] == "text":
        flow = ['📠<b>消息推送</b>','']
        flow.append(f"🧾<b>消息内容</b>：{data['content']}")

        result, autoreply = getKey(data["content"])
        if result:
            flow.append(f"\n💡<b>自动回复</b>：{autoreply}")
        elif openai is not None and session.get("enableAI"):
            try:
                # 增加 15s 超时防止 OpenAI 接口卡死导致整个 Bot 假死
                response = openai.chat.completions.create(
                    model="gpt-3.5-turbo",
                    messages=[
                        {"role": "system", "content": payload},
                        {"role": "user", "content": data["content"]}
                    ],
                    timeout=15 
                )
                autoreply = response.choices[0].message.content
                flow.append(f"\n💡<b>自动回复</b>：{autoreply}")
            except Exception as e:
                logger.error(f"AI 生成回复失败: {e}")
                autoreply = None
        
        if autoreply:
            query = {
                "type": "text",
                "content": autoreply,
                "from": "operator",
                "origin": "chat",
                "user": {
                    "nickname": '智能客服',
                    "avatar": 'https://img.ixintu.com/download/jpg/20210125/8bff784c4e309db867d43785efde1daf_512_512.jpg'
                }
            }
            try:
                client.website.send_message_in_conversation(websiteId, session_id, query)
            except Exception as e:
                logger.error(f"推送到 Crisp 失败: {e}")

        await bot_obj.send_message(
            groupId,
            '\n'.join(flow),
            message_thread_id=session["topicId"]
        )
    elif data["type"] == "file" and "image" in str(data["content"].get("type", "")):
        try:
            await bot_obj.send_photo(
                groupId,
                data["content"]["url"],
                message_thread_id=session["topicId"]
            )
        except Exception as e:
            logger.error(f"转发图片失败: {e}")

# --- 极致保守的重连策略 ---
sio = socketio.AsyncClient(
    reconnection=True, 
    reconnection_attempts=0,     # 无限重连，但受下方延迟参数限制
    reconnection_delay=10,        # 初始等待 10s 再重连
    reconnection_delay_max=60,    # 最长等待 60s
    randomization_factor=0.5,     # 随机抖动，防止固定频率访问
    logger=False, 
    engineio_logger=False
)

@sio.on("connect")
async def connect():
    logger.info("Crisp RTM 连接成功！")
    await sio.emit("authentication", {
        "tier": "plugin",
        "username": config["crisp"]["id"],
        "password": config["crisp"]["key"],
        "events": ["message:send", "session:set_data"]
    })

@sio.on("unauthorized")
async def unauthorized(data):
    logger.error(f'授权失败！请检查 Crisp ID 和 Key: {data}')

@sio.event
async def disconnect():
    logger.warning("RTM 连接已断开，系统将根据策略自动重连...")

@sio.on("message:send")
async def messageForward(data):
    if data["website_id"] != websiteId:
        return
    await createSession(data)
    await sendMessage(data)

def getCrispConnectEndpoints():
    try:
        url = "https://api.crisp.chat/v1/plugin/connect/endpoints"
        authtier = base64.b64encode(
            (config["crisp"]["id"] + ":" + config["crisp"]["key"]).encode("utf-8")
        ).decode("utf-8")
        headers = {"X-Crisp-Tier": "plugin", "Authorization": "Basic " + authtier}
        # 增加 timeout 防止请求卡死
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        return response.json().get("data").get("socket").get("app")
    except Exception as e:
        logger.error(f"无法获取 Crisp 端点: {e}")
        return None

# --- 核心入口：带守护逻辑 ---
async def exec(context: ContextTypes.DEFAULT_TYPE):
    global callbackContext
    callbackContext = context
    
    logger.info("RTM 守护进程已启动...")
    
    while True:
        try:
            endpoint = getCrispConnectEndpoints()
            if endpoint:
                if not sio.connected:
                    await sio.connect(
                        endpoint,
                        transports="websocket",
                        wait_timeout=30,
                    )
                await sio.wait()
            else:
                logger.warning("获取端点失败，30秒后重试...")
                await asyncio.sleep(30)
        except Exception as e:
            logger.error(f"RTM 运行中发生错误: {e}")
            await asyncio.sleep(20) # 异常发生后，强制冷却 20s
