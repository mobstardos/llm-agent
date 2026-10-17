#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════
# LLM Agent — интерактивный установщик для Linux / macOS
#
#   bash install.sh          # интерактивно
#   bash install.sh --auto   # тихо, все вопросы по умолчанию
# ═══════════════════════════════════════════════════════════════════════
set -u
cd "$(dirname "$0")"

AUTO=0
[ "${1:-}" = "--auto" ] && AUTO=1

C_G="\033[0;32m"; C_Y="\033[0;33m"; C_R="\033[0;31m"; C_D="\033[2m"; C_0="\033[0m"
ok()   { echo -e " ${C_G}[✓]${C_0} $1"; }
warn() { echo -e " ${C_Y}[!]${C_0} $1"; }
err()  { echo -e " ${C_R}[✗]${C_0} $1"; }
step() { echo -e " ${C_D}[·]${C_0} $1"; }

echo
echo " ┌─────────────────────────────────────────────────────────┐"
echo " │        LLM Agent — интерактивная установка на ПК        │"
echo " └─────────────────────────────────────────────────────────┘"
echo

# ─── 1. Python ────────────────────────────────────────────────────────
PY=""
for cand in python3.14 python3.13 python3.12 python3.11 python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then
        if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
            PY="$cand"; break
        fi
    fi
done
if [ -z "$PY" ]; then
    err "Python 3.11-3.14 (обычная сборка) не найден. Установите Python 3.13"
    echo     "     или: sudo apt install python3.11 python3.11-venv"
    exit 1
fi
# free-threaded сборки (3.13t/3.14t): часть колёс отсутствует — авто-переключение
# на усечённый requirements-freethreaded.txt (см. отличие в requirements.txt)
REQFILE=requirements.txt
if "$PY" -c "import sysconfig,sys; sys.exit(0 if str(sysconfig.get_config_var('Py_GIL_DISABLED') or '0')=='1' else 1)" 2>/dev/null; then
    REQFILE=requirements-freethreaded.txt
    warn "FREE-THREADED сборка Python (3.13t/3.14t): беру усечённый набор зависимостей"
    warn "без lancedb, sentence-transformers, tree-sitter, duckdb, aiokafka, faster-whisper"
    warn "psycopg чистый (без бинарника) -> PG-фичи 503, пока libpq не в PATH; embedder -> hashing"
fi
PYVER=$("$PY" -c 'import sys; print(sys.version.split()[0])')
ok "Python $PYVER найден ($PY)"

# ─── 2. venv + зависимости ────────────────────────────────────────────
if [ ! -d ".venv" ]; then
    step "Создаю виртуальное окружение .venv ..."
    "$PY" -m venv .venv || { err "не удалось создать venv (нужен пакет python3-venv)"; exit 1; }
fi
# shellcheck disable=SC1091
source .venv/bin/activate
if [ ! -f ".venv/.installed" ]; then
    step "Ставлю зависимости (несколько минут) ..."
    pip install --upgrade pip -q
    pip install -r "$REQFILE" || { err "ошибка установки зависимостей"; exit 1; }
    touch .venv/.installed
else
    ok "Зависимости уже установлены"
fi

# ─── 3. Вопросы ───────────────────────────────────────────────────────
PORT=8000
PG_MODE="skip"
DSN=""
EMBEDDER=auto
if [ "$AUTO" = "0" ]; then
    echo
    echo " ─── Настройка ────────────────────────────────────────────────"
    printf " Порт веб-интерфейса [8000]: "; read -r PORT_IN
    [ -n "${PORT_IN:-}" ] && PORT="$PORT_IN"

    echo
    echo " PostgreSQL — долговременные зеркала, память агентов, аналитика:"
    echo "   [1] Docker-контейнер (нужен Docker) — рекомендую"
    echo "   [2] Внешний сервер — введу DSN вручную"
    echo "   [3] Пропустить (работа только на локальных файлах)"
    printf " Выбор [3]: "; read -r PG_IN
    case "${PG_IN:-3}" in
        1) PG_MODE="docker" ;;
        2) PG_MODE="external" ;;
        *) PG_MODE="skip" ;;
    esac
    if [ "$PG_MODE" = "external" ]; then
        printf " DSN (postgres://user:pass@host:5432/db): "; read -r DSN
    fi

    echo
    echo " Embedder памяти агентов:"
    echo "   [1] auto — bge-m3, если получится загрузить, иначе hashing"
    echo "   [2] hash — детерминированный, мгновенный, без загрузки моделей"
    printf " Выбор [1]: "; read -r EM_IN
    [ "${EM_IN:-1}" = "2" ] && EMBEDDER=hash
fi

# ─── 4. .env ──────────────────────────────────────────────────────────
if [ -f ".env" ]; then
    ok ".env уже существует — не перезаписываю."
else
    step "Пишу .env ..."
    ROOT="$(pwd)"
    if [ "$PG_MODE" = "external" ] && [ -n "$DSN" ]; then
        PG_BLOCK="DATABASE_URL=$DSN"
    else
        PG_BLOCK="PG_APP_HOST=localhost
PG_APP_PORT=5432
PG_APP_USER=llmagent
PG_APP_PASSWORD=secret
PG_APP_DATABASE=llmagent"
    fi
    cat > .env <<EOF
# LLM Agent — сгенерировано install.sh
HOST=127.0.0.1
PORT=$PORT
PROJECT_ROOT=$ROOT

# PostgreSQL (зеркала журнала/сессий/планов, память агентов, аналитика)
$PG_BLOCK

# Память агентов: auto | hash | model
AGENT_MEMORY_EMBEDDER=$EMBEDDER

# Эксплуатация: авто-ретенция выключена по умолчанию
PG_RETENTION_DAYS=0
BACKUP_KEEP_LAST=7
EOF
    ok ".env записан"
fi

# ─── 5. Первичная настройка + схема PostgreSQL ────────────────────────
step "Первичная настройка (каталоги, базы, маркер) ..."
python first_run.py --defaults >/dev/null 2>&1 \
    || warn "first_run завершился с предупреждениями — не критично"

if [ "$PG_MODE" != "skip" ]; then
    if [ "$PG_MODE" = "docker" ]; then
        step "Поднимаю PostgreSQL в Docker ..."
        docker compose up -d postgres 2>/dev/null \
            || warn "Docker недоступен — поднимите вручную: docker compose up -d postgres"
    fi
    step "Накатываю схему БД (scripts/init_db.py) ..."
    python scripts/init_db.py \
        || warn "Схему накатить не удалось — сервер всё равно стартует (SQLite-режим)"
fi

# ─── 6. Смоук-тест ────────────────────────────────────────────────────
step "Смоук-тест: стартую сервер на порту $PORT ..."
SRV_LOG="$(pwd)/.install_smoke.log"
python run.py --skip-checks --skip-setup --port "$PORT" >"$SRV_LOG" 2>&1 &
SRV_PID=$!
HEALTH=0
for _ in $(seq 1 120); do
    sleep 1
    CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT/api/db/status" 2>/dev/null || echo 000)
    if [ "$CODE" = "200" ]; then HEALTH=1; break; fi
    kill -0 "$SRV_PID" 2>/dev/null || break
done
kill "$SRV_PID" 2>/dev/null
wait "$SRV_PID" 2>/dev/null
if [ "$HEALTH" = "1" ]; then
    ok "Сервер отвечает: HTTP 200"
else
    warn "Сервер не ответил за 2 минуты (лог: $SRV_LOG)."
    echo     "     Первый старт может быть долгим — просто запустите run.sh;"
    echo     "     при первом старте также будет мастер настройки AI."
fi

echo
echo " ─── Готово ───────────────────────────────────────────────────"
echo "  Запуск:               bash run.sh   (или: .venv/bin/python run.py)"
echo "  Веб-интерфейс:        http://127.0.0.1:$PORT"
echo "  Настройки AI:         python first_run.py --reconfigure-ai"
echo "  Проверка БД:          python scripts/init_db.py --check"
echo "  Переменные окружения: deploy/env.example"
echo
exit 0
