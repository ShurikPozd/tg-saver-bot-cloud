FROM python:3.11-slim

# ffmpeg нужен yt-dlp для слияния видео+аудио при скачивании выше 720p (/api/download);
# nodejs — JS-рантайм для декодирования сигнатур YouTube (yt-dlp требует >= 22; Debian bookworm даёт 18,
# поэтому ставим 22 с NodeSource).
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]
