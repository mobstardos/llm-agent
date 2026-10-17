#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════
# install.sh — установка Taste Skill (Leonxlnx/taste-skill)
# ═══════════════════════════════════════════════════════════════════════
# "The Anti-Slop Frontend Framework for AI Agents"
# Премиальные frontend-скилы: layout, typography, motion, spacing
# ═══════════════════════════════════════════════════════════════════════

set -e

REPO="https://github.com/Leonxlnx/taste-skill"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SKILLS_DIR="$ROOT/.skills"

mkdir -p "$SKILLS_DIR"

# Проверка Node.js
if ! command -v npx >/dev/null 2>&1; then
    echo "❌ npx не установлен. Установите Node.js: https://nodejs.org/"
    exit 1
fi

echo "════════════════════════════════════════════════════════════════"
echo "Установка Taste Skill (Leonxlnx/taste-skill)"
echo "════════════════════════════════════════════════════════════════"
echo ""

# V1 (стабильная) — рекомендуется для production
# V2 (experimental) — по умолчанию у Leonxlnx

SKILL="${1:-all}"

case "$SKILL" in
    "all")
        echo "Установка ВСЕХ skills из taste-skill..."
        npx -y skills add "$REPO"
        ;;
    "v1")
        echo "Установка V1 (стабильная) — design-taste-frontend-v1..."
        npx -y skills add "$REPO" --skill "design-taste-frontend-v1"
        ;;
    "v2")
        echo "Установка V2 (experimental) — design-taste-frontend..."
        npx -y skills add "$REPO" --skill "design-taste-frontend"
        ;;
    *)
        echo "Установка конкретного skill: $SKILL"
        npx -y skills add "$REPO" --skill "$SKILL"
        ;;
esac

echo ""
echo "✅ Taste Skill установлен в $SKILLS_DIR"
echo ""
echo "Skills в репозитории:"
echo "  - design-taste-frontend (v2 experimental, по умолчанию)"
echo "  - design-taste-frontend-v1 (стабильная v1)"
echo "  - image-generation skills (reference boards)"
echo ""
echo "GitHub: https://github.com/Leonxlnx/taste-skill"
echo "Сайт: https://tasteskill.dev"
