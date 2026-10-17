#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════
# install.sh — установка Awesome DESIGN.md (VoltAgent/awesome-design-md)
# ═══════════════════════════════════════════════════════════════════════
# Коллекция DESIGN.md файлов, проанализированных с реальных сайтов (73 шт.)
# DESIGN.md — plain-text design system от Google Stitch
# ═══════════════════════════════════════════════════════════════════════

set -e

REPO="https://github.com/VoltAgent/awesome-design-md"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DEST="$ROOT/.design-md"
TMP_DIR="${TMPDIR:-/tmp}/awesome-design-md-$$"

mkdir -p "$DEST"

# Проверка git
if ! command -v git >/dev/null 2>&1; then
    echo "❌ git не установлен. Установите: sudo apt install git"
    exit 1
fi

echo "════════════════════════════════════════════════════════════════"
echo "Установка Awesome DESIGN.md (VoltAgent/awesome-design-md)"
echo "════════════════════════════════════════════════════════════════"
echo ""

# Клонировать репозиторий во временную директорию
echo "1. Клонирование репозитория во временную директорию..."
git clone --depth 1 "$REPO" "$TMP_DIR"
echo "   ✓ Склонировано в $TMP_DIR"
echo ""

# Найти все DESIGN.md файлы
echo "2. Поиск всех DESIGN.md файлов..."
find "$TMP_DIR" -name "DESIGN.md" -type f | head -10
TOTAL=$(find "$TMP_DIR" -name "DESIGN.md" -type f | wc -l)
echo "   Найдено: $TOTAL DESIGN.md файлов"
echo ""

# Скопировать в локальную директорию
echo "3. Копирование DESIGN.md файлов в $DEST..."
cp -r "$TMP_DIR"/* "$DEST/" 2>/dev/null || true
echo "   ✓ Скопировано в $DEST"
echo ""

# Предложить установить один DESIGN.md в корень проекта
echo "4. Установка DESIGN.md в корень проекта..."
echo "   Доступные DESIGN.md (топ-10):"
ls "$DEST" | grep -v "README\|LICENSE\|.git" | head -10
echo "   ..."
echo ""
echo "   Чтобы установить конкретный DESIGN.md:"
echo "     cp $DEST/<site-name>/DESIGN.md $ROOT/DESIGN.md"
echo ""

# Cleanup
rm -rf "$TMP_DIR"
echo "✅ Awesome DESIGN.md установлен в $DEST"
echo ""
echo "Использование:"
echo "  1. Выберите сайт: ls $DEST"
echo "  2. Скопируйте его DESIGN.md в корень проекта:"
echo "     cp $DEST/<site-name>/DESIGN.md $ROOT/DESIGN.md"
echo "  3. Скажите llm-agent: «сделай страницу как в DESIGN.md»"
echo ""
echo "GitHub: https://github.com/VoltAgent/awesome-design-md"
echo "Запрос нового DESIGN.md: https://getdesign.md/request"
