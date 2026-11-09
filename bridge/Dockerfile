# The Zoom Meeting SDK only runs on Linux x86_64. On an Apple Silicon Mac, Docker Desktop
# emulates it; the bridge only moves audio, so that's fast enough.
FROM --platform=linux/amd64 ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates tini python3 python3-pip python3-gi gir1.2-glib-2.0 \
        libglib2.0-0t64 libdbus-1-3 libgbm1 libgl1 libx11-xcb1 libxfixes3 \
        libxcb-image0 libxcb-keysyms1 libxcb-randr0 libxcb-shape0 libxcb-shm0 libxcb-xfixes0 libxcb-xtest0 \
        libasound2t64 libasound2-plugins pulseaudio pulseaudio-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

# PulseAudio won't run as root, and the bot doesn't need to.
RUN useradd --create-home bot
COPY bridge.py entrypoint.sh ./
RUN chmod +x entrypoint.sh
USER bot

ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]
CMD ["python3", "/app/bridge.py"]
