"""MCP-сервер: сетевые утилиты."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import ssl
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("network-mcp")


def _json_result(data) -> str:
    try:
        return json.dumps(data, ensure_ascii=False, indent=2, default=str)[:30000]
    except Exception:
        return str(data)[:30000]


async def _run(cmd, timeout=60):
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            return -1, "timeout"
        return (proc.returncode or 0, stdout.decode("utf-8", errors="replace"))
    except FileNotFoundError:
        return -1, f"Command not found: {cmd[0]}"
    except Exception as e:
        return -1, str(e)


app = Server("network")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name="net_ping",
             description="Ping хоста.",
             inputSchema={"type": "object", "properties": {
                 "host": {"type": "string"},
                 "count": {"type": "integer", "default": 4}},
                 "required": ["host"]}),
        Tool(name="net_traceroute",
             description="Traceroute до хоста.",
             inputSchema={"type": "object", "properties": {
                 "host": {"type": "string"},
                 "max_hops": {"type": "integer", "default": 20}},
                 "required": ["host"]}),
        Tool(name="net_dns_lookup",
             description="DNS lookup.",
             inputSchema={"type": "object", "properties": {
                 "host": {"type": "string"}},
                 "required": ["host"]}),
        Tool(name="net_reverse_dns",
             description="Reverse DNS lookup.",
             inputSchema={"type": "object", "properties": {
                 "ip": {"type": "string"}},
                 "required": ["ip"]}),
        Tool(name="net_port_check",
             description="Проверить доступность TCP-порта.",
             inputSchema={"type": "object", "properties": {
                 "host": {"type": "string"},
                 "port": {"type": "integer"},
                 "timeout": {"type": "number", "default": 3.0}},
                 "required": ["host", "port"]}),
        Tool(name="net_port_scan",
             description="Сканировать список портов.",
             inputSchema={"type": "object", "properties": {
                 "host": {"type": "string"},
                 "ports": {"type": "array", "items": {"type": "integer"}},
                 "timeout": {"type": "number", "default": 1.0}},
                 "required": ["host", "ports"]}),
        Tool(name="net_ssl_check",
             description="Проверить SSL-сертификат хоста.",
             inputSchema={"type": "object", "properties": {
                 "host": {"type": "string"},
                 "port": {"type": "integer", "default": 443}},
                 "required": ["host"]}),
        Tool(name="net_http_headers",
             description="HTTP-заголовки URL.",
             inputSchema={"type": "object", "properties": {
                 "url": {"type": "string"}},
                 "required": ["url"]}),
        Tool(name="net_whois",
             description="WHOIS домена.",
             inputSchema={"type": "object", "properties": {
                 "domain": {"type": "string"}},
                 "required": ["domain"]}),
        Tool(name="net_local_ip",
             description="Локальные IP-адреса и hostname.",
             inputSchema={"type": "object", "properties": {}}),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "net_local_ip":
            hostname = socket.gethostname()
            try:
                local_ip = socket.gethostbyname(hostname)
            except Exception:
                local_ip = ""
            return [TextContent(
                type="text",
                text=_json_result({
                    "hostname": hostname,
                    "local_ip": local_ip,
                    "fqdn": socket.getfqdn(),
                }),
            )]

        if name == "net_ping":
            host = arguments["host"]
            count = arguments.get("count", 4)
            # Windows: -n, Unix: -c
            flag = "-n" if os.name == "nt" else "-c"
            rc, out = await _run(
                ["ping", flag, str(count), host], timeout=30,
            )
            return [TextContent(type="text", text=out[:5000])]

        if name == "net_traceroute":
            host = arguments["host"]
            max_hops = arguments.get("max_hops", 20)
            if os.name == "nt":
                cmd = ["tracert", "-h", str(max_hops), host]
            else:
                cmd = ["traceroute", "-m", str(max_hops), host]
            rc, out = await _run(cmd, timeout=120)
            return [TextContent(type="text", text=out[:10000])]

        if name == "net_dns_lookup":
            host = arguments["host"]
            try:
                infos = socket.getaddrinfo(host, None)
                results = set()
                for info in infos:
                    results.add(info[4][0])
                return [TextContent(
                    type="text",
                    text=_json_result({
                        "host": host,
                        "addresses": sorted(results),
                        "count": len(results),
                    }),
                )]
            except socket.gaierror as e:
                return [TextContent(type="text", text=f"DNS error: {e}")]

        if name == "net_reverse_dns":
            ip = arguments["ip"]
            try:
                host = socket.gethostbyaddr(ip)
                return [TextContent(
                    type="text",
                    text=_json_result({
                        "ip": ip,
                        "hostname": host[0],
                        "aliases": host[1],
                    }),
                )]
            except Exception as e:
                return [TextContent(type="text", text=f"Reverse DNS: {e}")]

        if name == "net_port_check":
            host = arguments["host"]
            port = arguments["port"]
            timeout = arguments.get("timeout", 3.0)
            t0 = time.perf_counter()
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(timeout)
                    rc = s.connect_ex((host, port))
                duration_ms = (time.perf_counter() - t0) * 1000
                if rc == 0:
                    return [TextContent(
                        type="text",
                        text=_json_result({
                            "host": host, "port": port,
                            "open": True,
                            "duration_ms": round(duration_ms, 1),
                        }),
                    )]
                return [TextContent(
                    type="text",
                    text=_json_result({
                        "host": host, "port": port,
                        "open": False, "error_code": rc,
                    }),
                )]
            except Exception as e:
                return [TextContent(type="text", text=f"Error: {e}")]

        if name == "net_port_scan":
            host = arguments["host"]
            ports = arguments["ports"]
            timeout = arguments.get("timeout", 1.0)

            async def check(port):
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                        s.settimeout(timeout)
                        rc = s.connect_ex((host, port))
                    return port, rc == 0
                except Exception:
                    return port, False

            results = await asyncio.gather(
                *(check(p) for p in ports[:1000]),
            )
            open_ports = [p for p, ok in results if ok]
            return [TextContent(
                type="text",
                text=_json_result({
                    "host": host,
                    "scanned": len(ports),
                    "open_ports": open_ports,
                }),
            )]

        if name == "net_ssl_check":
            host = arguments["host"]
            port = arguments.get("port", 443)
            try:
                ctx = ssl.create_default_context()
                with socket.create_connection((host, port), timeout=10) as sock:
                    with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                        cert = ssock.getpeercert()
                        cipher = ssock.cipher()
                        version = ssock.version()

                # Дни до истечения
                not_after = cert.get("notAfter", "")
                days_left = None
                if not_after:
                    try:
                        exp = datetime.strptime(
                            not_after, "%b %d %H:%M:%S %Y %Z",
                        ).replace(tzinfo=timezone.utc)
                        days_left = (exp - datetime.now(timezone.utc)).days
                    except Exception:
                        pass

                return [TextContent(
                    type="text",
                    text=_json_result({
                        "host": host, "port": port,
                        "subject": dict(x[0] for x in cert.get("subject", [])),
                        "issuer": dict(x[0] for x in cert.get("issuer", [])),
                        "not_before": cert.get("notBefore"),
                        "not_after": not_after,
                        "days_left": days_left,
                        "version": version,
                        "cipher": cipher[0] if cipher else "",
                        "serial_number": cert.get("serialNumber"),
                        "alt_names": [
                            v for k, v in cert.get("subjectAltName", [])
                            if k == "DNS"
                        ][:20],
                    }),
                )]
            except ssl.SSLCertVerificationError as e:
                return [TextContent(
                    type="text",
                    text=f"⚠️ SSL verification failed: {e}",
                )]
            except Exception as e:
                return [TextContent(type="text", text=f"Error: {e}")]

        if name == "net_http_headers":
            url = arguments["url"]
            try:
                import httpx
                r = httpx.head(url, timeout=10, follow_redirects=True,
                               verify=False)
                return [TextContent(
                    type="text",
                    text=_json_result({
                        "url": str(r.url),
                        "status": r.status_code,
                        "headers": dict(r.headers),
                    }),
                )]
            except Exception as e:
                return [TextContent(type="text", text=f"Error: {e}")]

        if name == "net_whois":
            domain = arguments["domain"]
            whois_bin = None
            for candidate in ("whois", "whois.exe"):
                import shutil
                if shutil.which(candidate):
                    whois_bin = candidate
                    break
            if not whois_bin:
                return [TextContent(
                    type="text",
                    text="whois не установлен. Windows: download Sysinternals whois.",
                )]
            rc, out = await _run([whois_bin, domain], timeout=30)
            return [TextContent(type="text", text=out[:10000])]

        return [TextContent(type="text", text=f"Неизвестный инструмент: {name}")]
    except Exception as e:
        logger.exception("network tool %s failed", name)
        return [TextContent(type="text", text=f"Ошибка: {e}")]


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
