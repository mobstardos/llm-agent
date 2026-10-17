#!/usr/bin/env python3
"""Фейковый OpenAI-совместимый провайдер для E2E-теста «проверки связи».

Воспроизводит два сценария:
  --mode region  → OpenAI-стиль 403 unsupported_country_region_territory
                   (ровно та ошибка, которую OpenAI отдаёт из РФ/РБ)
  --mode ok      → корректный 200 с choices

Запуск: python scripts/fake_provider.py --port 9401 --mode region
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    mode: str = "region"

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        if not self.path.rstrip("/").endswith("chat/completions"):
            self._send(404, {"error": {"message": f"no route {self.path}"}})
            return
        if self.mode == "region":
            self._send(403, {
                "error": {
                    "code": "unsupported_country_region_territory",
                    "message": "Country, region, or territory not supported",
                    "param": None,
                    "type": "request_forbidden",
                }
            })
        else:
            self._send(200, {
                "id": "chatcmpl-fake", "object": "chat.completion",
                "model": "fake-model",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "pong"},
                    "finish_reason": "stop",
                }],
            })

    def log_message(self, fmt, *args):  # тишина в консоли
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9401)
    ap.add_argument("--mode", choices=["region", "ok"], default="region")
    args = ap.parse_args()

    Handler.mode = args.mode
    server = HTTPServer(("127.0.0.1", args.port), Handler)
    print(f"fake provider ({args.mode}) on http://127.0.0.1:{args.port}/v1")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
