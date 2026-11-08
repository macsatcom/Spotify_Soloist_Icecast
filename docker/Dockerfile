# Spotify Soloist -> Icecast
# NOTE: the Soloist binary is NOT included (Spotify forbids redistribution).
# It is downloaded at runtime into /data/bin by the updater.
FROM debian:trixie-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl tini tzdata \
        python3 python3-pip \
        pulseaudio pulseaudio-utils \
        ffmpeg icecast2 \
        libasound2t64 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/app
COPY requirements.txt .
RUN pip3 install -r requirements.txt
COPY app ./app

ENV DATA_DIR_IN_CONTAINER=/data \
    CACHE_DIR_IN_CONTAINER=/cache \
    WEB_PORT=8380
VOLUME ["/data", "/cache"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
  CMD curl -fs "http://127.0.0.1:${WEB_PORT}/api/v1/health" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sh", "-c", "exec python3 -m uvicorn app.main:app --host 0.0.0.0 --port ${WEB_PORT} --no-access-log"]
