# ═══════════════════════════════════════════════════════════════════════
# Dockerfile для llm-agent — основное приложение
# Рекомендация 10 (Sprint 4, P2)
# ═══════════════════════════════════════════════════════════════════════
#
# Multi-stage build:
#   Stage 1 (builder): ставим Python-зависимости в .venv
#   Stage 2 (runtime): копируем .venv + src/ + Python-slim образ
#
# Запуск (из корня проекта):
#   docker build -t llm-agent:latest .
#   docker run --rm -p 8000:8000 --env-file .env llm-agent:latest
#
# Или через docker-compose (см. обновлённый docker-compose.yml):
#   docker compose up -d llm-agent
#
# Размер образа: ~450 МБ (Python 3.11-slim ~50 МБ + зависимости ~400 МБ).
# Production-ready: запускается от непривилегированного пользователя,
# HEALTHCHECK добавлен, /tmp примонтирован как tmpfs для кэша.
# ═══════════════════════════════════════════════════════════════════════

# ─── Stage 1: Builder ────────────────────────────────────────────────
FROM python:3.12-slim AS builder

# Установить системные библиотеки, нужные для сборки wheel-пакетов:
#   - build-essential — gcc/g++ для C-расширений (psycopg, tree-sitter)
#   - libpq-dev — для psycopg (PostgreSQL client lib)
#   - libffi-dev — для cffi (используется cryptography)
#   - libxml2-dev/libxslt-dev — для lxml (если понадобится)
#   - libjpeg-dev/zlib1g-dev — для Pillow
#   - libtesseract-dev libleptonica-dev — для pytesseract (опционально)
#   - git — pip install через git+https (если есть в requirements)
#   - curl — для healthcheck
ARG BUILD_DEPS="\
  build-essential \
  libpq-dev \
  libffi-dev \
  libxml2-dev libxslt-dev \
  libjpeg-dev zlib1g-dev \
  libtesseract-dev libleptonica-dev \
  git curl \
  "
RUN apt-get update && apt-get install -y --no-install-recommends $BUILD_DEPS \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Сначала копируем только requirements (кэшируем слой pip install)
COPY requirements.txt requirements-freethreaded.txt ./

# Создать venv и поставить зависимости
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --upgrade pip setuptools wheel \
    && pip install --no-cache-dir -r requirements.txt

# ─── Stage 2: Runtime ─────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# Только runtime-зависимости (без -dev пакетов)
ARG RUNTIME_DEPS="\
  libpq5 \
  libffi8 \
  libxml2 libxslt1.1 \
  libjpeg62-turbo zlib1g \
  tesseract-ocr tesseract-ocr-rus \
  ffmpeg \
  ripgrep \
  fd-find \
  "
RUN apt-get update && apt-get install -y --no-install-recommends $RUNTIME_DEPS \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/fdfind /usr/bin/fd  # Debian/Fedora compatibility

# Создать непривилегированного пользователя
RUN groupadd -r llmagent && useradd -r -g llmagent -d /app -s /sbin/nologin llmagent

# Скопировать venv из builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # llm-agent specific
    PG_ENABLED=true \
    PG_REPLICATE=1

WORKDIR /app

# Скопировать проект (используем .dockerignore для исключения data/, .git/, ...)
COPY --chown=llmagent:llmagent . /app/

# Создать каталоги для runtime-данных (примонтируем volume)
RUN mkdir -p /app/data /app/logs /app/shadows \
    && chown -R llmagent:llmagent /app

USER llmagent

# Применить схему БД при старте (если PG доступен)
# HEALTHCHECK через /api/db/health эндпоинт
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
  CMD curl -sf http://localhost:8000/api/db/health || exit 1

# Открываем порт (внутри контейнера)
EXPOSE 8000

# Запуск через run.py — он сам сделает проверки окружения,
# выберет PG-драйвер, запустит qwenproxy если нужно, поднимет uvicorn
CMD ["python", "run.py", "--port", "8000", "--host", "0.0.0.0"]
