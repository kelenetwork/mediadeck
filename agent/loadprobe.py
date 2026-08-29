#!/usr/bin/env python3
"""mediadeck node load probe — single-file agent for streaming nodes.

Deploy this one file to each streaming node; it exposes a tiny /load endpoint
the scheduler polls.  Zero third-party dependencies (stdlib only).

Usage:
    python3 loadprobe.py --port 9800 [--iface eth0] [--token SECRET]
                         [--speed-log /var/log/nginx/mediadeck-speed.log]

Response:
    GET /load  ->  {"ok": true, "active_streams": N, "egress_mbps": X,
                    "user_speeds": {"<tag>": bytes_per_second, ...}}

- active_streams: ESTABLISHED TCP connections on the given service ports
  (default 443,80) — a good proxy for concurrent media streams.
- egress_mbps: TX rate of the interface, sampled over a sliding 5s window.
- user_speeds: real bytes/second per anonymised user tag over the last
  window, tailed from nginx's mediadeck_speed log
  (format: "$msec $arg_u $bytes_sent $request_time").  This is what the
  panel shows as live bandwidth — actual wire bytes, not an estimate.
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


class SpeedLog:
    """Tail nginx's per-request speed log into per-user live rates.

    Each completed request logs "<msec> <utag> <bytes_sent> <request_time>".
    A request that took R seconds and sent B bytes contributed B/R bytes/s
    while it ran; summing the contributions of requests that overlap the
    current window approximates each user's wire rate well enough for a
    dashboard, without packet capture or nginx modules.

    A long-running request (one big Range read) only logs when it *ends*, so
    completed lines alone would show 0 mid-transfer.  Nothing to be done
    stdlib-side cheaply — but media players issue frequent bounded Range
    reads, so in practice lines arrive every few seconds during playback.
    """

    WINDOW = 15.0  # seconds of history that count toward "current" speed

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        # list of (end_ts, start_ts, utag, bytes)
        self._events: list[tuple[float, float, str, int]] = []
        if path:
            threading.Thread(target=self._tail, daemon=True).start()

    def _tail(self) -> None:
        fh = None
        inode = None
        while True:
            try:
                if fh is None:
                    fh = open(self.path, encoding="utf-8", errors="replace")
                    inode = os.fstat(fh.fileno()).st_ino
                    fh.seek(0, os.SEEK_END)
                line = fh.readline()
                if line:
                    self._ingest(line)
                    continue
                # idle: detect logrotate (new inode or shrunk file)
                time.sleep(1)
                try:
                    st = os.stat(self.path)
                    if st.st_ino != inode or st.st_size < fh.tell():
                        fh.close()
                        fh = None
                except OSError:
                    fh.close()
                    fh = None
            except OSError:
                fh = None
                time.sleep(5)

    def _ingest(self, line: str) -> None:
        parts = line.split()
        if len(parts) < 4:
            return
        try:
            end_ts = float(parts[0])
            utag = parts[1]
            sent = int(parts[2])
            took = max(0.05, float(parts[3]))
        except ValueError:
            return
        if not utag or utag == "-" or sent <= 0:
            return
        with self._lock:
            self._events.append((end_ts, end_ts - took, utag, sent))
            if len(self._events) > 10000:
                del self._events[:5000]

    def speeds(self) -> dict:
        """utag -> bytes/second over the last WINDOW seconds.

        Each request transferred at ``sent/duration`` bytes/s while it ran;
        its contribution to "current speed" is that rate weighted by how much
        of the request overlaps the window. Requests fully inside the window
        contribute rate*(duration/WINDOW), which sums to bytes/WINDOW — the
        honest average, immune to double-counting bursts of tiny Range reads.
        """
        now = time.time()
        cutoff = now - self.WINDOW
        out: dict[str, float] = {}
        with self._lock:
            self._events = [e for e in self._events if e[0] >= cutoff]
            for end_ts, start_ts, utag, sent in self._events:
                duration = max(0.05, end_ts - start_ts)
                overlap = max(0.0, min(end_ts, now) - max(start_ts, cutoff))
                if overlap <= 0:
                    continue
                out[utag] = out.get(utag, 0.0) + (sent / duration) * (overlap / self.WINDOW)
        return {k: int(v) for k, v in out.items() if v >= 1}


class Sampler:
    def __init__(self, iface: str, ports: set[int], speedlog: "SpeedLog") -> None:
        self.iface = iface
        self.ports = ports
        self.speedlog = speedlog
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
                    "egress_mbps": self.egress_mbps,
                    "user_speeds": self.speedlog.speeds()}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9800)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--iface", default=default_iface())
    parser.add_argument("--service-ports", default="443,80",
                        help="comma-separated ports counted as streams")
    parser.add_argument("--token", default=os.environ.get("LOADPROBE_TOKEN", ""))
    parser.add_argument("--speed-log",
                        default=os.environ.get(
                            "LOADPROBE_SPEED_LOG",
                            "/var/log/nginx/mediadeck-speed.log"),
                        help="nginx mediadeck_speed log; empty disables")
    args = parser.parse_args()

    ports = {int(p) for p in args.service_ports.split(",") if p.strip()}
    sampler = Sampler(args.iface, ports, SpeedLog(args.speed_log))

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
