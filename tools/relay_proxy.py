"""Logging proxy between Azeroth Survivors and the official co-op servers.

The game connects here (WOW_ROGUE_COOP_URL=ws://127.0.0.1:8765/coop/lobby/),
every connection is forwarded to the official lobby with the same path, and
every frame in both directions is written to logs/relay-<time>.jsonl.

The lobby hands out relay tickets carrying a wss:// relay address (message
0x1e); the proxy rewrites any such address in lobby->game frames to
ws://<this proxy>/__up/<encoded original>, so the relay traffic comes through
here and gets logged too. Co-op keeps working normally while it runs.

    python relay_proxy.py [--port 8765] [--upstream wss://wow.aethus.pl]
"""
import argparse
import asyncio
import base64
import datetime
import itertools
import json
import os
import re
import struct
import time

import websockets

HERE = os.path.dirname(os.path.abspath(__file__))
conn_ids = itertools.count(1)
UP_PREFIX = "/__up/"
# u32 little-endian length followed by a wss:// address, as the lobby encodes strings
WSS_FIELD = re.compile(rb"(?s)(.{4})(wss://[\x21-\x7e]+)")


def rewrite_relay_urls(msg, proxy_host):
    """Point every length-prefixed wss:// string in a binary frame at the proxy."""
    out, pos, hits = bytearray(), 0, []
    for m in WSS_FIELD.finditer(msg):
        n = struct.unpack("<I", m.group(1))[0]
        url = m.group(2)
        if n > len(url) or not url[:n].startswith(b"wss://"):
            continue
        url = url[:n].decode()
        enc = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
        # trailing slash keeps anything the game appends out of the encoded part
        new = f"ws://{proxy_host}{UP_PREFIX}{enc}/".encode()
        out += msg[pos:m.start()] + struct.pack("<I", len(new)) + new
        pos = m.start(2) + n
        hits.append(url)
    if not hits:
        return msg, hits
    out += msg[pos:]
    return bytes(out), hits


def upstream_for(path, default_base):
    if path.startswith(UP_PREFIX):
        enc, _, rest = path[len(UP_PREFIX):].partition("/")
        url = base64.urlsafe_b64decode(enc + "=" * (-len(enc) % 4)).decode()
        return url + rest
    return default_base.rstrip("/") + path


class Log:
    def __init__(self, path):
        self.f = open(path, "a", encoding="utf-8")
        self.t0 = time.monotonic()

    def write(self, **rec):
        rec = {"t": round(time.monotonic() - self.t0, 3), **rec}
        self.f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.f.flush()


def frame_record(msg):
    if isinstance(msg, str):
        try:
            return {"json": json.loads(msg)}
        except ValueError:
            return {"text": msg}
    return {"bin": base64.b64encode(msg).decode(), "len": len(msg)}


def summary(msg):
    s = msg if isinstance(msg, str) else f"<{len(msg)} bytes>"
    return s if len(s) <= 160 else s[:157] + "..."


async def pump(src, dst, log, cid, direction, proxy_host=None):
    async for msg in src:
        log.write(c=cid, dir=direction, **frame_record(msg))
        print(f"[{cid}] {direction} {summary(msg)}")
        if proxy_host and isinstance(msg, bytes):
            msg, hits = rewrite_relay_urls(msg, proxy_host)
            for url in hits:
                log.write(c=cid, ev="rewrote", url=url)
                print(f"[{cid}] rerouted relay {url} through proxy")
        await dst.send(msg)


async def handle(client, args, log):
    cid = next(conn_ids)
    path = client.request.path
    headers = {k: v for k, v in client.request.headers.raw_items()}
    kind = "relay" if path.startswith(UP_PREFIX) else "lobby"
    log.write(c=cid, ev="open", kind=kind, path=path, headers=headers)
    print(f"[{cid}] open {kind} {path}")
    url = upstream_for(path, args.upstream)
    proxy_host = headers.get("Host") or f"{args.host}:{args.port}"
    try:
        async with websockets.connect(url, max_size=None, ping_interval=None,
                                      user_agent_header=headers.get("User-Agent")) as up:
            log.write(c=cid, ev="upstream", url=url)
            tasks = [asyncio.create_task(pump(client, up, log, cid, "c>s")),
                     asyncio.create_task(pump(up, client, log, cid, "s>c", proxy_host))]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            for t in done:
                if t.exception():
                    log.write(c=cid, ev="error", error=repr(t.exception()))
    except Exception as e:
        log.write(c=cid, ev="error", error=repr(e))
        print(f"[{cid}] error {e!r}")
    finally:
        log.write(c=cid, ev="close", code=client.close_code, reason=client.close_reason)
        print(f"[{cid}] closed")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--upstream", default="wss://wow.aethus.pl")
    args = ap.parse_args()

    os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(HERE, "logs", f"relay-{stamp}.jsonl")
    log = Log(path)
    print(f"proxy ws://{args.host}:{args.port} -> {args.upstream}  log: {path}")
    async with websockets.serve(lambda ws: handle(ws, args, log), args.host, args.port,
                                max_size=None, ping_interval=None):
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
