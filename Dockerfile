FROM python:3.12-slim

# ffmpeg does the mp3 encoding; libsodium/libopus are needed for voice receive.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg libopus0 libsodium23 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY dsbot ./dsbot
ENV DATA_DIR=/data PYTHONUNBUFFERED=1
VOLUME ["/data"]

CMD ["python", "-m", "dsbot"]
