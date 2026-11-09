FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY voice ./voice

ENV DATA_DIR=/app/data
EXPOSE 8090
CMD ["python", "-m", "voice"]
