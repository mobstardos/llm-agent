#!/usr/bin/env python3
"""Авто-подпись Firefox-сборки расширения «LLM Agent Bridge» через web-ext.

Что делает:
  1. (по умолчанию) пересобирает dist/bridge-firefox (build_extension.py);
  2. запускает `web-ext lint --output json` и печатает сводку
     (при --strict ошибки lint останавливают процесс);
  3. запускает `web-ext sign` против AMO API и находит готовый .xpi.

Подписанный (даже unlisted) .xpi ставится НАВСЕГДА в обычный Firefox —
это и есть смысл авто-подписи; unlisted = распространение без публикации
в каталоге AMO, listed = публикация на addons.mozilla.org с ревью.

Учётные данные AMO (по убыванию приоритета):
  1. --api-key / --api-secret;
  2. env AMO_JWT_ISSUER / AMO_JWT_SECRET;
  3. env WEB_EXT_API_KEY / WEB_EXT_API_SECRET.
Пара ключей выдаётся на https://addons.mozilla.org/developers/addon/api/key/
(нужен аккаунт AMO). Без ключей подпись невозможна — скрипт печатает
инструкцию и завершается с кодом 2 (временная загрузка через
about:debugging работает и без подписи).

Зависимости: Node.js + npx (web-ext подтянется сам, версия — --webext-version).
ID дополнения берётся из манифеста (browser_specific_settings.gecko.id);
первая подпись навсегда привязывает этот id к аккаунту AMO — у другого
аккаунта подписать тот же id не получится. При повторной подписи той же
версии AMO откажет — поднимите version в обоих манифестах (build_extension.py
копирует его в dist автоматически).

Примеры:
  python scripts/sign_firefox.py                          # build + lint + sign
  python scripts/sign_firefox.py --no-lint --no-build     # только sign
  python scripts/sign_firefox.py --channel listed --strict
  set AMO_JWT_ISSUER=user:...&& set AMO_JWT_SECRET=...&& python scripts/sign_firefox.py
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "extension" / "dist" / "bridge-firefox"
ARTIFACTS = ROOT / "extension" / "dist" / "signed"
AMO_KEY_URL = "https://addons.mozilla.org/developers/addon/api/key/"


class CredsError(Exception):
    """Учётные данные AMO не найдены."""


# ─────────────────────────── чистые функции (тестируются в test_bridge) ──
def resolve_creds(args: argparse.Namespace,
                  env: dict | None = None) -> tuple[str, str, str]:
    """Вернуть (api_key, api_secret, источник): args → AMO_* → WEB_EXT_*."""
    e = env if env is not None else os.environ
    if getattr(args, "api_key", None) and getattr(args, "api_secret", None):
        return args.api_key, args.api_secret, "args"
    issuer = e.get("AMO_JWT_ISSUER") or e.get("WEB_EXT_API_KEY") or ""
    secret = e.get("AMO_JWT_SECRET") or e.get("WEB_EXT_API_SECRET") or ""
    if issuer and secret:
        src = "env:AMO_*" if e.get("AMO_JWT_ISSUER") else "env:WEB_EXT_*"
        return issuer, secret, src
    raise CredsError(
        "Нет учётных данных AMO. Получите пару ключей на " + AMO_KEY_URL +
        " и передайте их через --api-key/--api-secret или переменные "
        "окружения AMO_JWT_ISSUER / AMO_JWT_SECRET (или WEB_EXT_API_KEY / "
        "WEB_EXT_API_SECRET). Без подписи: временная загрузка — "
        "about:debugging#/runtime/this-firefox; постоянная без подписи — "
        "только Developer/Nightly/ESR (xpinstall.signatures.required=false).")


def find_npx(env: dict | None = None) -> str | None:
    """npx / npx.cmd — где он есть в системе, None если нет."""
    e = env if env is not None else os.environ
    for name in ("npx", "npx.cmd", "npx.exe"):
        p = shutil.which(name)
        if p:
            return p
    if os.name == "nt":  # который в PATH не нашёл — типовые места установки
        for cand in (Path(e.get("APPDATA", "")) / "npm" / "npx.cmd",
                     Path("C:/Program Files/nodejs/npx.cmd")):
            if cand.is_file():
                return str(cand)
    return None


def webext_cmd(mode: str, npx: str, webext_version: str,
               src: Path, artifacts: Path, channel: str,
               api_key: str, api_secret: str,
               timeout: int) -> list[str]:
    """Собрать командную строку web-ext (без shell). mode: lint|sign."""
    cmd = [npx, "--yes", "web-ext@" + webext_version, mode,
           "--source-dir", str(src)]
    if mode == "lint":
        cmd += ["--output", "json"]
        return cmd
    cmd += ["--artifacts-dir", str(artifacts), "--channel", channel,
            "--api-key", api_key, "--api-secret", api_secret,
            "--timeout", str(timeout)]
    return cmd


def run_cmd(cmd: list[str], env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def lint_summary(stdout: str) -> dict:
    """Вытащить summary из JSON-вывода web-ext lint (устойчиво к мусору)."""
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
                s = data.get("summary") or {}
                return {"errors": int(s.get("errors", 0)),
                        "warnings": int(s.get("warnings", 0)),
                        "notices": int(s.get("notices", 0))}
            except Exception:
                continue
    return {"errors": -1, "warnings": -1, "notices": -1}  # не распарсилось


def pick_xpi(artifacts: Path, newer_than: float = 0) -> Path | None:
    """Самый свежий .xpi в каталоге артефактов (после метки времени)."""
    if not artifacts.is_dir():
        return None
    cands = [p for p in artifacts.glob("*.xpi")
             if p.stat().st_mtime >= newer_than]
    return max(cands, key=lambda p: p.stat().st_mtime) if cands else None


# ─────────────────────────────────────────────────────────── main ──
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Авто-подпись Firefox-сборки llm-agent bridge (web-ext)")
    ap.add_argument("--source-dir", type=Path, default=SRC,
                    help=f"собранная раскладка (по умолч. {SRC})")
    ap.add_argument("--artifacts-dir", type=Path, default=ARTIFACTS,
                    help=f"куда класть .xpi (по умолч. {ARTIFACTS})")
    ap.add_argument("--channel", choices=("unlisted", "listed"),
                    default="unlisted",
                    help="unlisted = подпись для своего распространения "
                         "(по умолч.), listed = публикация на AMO с ревью")
    ap.add_argument("--api-key", default=None, help="AMO JWT issuer")
    ap.add_argument("--api-secret", default=None, help="AMO JWT secret")
    ap.add_argument("--api-url-prefix",
                    default="https://addons.mozilla.org/api/v5")
    ap.add_argument("--webext-version", default="7",
                    help="версия web-ext для npx (по умолч. 7)")
    ap.add_argument("--timeout", type=int, default=300,
                    help="таймаут запроса подписи, c (по умолч. 300)")
    ap.add_argument("--no-build", action="store_true",
                    help="не пересобирать dist/bridge-firefox")
    ap.add_argument("--no-lint", action="store_true",
                    help="пропустить web-ext lint")
    ap.add_argument("--strict", action="store_true",
                    help="ошибки lint останавливают подпись")
    args = ap.parse_args()

    if os.name == "nt":
        try:  # Windows-консоль: не падать на unicode-стрелках web-ext
            sys.stdout.reconfigure(errors="replace")
            sys.stderr.reconfigure(errors="replace")
        except Exception:
            pass

    src: Path = args.source_dir
    artifacts: Path = args.artifacts_dir

    # 1) сборка
    if not args.no_build:
        print("[1/3] сборка dist/bridge-firefox …")
        r = run_cmd([sys.executable, str(ROOT / "scripts" /
                                        "build_extension.py")], os.environ)
        sys.stdout.write(r.stdout or "")
        if r.returncode != 0:
            sys.stderr.write(r.stderr or "")
            print("[ОШИБКА] build_extension.py завершился с ошибкой")
            return 1
    if not (src / "manifest.json").is_file():
        print(f"[ОШИБКА] нет {src / 'manifest.json'} — соберите расширение: "
              "python scripts/build_extension.py")
        return 1

    # 2) lint
    npx = find_npx()
    if not npx:
        print("[ОШИБКА] не найден npx — установите Node.js "
              "(https://nodejs.org) и повторите. Либо подпишите вручную: "
              "`npx web-ext sign --source-dir extension/dist/bridge-firefox`")
        return 3
    t0 = time.time()
    if not args.no_lint:
        print("[2/3] web-ext lint …")
        r = run_cmd(webext_cmd("lint", npx, args.webext_version, src,
                               artifacts, args.channel, "", "", args.timeout),
                    os.environ)
        s = lint_summary(r.stdout or "")
        if s["errors"] < 0:
            print("  lint: вывод не распознан (не блокирует)")
            tail = (r.stdout or r.stderr or "").strip().splitlines()[-5:]
            for ln in tail:
                print("    " + ln[:160])
        else:
            print(f"  lint: ошибок {s['errors']}, предупреждений "
                  f"{s['warnings']}, замечаний {s['notices']}")
            if s["errors"] > 0 and args.strict:
                print("[ОШИБКА] --strict: ошибки lint запрещают подпись")
                return 4
    else:
        print("[2/3] lint пропущен (--no-lint)")

    # 3) sign
    try:
        key, secret, creds_src = resolve_creds(args)
    except CredsError as e:
        print("[ОШИБКА]", e)
        return 2
    print(f"[3/3] web-ext sign (channel={args.channel}, ключи из {creds_src})…")
    artifacts.mkdir(parents=True, exist_ok=True)
    cmd = webext_cmd("sign", npx, args.webext_version, src, artifacts,
                     args.channel, key, secret, args.timeout)
    cmd += ["--api-url-prefix", args.api_url_prefix]
    r = run_cmd(cmd, os.environ)
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        print("[ОШИБКА] web-ext sign:", (r.stderr or r.stdout or "").strip()
              [-800:])
        if "version" in out.lower() and "already" in out.lower():
            print("  Подсказка: такая версия уже подписана этим аккаунтом — "
                  "поднимите version в манифестах и пересоберите.")
        return 5
    xpi = pick_xpi(artifacts, newer_than=t0)
    if not xpi:
        print("[ОШИБКА] подпись отчиталась успешно, но .xpi не найден в "
              + str(artifacts))
        return 6
    print()
    print("=" * 62)
    print("Подписанный пакет:", xpi)
    print("Установка: откройте .xpi в Firefox (или about:addons → «Установить "
          "дополнение из файла») — постоянная установка, без режима отладки.")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nпрервано пользователем")
        sys.exit(130)
