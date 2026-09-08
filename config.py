import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", os.path.join("data", "saved_items.db"))
SESSION_FILE = os.getenv("SESSION_FILE", os.path.join("data", "saver.session"))
BOT_PROXY = os.getenv("BOT_PROXY", "").strip() or None

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_BASE_URL = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
GROQ_MODEL = os.getenv("GROQ_MODEL", "qwen/qwen3.6-27b")
GROQ_VISION_MODEL = os.getenv("GROQ_VISION_MODEL", GROQ_MODEL)
GROQ_STT_MODEL = os.getenv("GROQ_STT_MODEL", "whisper-large-v3-turbo")
GROQ_PROXY = os.getenv("GROQ_PROXY", "").strip() or None
GROQ_TIMEOUT_SEC = int(os.getenv("GROQ_TIMEOUT_SEC") or 300)

API_ID = int(os.getenv("API_ID") or 0)
API_HASH = os.getenv("API_HASH", "")

# MTProxy для Telegram: если MT_PROXY_HOST пуст — прямое соединение (Render/зарубежный хост).
MT_PROXY_HOST = os.getenv("MT_PROXY_HOST", "").strip()
MT_PROXY_PORT = int(os.getenv("MT_PROXY_PORT") or 1080)
MT_PROXY_SECRET = os.getenv("MT_PROXY_SECRET", "").strip() or None

# HTTP-порт для health-чека (Render прокидывает PORT). 0 — сервер не поднимается.
HTTP_PORT = int(os.getenv("PORT") or os.getenv("HTTP_PORT") or 0)

# Владелец бота (числовой user_id) — запасной для бэкапов на эфемерном диске.
OWNER_ID = os.getenv("OWNER_ID", "").strip()

# Seed-файл экспорта БД (JSON) для первого запуска, когда БД пуста.
SEED_FILE = os.getenv("SEED_FILE", "tg_saver_seed.json")

# Приватный канал-хранилище снимков экспорта: сюда бот кладёт копии (и имеет право
# читать их обратно, т.к. является админом канала) — единственный способ для бота
# автоматически восстановить историю после потери эфемерного диска Render.
BACKUP_CHANNEL_ID = int(os.getenv("BACKUP_CHANNEL_ID", "0") or 0) or None