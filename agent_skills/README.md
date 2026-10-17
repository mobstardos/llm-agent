# Agent Skills — расширения для AI-кода-агентов

> **Важно:** Agent Skills — это НЕ MCP-серверы!
> Это markdown-файлы (SKILL.md / DESIGN.md), которые **код-агенты читают напрямую**
> (Claude Code, Cursor, Codex, ChatGPT). Они не используют протокол MCP.

## Что такое Agent Skills

Agent Skills — новый формат расширений, введённый Vercel Labs
(https://github.com/vercel-labs/agent-skills). Это markdown-файлы с
правилами, токенами дизайна, паттернами. AI-агенты читают их перед
выполнением задачи — и получают знания «как правильно делать».

**Отличие от MCP:**

| Аспект | MCP-сервер | Agent Skill |
|---|---|---|
| Формат | Исполняемый код (Python/Node) | Markdown-файлы (SKILL.md) |
| Протокол | JSON-RPC через stdio | Прямое чтение LLM-агентом |
| Активация | Запуск процесса | Копирование .md в проект |
| Управление | YAML-декларация + settings | `npx skills add <github-url>` |
| Когда работает | По запросу агента (tool call) | Перед выполнением задачи (system context) |

## Установленные skills в проекте

### 1. **Taste Skill** (Leonxlnx/taste-skill)

> "The Anti-Slop Frontend Framework for AI Agents"

**Что делает:** Задаёт реальные правила дизайна — layout, typography, motion,
spacing — избавляя AI от шаблонных решений. Включает skills для image-generation
(reference boards для web, mobile, brand kits).

**Установка:**
```bash
# Все skills сразу:
npx skills add https://github.com/Leonxlnx/taste-skill

# Только основной skill:
npx skills add https://github.com/Leonxlnx/taste-skill --skill "design-taste-frontend"

# V1 (стабильная):
npx skills add https://github.com/Leonxlnx/taste-skill --skill "design-taste-frontend-v1"
```

**GitHub:** https://github.com/Leonxlnx/taste-skill
**Сайт:** https://tasteskill.dev
**Лицензия:** MIT

### 2. **Awesome DESIGN.md** (VoltAgent/awesome-design-md)

> "Curated collection of DESIGN.md analysis by developer focused websites"

**Что делает:** Готовые DESIGN.md файлы (73 шт.), проанализированные с реальных
сайтов. Копируете DESIGN.md в корень проекта → AI-агент читает его → генерирует
UI, визуально консистентный с дизайном.

**Что такое DESIGN.md:**
Новый формат от Google Stitch — plain-text design system, который AI читает
как обычный markdown. Определяет: как проект должен выглядеть и ощущаться.

**Установка:**
```bash
# Клонировать репозиторий с готовыми DESIGN.md:
git clone https://github.com/VoltAgent/awesome-design-md.git

# Выбрать нужный DESIGN.md, скопировать в корень проекта:
cp awesome-design-md/<site-name>/DESIGN.md ./DESIGN.md

# Сказать агенту: "build me a page that looks like this"
```

**GitHub:** https://github.com/VoltAgent/awesome-design-md
**Запросить новый DESIGN.md:** https://getdesign.md/request
**Лицензия:** MIT

## Дополнительные Agent Skills (можно добавить)

### Vercel Labs Agent Skills (эталонные)
- **GitHub:** https://github.com/vercel-labs/agent-skills
- Установка: `npx skills add https://github.com/vercel-labs/agent-skills`

### Другие популярные skills (поиск через `npx skills`)

```bash
# Найти skills (Vercel CLI):
npx skills search "design"
npx skills search "frontend"
npx skills search "code review"
npx skills search "documentation"
```

## Как интегрировать с llm-agent

### Способ 1: через llm-agent MCP-сервер (recommended)

Llm-agent читает `.md` файлы из корня проекта автоматически. Установите
skills в корень — агент их подхватит через свои промпты.

```bash
cd /path/to/llm-agent-v2026-10-01

# Taste Skill:
npx skills add https://github.com/Leonxlnx/taste-skill

# Awesome DESIGN.md (выберите 1-2):
git clone https://github.com/VoltAgent/awesome-design-md.git /tmp/awesome-design
cp /tmp/awesome-design/<site-name>/DESIGN.md ./DESIGN.md

# Теперь llm-agent читает SKILL.md и DESIGN.md при работе с UI-задачами
```

### Способ 2: через capabilities

Добавьте capability в `capabilities/design.md`:

```yaml
id: design_skills
schema_version: "1.5.0"
title: Design Skills
description: Taste Skill + DESIGN.md для premium frontend
operations:
  - id: apply_taste
    description: Применить правила дизайна из taste-skill
    input: [task_description]
    output: design_recommendations
  - id: apply_design_md
    description: Использовать DESIGN.md из репозитория
    input: [design_md_path]
    output: ui_specification
```

### Способ 3: через prompts

Skills можно подключить к конкретному агенту через `prompt.md`. Добавьте
в `agents/frontend/prompt.md`:

```markdown
## Дизайн-правила

Перед выполнением UI-задачи — прочитай:
- `SKILL.md` (если установлен taste-skill)
- `DESIGN.md` (если установлен из awesome-design-md)

Применяй правила: layout, typography, motion, spacing. Не делай шаблонный UI.
```

## Установка всех Agent Skills одной командой

```bash
bash agent_skills/install_all.sh
```

См. `install_all.sh` в этом каталоге.

## Структура каталога

```
agent_skills/
├── README.md                      ← этот файл
├── install_all.sh                 ← установка всех skills
├── taste-skill/
│   └── install.sh                 ← npx skills add Leonxlnx/taste-skill
└── awesome-design-md/
    ├── install.sh                 ← git clone + копирование DESIGN.md
    └── examples/                  ← примеры DESIGN.md (опционально)
```

## Связанные ресурсы

- **Vercel Labs Agent Skills:** https://github.com/vercel-labs/agent-skills
- **Google Stitch DESIGN.md:** https://stitch.withgoogle.com/docs/design-md/overview/
- **Awesome Agent Skills:** https://github.com/anthropics/claude-code-skills (Anthropic)
- **Taste Skill сайт:** https://tasteskill.dev
- **DESIGN.md запрос:** https://getdesign.md/request
