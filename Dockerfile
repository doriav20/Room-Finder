# syntax=docker/dockerfile:1

# The Playwright version must match the "playwright" pin in pyproject.toml
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /uvx /bin/

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=Asia/Jerusalem \
    LANG=C.UTF-8 \
    DISPLAY=:99 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH=/opt/venv/bin:$PATH

# Virtual display, VNC server and noVNC's page for watching the browser, process supervisor,
# Hebrew fonts
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        xvfb fluxbox x11vnc novnc supervisor \
        culmus tzdata curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Regular Google Chrome build: no automation flags, same fingerprint as a desktop Chrome
RUN curl -fsSL -o /tmp/chrome.deb \
        https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && apt-get update \
    && apt-get install -y --no-install-recommends /tmp/chrome.deb \
    && rm -rf /tmp/chrome.deb /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["supervisord", "-n", "-c", "/app/docker/supervisord.conf"]
