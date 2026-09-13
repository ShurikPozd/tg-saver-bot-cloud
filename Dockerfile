FROM python:3.11-slim

# ffmpeg нужен yt-dlp для слияния видео+аудио при скачивании выше 720p (/api/download).
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]
