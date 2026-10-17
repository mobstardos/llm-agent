# -*- coding: utf-8 -*-
"""Автодетект протокола: опрашивает реестр парсеров по приоритету."""


def detect_best(registry, chunk, direction):
    """Возвращает (ParserAPI|None, confidence). Побеждает максимум score,
    при равенстве — больший PRIORITY парсера."""
    best = None
    best_score = 0
    best_conf = 0
    for p in registry.all():
        try:
            score = int(p.detect(chunk, {"direction": direction}) or 0)
        except Exception:
            score = 0
        if score <= 0:
            continue
        weighted = score * 100 + p.priority
        if weighted > best_score:
            best_score = weighted
            best = p
            best_conf = min(score, 99)
    return best, best_conf
