#!/bin/sh

AUTOREPLY=`printf '%b' "${AUTOREPLY}"`
OPENAI_PAYLOAD=`printf '%b' "${OPENAI_PAYLOAD}"`
NETWORK_PREFER_IPV4=${NETWORK_PREFER_IPV4:-true}
STORAGE_SQLITE_PATH=${STORAGE_SQLITE_PATH:-data/sessions.sqlite3}
OPENAI_BASEURL=${OPENAI_BASEURL:-https://api.openai.com/v1}
OPENAI_MODEL=${OPENAI_MODEL:-gpt-4o-mini}

cat > /Crisp-Telegram-Bot/config.yml << EOF
bot:
  token: ${BOT_TOKEN}
  groupId: ${BOT_GROUPID}
network:
  preferIPv4: ${NETWORK_PREFER_IPV4}
storage:
  sqlitePath: ${STORAGE_SQLITE_PATH}
crisp:
  id: ${CRISP_ID}
  key: ${CRISP_KEY}
  website: ${CRISP_WEBSITE}
easyimages:
  apiUrl: ${EasyImages_apiUrl}
  apiToken: ${EasyImages_apiToken}
autoreply:
${AUTOREPLY}
openai:
  apiKey: ${OPENAI_APIKEY}
  baseUrl: ${OPENAI_BASEURL}
  model: ${OPENAI_MODEL}
  payload: |
${OPENAI_PAYLOAD}
EOF
exec "$@"
