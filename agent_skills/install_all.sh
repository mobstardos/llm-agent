#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════
# install_all.sh — установка ВСЕХ Agent Skills одной командой
# ═════════════════════════════════════════════════════════════════════════
# ВАЖНО: Agent Skills — это НЕ MCP-серверы!
# Это markdown-файлы, которые AI-код-агенты (Claude Code, Cursor, Codex)
# читают напрямую. Установка через npx skills add / git clone.
# ═══════════════════════════════════════════════════════════════════════

set -e

SKILLS_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SKILLS_DIR/.." && pwd)"

echo "════════════════════════════════════════════════════════════════"
echo "Установка ВСЕХ Agent Skills для llm-agent"
echo "════════════════════════════════════════════════════════════════"
echo ""
echo "Внимание: Agent Skills — это НЕ MCP-серверы."
echo "Это markdown-файлы, которые AI-код-агенты читают напрямую."
echo ""

# ─── 1. Taste Skill ─────────────────────────────────────────────────
echo "1. Установка Taste Skill (Leonxlnx/taste-skill)..."
echo "   'The Anti-Slop Frontend Framework for AI Agents'"
if [ -x "$SKILLS_DIR/taste-skill/install.sh" ]; then
    bash "$SKILLS_DIR/taste-skill/install.sh" all || {
        echo "   ⚠ Ошибка установки Taste Skill"
        echo "   Ручная установка: npx skills add https://github.com/Leonxlnx/taste-skill"
    }
else
    echo "   ⚠ install.sh не найден для taste-skill"
fi
echo ""

# ─── 2. Awesome DESIGN.md ──────────────────────────────────────────
echo "2. Установка Awesome DESIGN.md (VoltAgent/awesome-design-md)..."
echo "   '73 DESIGN.md файлов с реальных сайтов'"
if [ -x "$SKILLS_DIR/awesome-design-md/install.sh" ]; then
    bash "$SKILLS_DIR/awesome-design-md/install.sh" || {
        echo "   ⚠ Ошибка установки Awesome DESIGN.md"
        echo "   Ручная установка: git clone https://github.com/VoltAgent/awesome-design-md.git"
    }
else
    echo "   ⚠ install.sh не найден для awesome-design-md"
fi
echo ""

# ─── 3. Дополнительные Vercel Labs Agent Skills (опционально) ─────
echo "3. Установка Vercel Labs Agent Skills (опционально)..."
echo "   Эталонные skills от Vercel Labs"
read -p "Установить Vercel Labs Agent Skills? (y/N): " -r
if [[ $REPLY =~ ^[Yy]$ ]]; then
    if command -v npx >/dev/null 2>&1; then
        npx -y skills add https://github.com/vercel-labs/agent-skills || true
        echo "   ✓ Vercel Labs Agent Skills установлены"
    else
        echo "   ⚠ npx не установлен"
    fi
else
    echo "   - Пропущено"
fi
echo ""

echo "════════════════════════════════════════════════════════════════"
echo "✅ Установка Agent Skills завершена"
echo "════════════════════════════════════════════════════════════════"
echo ""
echo "Установлено в:"
echo "  - Taste Skill: $ROOT/.skills/"
echo "  - DESIGN.md collection: $ROOT/.design-md/"
echo ""
echo "Использование:"
echo "  1. Скопируйте выбранный DESIGN.md в корень проекта:"
echo "     cp .design-md/<site-name>/DESIGN.md ./DESIGN.md"
echo "  2. Сказать llm-agent: «сделай страницу как в DESIGN.md»"
echo ""
echo "Документация: agent_skills/README.md"
