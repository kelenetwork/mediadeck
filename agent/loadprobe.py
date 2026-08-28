#!/usr/bin/env python3
"""mediadeck node load probe — single-file agent for streaming nodes.

Deploy this one file to each streaming node; it exposes a tiny /load endpoint
the scheduler polls.  Zero third-party dependencies (stdlib only).

Usage:
    python3 loadprobe.py --port 9800 [--iface eth0] [--token SECRET]

Response:
    GET /load  ->  {"ok": true, "active_streams": N, "egress_mbps": X}

- active_streams: ESTABLISHED TCP connections on the given service ports
  (default 443,80) — a good proxy for concurrent media streams.
- egress_mbps: TX rate of the interface, sampled over a sliding 5s window.
- Optional bearer token via --token / LOADPROBE_TOKEN.
"""
from __future__ import annotations

import argparse
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def default_iface() -> str:
    try:
        with open("/proc/net/route", encoding="utf-8") as fh:
            for line in fh.readlines()[1:]:
                fields = line.split()
                if fields[1] == "00000000":
                    return fields[0]
    except OSError:
        pass
    return "eth0"


def tx_bytes(iface: str) -> int:
    try:
        with open("/proc/net/dev", encoding="utf-8") as fh:
            for line in fh:
                if line.strip().startswith(iface + ":"):
                    return int(line.split()[9])
    except (OSError, IndexError, ValueError):
        pass
    return 0


def established_count(ports: set[int]) -> int:
    count = 0
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh.readlines()[1:]:
                    fields = line.split()
                    if len(fields) < 4 or fields[3] != "01":  # 01 = ESTABLISHED
                        continue
                    local_port = int(fields[1].rsplit(":", 1)[1], 16)
                    if local_port in ports:
                        count += 1
        except (OSError, IndexError, ValueError):
            continue
    return count


class Sampler:
    def __init__(self, iface: str, ports: set[int]) -> None:
        self.iface = iface
        self.ports = ports
        self.egress_mbps = 0.0
        self.active = 0
        self._lock = threading.Lock()
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        prev = tx_bytes(self.iface)
        prev_ts = time.monotonic()
        while True:
            time.sleep(5)
            now = tx_bytes(self.iface)
            now_ts = time.monotonic()
            mbps = max(0.0, (now - prev) * 8 / (now_ts - prev_ts) / 1e6)
            active = established_count(self.ports)
            with self._lock:
                self.egress_mbps = round(mbps, 1)
                self.active = active
            prev, prev_ts = now, now_ts

    def snapshot(self) -> dict:
        with self._lock:
            return {"ok": True, "active_streams": self.active,
                    "egress_mbps": self.egress_mbps}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9800)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--iface", default=default_iface())
    parser.add_argument("--service-ports", default="443,80",
                        help="comma-separated ports counted as streams")
    parser.add_argument("--token", default=os.environ.get("LOADPROBE_TOKEN", ""))
    args = parser.parse_args()

    ports = {int(p) for p in args.service_ports.split(",") if p.strip()}
    sampler = Sampler(args.iface, ports)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path.rstrip("/") not in ("", "/load", "/healthz"):
                self.send_response(404)
                self.end_headers()
                return
            if args.token:
                auth = self.headers.get("Authorization", "")
                if auth != f"Bearer {args.token}":
                    self.send_response(401)
                    self.end_headers()
                    return
            body = json.dumps(sampler.snapshot()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args) -> None:
            pass

    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    print(f"loadprobe on {args.bind}:{args.port} iface={args.iface} ports={sorted(ports)}")
    server.serve_forever()


if __name__ == "__main__":
    main()
