FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GROK_REG_DATA_DIR=/app/data \
    GROK_REG_HEADLESS=0 \
    CHROME_BIN=/usr/bin/chromium

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        chromium \
        fonts-noto-cjk \
        libasound2 \
        libatk-bridge2.0-0 \
        libgtk-3-0 \
        libnss3 \
        libx11-xcb1 \
        libxcomposite1 \
        libxdamage1 \
        libxrandr2 \
        libgbm1 \
        libu2f-udev \
        xvfb \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /app/data

EXPOSE 8787

CMD ["xvfb-run", "-a", "-s", "-screen 0 1365x900x24", "uvicorn", "web_app:app", "--host", "0.0.0.0", "--port", "8787"]
