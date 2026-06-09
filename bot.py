import asyncio
import io
import logging
import os
import time

import requests
from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    filters,
)

import session_store
from shared import change_ai_button, client, config, openai, operator_user
import handler


logger = logging.getLogger(__name__)
background_tasks = []


async def cleanup_sessions(context: ContextTypes.DEFAULT_TYPE):
    now = time.time()
    expiry_seconds = 7 * 24 * 3600
    sessions_to_delete = []

    for session_id, data in list(context.bot_data.items()):
        if not isinstance(session_id, str) or session_id.startswith("_"):
            continue
        if not isinstance(data, dict):
            continue
        if "last_activity" not in data:
            data["last_activity"] = now
            continue
        if now - data["last_activity"] > expiry_seconds:
            sessions_to_delete.append(session_id)

    if not sessions_to_delete:
        return

    for session_id in sessions_to_delete:
        del context.bot_data[session_id]

    await asyncio.to_thread(session_store.delete_sessions, sessions_to_delete)

    topic_index = context.bot_data.get("_topic_session_index", {})
    for topic_id, session_id in list(topic_index.items()):
        if session_id in sessions_to_delete:
            del topic_index[topic_id]

    try:
        handler.forget_sessions(sessions_to_delete)
    except Exception as exc:
        logger.warning("Cleanup session state failed: %s", exc)


async def log_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    if error is None:
        logger.error("Unhandled Telegram update error without exception context")
        return

    error_name = f"{type(error).__module__}.{type(error).__name__}"
    if "RemoteProtocolError" in error_name:
        logger.warning("Temporary Telegram polling connection error: %s", error)
        return

    logger.error(
        "Unhandled Telegram update error: %s",
        error,
        exc_info=(type(error), error, error.__traceback__),
    )


class RuntimeContext:
    def __init__(self, application):
        self.application = application
        self.bot = application.bot
        self.bot_data = application.bot_data


async def restore_sessions(application):
    sessions = await asyncio.to_thread(session_store.load_all_sessions)
    topic_index = {}

    for session_id, session in sessions.items():
        application.bot_data[session_id] = session
        topic_id = session.get("topicId")
        if topic_id is not None:
            topic_index[str(topic_id)] = session_id

    application.bot_data["_topic_session_index"] = topic_index
    if sessions:
        logger.info("Restored %s sessions from SQLite", len(sessions))


async def cleanup_sessions_loop(context: RuntimeContext):
    await asyncio.sleep(60)
    while True:
        try:
            await cleanup_sessions(context)
        except Exception as exc:
            logger.exception("Session cleanup failed: %s", exc)
        await asyncio.sleep(86400)


def log_background_task_result(task):
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        logger.error(
            "Background task %s stopped unexpectedly: %s",
            task.get_name(),
            exc,
            exc_info=(type(exc), exc, exc.__traceback__),
        )


async def start_background_tasks(application):
    application.bot_data.pop("_background_tasks", None)
    await restore_sessions(application)

    runtime_context = RuntimeContext(application)
    global background_tasks
    background_tasks = [
        asyncio.create_task(handler.exec(runtime_context), name="RTM"),
        asyncio.create_task(cleanup_sessions_loop(runtime_context), name="cleanup_sessions"),
    ]
    for task in background_tasks:
        task.add_done_callback(log_background_task_result)


async def stop_background_tasks(application):
    try:
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


async def forward_reply_to_crisp(session_id, session, text):
    now = time.time()
    session["last_activity"] = now
    query = {
        "type": "text",
        "content": text,
        "from": "operator",
        "origin": "chat",
        "user": operator_user(),
    }

    try:
        await asyncio.to_thread(
            client.website.send_message_in_conversation,
            config["crisp"]["website"],
            session_id,
            query,
        )
        await asyncio.to_thread(session_store.touch_session, session_id, now)
        logger.info("Telegram reply forwarded to Crisp session %s", session_id)
    except Exception as exc:
        logger.error("Send operator reply to Crisp failed: %s", exc)


async def onReply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg or msg.chat_id != config["bot"]["groupId"]:
        return
    if msg.from_user and msg.from_user.is_bot:
        return
    if not msg.text:
        return

    topic_id = str(msg.message_thread_id)
    topic_index = context.bot_data.setdefault("_topic_session_index", {})

    if topic_id in topic_index:
        session_id = topic_index[topic_id]
        if session_id is None:
            return

        session = context.bot_data.get(session_id)
        if isinstance(session, dict) and session.get("topicId") == msg.message_thread_id:
            await forward_reply_to_crisp(session_id, session, msg.text)
            return
        topic_index.pop(topic_id, None)

    for candidate_id, candidate_session in list(context.bot_data.items()):
        if not isinstance(candidate_id, str) or candidate_id.startswith("_"):
            continue
        if not isinstance(candidate_session, dict):
            continue
        if candidate_session.get("topicId") == msg.message_thread_id:
            topic_index[topic_id] = candidate_id
            await forward_reply_to_crisp(candidate_id, candidate_session, msg.text)
            return

    topic_index[topic_id] = None
    logger.warning("No Crisp session matched Telegram topic %s", msg.message_thread_id)


EASYIMAGES_API_URL = config.get("easyimages", {}).get("apiUrl", "")
EASYIMAGES_API_TOKEN = config.get("easyimages", {}).get("apiToken", "")


async def handleImage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg:
        return

    if msg.photo:
        file_id = msg.photo[-1].file_id
    elif msg.document and msg.document.mime_type and msg.document.mime_type.startswith("image/"):
        file_id = msg.document.file_id
    else:
        return

    if not EASYIMAGES_API_URL or not EASYIMAGES_API_TOKEN:
        logger.warning("EasyImages is not configured; image reply ignored.")
        await msg.reply_text("图片未发送：EasyImages 图床未配置。")
        return

    try:
        file = await context.bot.get_file(file_id)
        uploaded_url = await asyncio.to_thread(upload_image_to_easyimages, file.file_path)
        session_id = get_target_session_id(context, msg.message_thread_id)

        if not session_id:
            logger.warning("No Crisp session matched Telegram image topic %s", msg.message_thread_id)
            await msg.reply_text("图片未发送：未找到对应的 Crisp 会话。")
            return

        now = time.time()
        context.bot_data[session_id]["last_activity"] = now
        await asyncio.to_thread(session_store.touch_session, session_id, now)
        await asyncio.to_thread(send_markdown_to_client, session_id, f"![Image]({uploaded_url})")
    except Exception as exc:
        logger.error("Handle Telegram image failed: %s", exc)
        try:
            await msg.reply_text("图片发送失败，请稍后重试或联系管理员检查图床配置。")
        except Exception as reply_exc:
            logger.warning("Send image failure notice failed: %s", reply_exc)


def upload_image_to_easyimages(file_url):
    response = requests.get(file_url, timeout=10)
    response.raise_for_status()

    files = {
        "image": ("image.jpg", io.BytesIO(response.content), "image/jpeg"),
        "token": (None, EASYIMAGES_API_TOKEN),
    }
    res = requests.post(EASYIMAGES_API_URL, files=files, timeout=20)
    res_data = res.json()

    if res_data.get("result") == "success":
        return res_data["url"]
    raise RuntimeError(f"EasyImages upload failed: {res_data}")


def get_target_session_id(context, thread_id):
    topic_index = context.bot_data.get("_topic_session_index", {})
    session_id = topic_index.get(str(thread_id))

    if session_id and isinstance(context.bot_data.get(session_id), dict):
        return session_id

    for session_id, session_data in context.bot_data.items():
        if not isinstance(session_id, str) or session_id.startswith("_"):
            continue
        if not isinstance(session_data, dict):
            continue
        if session_data.get("topicId") == thread_id:
            topic_index[str(thread_id)] = session_id
            return session_id

    return None


def send_markdown_to_client(session_id, markdown_link):
    query = {
        "type": "text",
        "content": markdown_link,
        "from": "operator",
        "origin": "chat",
        "user": operator_user(),
    }

    try:
        client.website.send_message_in_conversation(config["crisp"]["website"], session_id, query)
    except Exception as exc:
        logger.error("Send image markdown to Crisp failed: %s", exc)


async def onChange(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return

    if not query.data:
        await query.answer("数据异常")
        return

    if openai is None:
        await query.answer("无法设置此功能")
        return

    data = query.data.split(",")
    if len(data) < 2:
        await query.answer("数据异常")
        return

    session_id = data[0]
    session = context.bot_data.get(session_id)
    if not session:
        await query.answer("会话不存在")
        return

    now = time.time()
    session["last_activity"] = now
    is_ai_enabled = data[1].lower() == "true"
    session["enableAI"] = not is_ai_enabled
    await asyncio.to_thread(session_store.set_enable_ai, session_id, session["enableAI"], now)
    await query.answer()

    try:
        await query.edit_message_reply_markup(change_ai_button(session_id, session["enableAI"]))
    except Exception as exc:
        logger.warning("Edit AI toggle button failed: %s", exc)


def main():
    try:
        asyncio.set_event_loop(asyncio.new_event_loop())

        app = (
            Application.builder()
            .token(config["bot"]["token"])
            .defaults(Defaults(parse_mode="HTML"))
            .job_queue(None)
            .post_init(start_background_tasks)
            .post_shutdown(stop_background_tasks)
            .read_timeout(20)
            .connect_timeout(20)
            .write_timeout(20)
            .build()
        )

        if os.getenv("RUNNER_NAME") is not None:
            return

        app.add_handler(MessageHandler(filters.TEXT, onReply))
        app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handleImage))
        app.add_handler(CallbackQueryHandler(onChange))
        app.add_error_handler(log_error)
        app.run_polling(drop_pending_updates=True, timeout=10)
    except Exception as exc:
        logger.exception("Bot stopped unexpectedly: %s", exc)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
