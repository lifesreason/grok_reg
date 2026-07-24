# 精简镜像：默认 amd64 构建约 2–4 分钟（视缓存）
# 反检测：Google Chrome（amd64）/ Chromium（arm64）
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    GROK_REG_DATA_DIR=/app/data \
    GROK_REG_IN_DOCKER=1 \
    GROK_REG_HEADLESS=0 \
    TZ=Asia/Shanghai

WORKDIR /app

# 浏览器 + 最少 GUI/字体依赖（去掉 fonts-noto-cjk，体积大、装得慢）
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        wget \
        fonts-liberation \
        fonts-dejavu-core \
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
        xauth \
        xvfb \
        tzdata \
    && if [ "$(dpkg --print-architecture)" = "amd64" ]; then \
        wget -q -O /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
        && apt-get install -y --no-install-recommends /tmp/chrome.deb \
        && browser_path="$(command -v google-chrome-stable || command -v google-chrome || true)" \
        && browser_package=google-chrome-stable; \
    else \
        apt-get install -y --no-install-recommends chromium \
        && browser_path="$(command -v chromium || command -v chromium-browser || true)" \
        && browser_package=chromium; \
    fi \
    && test -n "${browser_path}" \
    && test -x "${browser_path}" \
    && apt-mark manual "${browser_package}" \
    && printf '%s\n' \
        '#!/bin/sh' \
        "exec \"${browser_path}\" \"\$@\"" \
        > /usr/bin/browser \
    && chmod 755 /usr/bin/browser \
    && test -x /usr/bin/browser \
    && rm -f /tmp/chrome.deb \
    && apt-get purge -y --auto-remove wget \
    && rm -rf \
        /var/lib/apt/lists/* \
        /var/cache/apt/archives/* \
        /tmp/* \
        /var/tmp/* \
        /usr/share/doc/* \
        /usr/share/man/* \
        /usr/share/info/*

ENV CHROME_BIN=/usr/bin/browser

# Fail the image build early instead of retrying browser startup at runtime.
RUN test -x /usr/bin/browser

COPY requirements.txt .
RUN pip install --no-cache-dir --no-compile -r requirements.txt

COPY . .
RUN mkdir -p /app/data \
    && find /app -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true

EXPOSE 8787

CMD ["uvicorn", "web_app:app", "--host", "0.0.0.0", "--port", "8787"]
