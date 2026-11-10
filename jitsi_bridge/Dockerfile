# The Jitsi bridge in Docker (Linux servers). On a Mac, running it natively with ./start.sh is simpler.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

WORKDIR /app
RUN pip install --no-cache-dir "aiohttp>=3.9" "playwright==1.63.0"
COPY jitsi_bridge ./jitsi_bridge
COPY voice/__init__.py voice/config.py ./voice/

CMD ["python", "-m", "jitsi_bridge"]
