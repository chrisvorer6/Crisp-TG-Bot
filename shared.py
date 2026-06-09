import logging
import socket

import yaml
from crisp_api import Crisp
from openai import OpenAI
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import session_store


def configure_logging():
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    class SuppressEngineIOPacketQueueFilter(logging.Filter):
        def filter(self, record):
            return "packet queue is empty, aborting" not in record.getMessage()

    packet_queue_filter = SuppressEngineIOPacketQueueFilter()
    for log_name in ("engineio", "engineio.client"):
        logging.getLogger(log_name).addFilter(packet_queue_filter)
    for handler in logging.getLogger().handlers:
        handler.addFilter(packet_queue_filter)


configure_logging()
logger = logging.getLogger(__name__)


def load_config(path="config.yml"):
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded_config = yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.error("%s not found. Copy config.yml.example to config.yml and update it.", path)
        raise SystemExit(1)
    except yaml.YAMLError as exc:
        logger.error("%s is invalid: %s", path, exc)
        raise SystemExit(1)

    required_config = {
        "bot": ("token", "groupId"),
        "crisp": ("id", "key", "website"),
    }
    missing_config = [
        f"{section}.{key}"
        for section, keys in required_config.items()
        for key in keys
        if not loaded_config.get(section, {}).get(key)
    ]
    if missing_config:
        logger.error("Missing required config values: %s", ", ".join(missing_config))
        raise SystemExit(1)

    return loaded_config


def prefer_ipv4_with_ipv6_fallback():
    if getattr(socket.getaddrinfo, "_prefer_ipv4_enabled", False):
        return

    orig_getaddrinfo = socket.getaddrinfo

    def getaddrinfo_prefer_ipv4(host, port, family=0, type=0, proto=0, flags=0):
        addresses = orig_getaddrinfo(host, port, family, type, proto, flags)
        return sorted(addresses, key=lambda item: item[0] != socket.AF_INET)

    getaddrinfo_prefer_ipv4._prefer_ipv4_enabled = True
    socket.getaddrinfo = getaddrinfo_prefer_ipv4


def init_crisp_client(crisp_config):
    crisp_client = Crisp()
    crisp_client.set_tier("plugin")
    crisp_client.authenticate(crisp_config["id"], crisp_config["key"])
    crisp_client.plugin.get_connect_account()
    crisp_client.website.get_website(crisp_config["website"])
    return crisp_client


def init_openai_client(openai_config):
    openai_api_key = openai_config.get("apiKey")
    if not openai_api_key:
        raise ValueError("openai.apiKey is not configured")
    openai_base_url = openai_config.get("baseUrl", "https://api.openai.com/v1")
    openai_client = OpenAI(api_key=openai_api_key, base_url=openai_base_url)
    openai_client.models.list()
    return openai_client


config = load_config()
session_store.init(config.get("storage", {}).get("sqlitePath", "data/sessions.sqlite3"))

if config.get("network", {}).get("preferIPv4", True):
    prefer_ipv4_with_ipv6_fallback()

try:
    client = init_crisp_client(config["crisp"])
except Exception as exc:
    logger.error("Crisp authentication failed: %s", exc)
    raise SystemExit(1)

try:
    openai = init_openai_client(config.get("openai", {}))
except Exception as exc:
    logger.warning("OpenAI auto-reply disabled: %s", exc)
    openai = None


def change_ai_button(session_id, is_enabled):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    text="关闭 AI 回复" if is_enabled else "打开 AI 回复",
                    callback_data=f"{session_id},{is_enabled}",
                )
            ]
        ]
    )


def crisp_user(nickname, avatar_url=None):
    user = {"nickname": nickname}
    if avatar_url:
        user["avatar"] = avatar_url
    return user


def operator_user():
    bot_config = config.get("bot", {})
    return crisp_user(
        bot_config.get("operatorName", "人工客服"),
        bot_config.get("operatorAvatar"),
    )


def ai_user():
    bot_config = config.get("bot", {})
    return crisp_user(
        bot_config.get("aiName", "智能客服"),
        bot_config.get("aiAvatar"),
    )
