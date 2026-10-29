"""Standalone Azeroth Survivors co-op server: lobby and relay in one process.

Players point the game at it with
    WOW_ROGUE_COOP_URL=ws://<server address>:8765/coop/lobby/
and host/join as usual. The lobby hands out relay tickets pointing back at
this server (/coop/relay/), and the relay forwards game messages between the
host and its guests. Nothing goes to the official servers.

Protocol (reconstructed from captures, see PROTOCOL.md): every WebSocket
binary frame is one message, first byte = type, integers little-endian,
strings and byte vectors prefixed with a u32 length.

    python coop_server.py [--port 8765] [--public-host ADDR]
"""
import argparse
import asyncio
import itertools
import os
import random
import secrets
import struct
import sys
import time

import websockets

import nat

# lobby, game -> server
C_HELLO, C_LIST, C_HOST, C_UPDATE, C_JOIN = 0x01, 0x02, 0x03, 0x04, 0x05
# relay, game -> server
C_SEND, C_AUTH = 0x06, 0x0B
# both
C_KICK, C_LEAVE, C_PING = 0x07, 0x08, 0x09
# server -> game
S_LIST, S_HOSTED, S_JOINED, S_ERROR = 0x14, 0x15, 0x16, 0x17
S_MEMBER_JOINED, S_MEMBER_LEFT, S_DELIVER, S_PONG = 0x18, 0x19, 0x1A, 0x1C
S_TICKET, S_PEER_DROPPED, S_PEER_BACK, S_COUNTRY = 0x1E, 0x1F, 0x20, 0x21

FROZEN = getattr(sys, "frozen", False)  # built into an .exe by PyInstaller
# logs go next to the .exe, not into PyInstaller's temp unpack folder
HERE = os.path.dirname(os.path.abspath(sys.executable if FROZEN else __file__))
HOST_ID = 0
CODE_CHARS = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
RECONNECT_GRACE = 60.0
MAX_PARTY = 5


class Reader:
    def __init__(self, data):
        self.b, self.p = data, 1

    def u8(self):
        self.p += 1
        return self.b[self.p - 1]

    def u32(self):
        self.p += 4
        return struct.unpack_from("<I", self.b, self.p - 4)[0]

    def u64(self):
        self.p += 8
        return struct.unpack_from("<Q", self.b, self.p - 8)[0]

    def raw(self, n):
        self.p += n
        return self.b[self.p - n:self.p]

    def vec(self):
        return self.raw(self.u32())

    def str(self):
        return self.vec().decode("utf-8", "replace")


def p_u8(v):
    return bytes([v])


def p_u32(v):
    return struct.pack("<I", v)


def p_vec(b):
    return p_u32(len(b)) + b


def p_str(s):
    return p_vec(s.encode())


def msg(kind, *parts):
    return bytes([kind]) + b"".join(parts)


LOG_FILE = None


def log(*a):
    line = " ".join([time.strftime("%H:%M:%S")] + [str(x) for x in a])
    print(line, flush=True)
    if LOG_FILE:
        LOG_FILE.write(line + "\n")
        LOG_FILE.flush()


def trace(where, data):
    """Every message except pings and bulk game traffic, for debugging."""
    if not LOG_FILE or not data or data[0] in (C_PING, S_PONG):
        return
    if data[0] in (C_SEND, S_DELIVER) and len(data) > 40:
        return
    LOG_FILE.write(f"{time.strftime('%H:%M:%S')}   {where} {data[:64].hex(' ')}\n")
    LOG_FILE.flush()


def trace_event(text):
    if LOG_FILE:
        LOG_FILE.write(f"{time.strftime('%H:%M:%S')}   {text}\n")
        LOG_FILE.flush()


class Lobby:
    def __init__(self, code, version, host_ws):
        self.code = code
        self.version = version
        self.host_ws = host_ws          # host's lobby connection
        self.token = secrets.token_bytes(8)
        self.name = self.description = self.password = ""
        self.public = 1
        self.hero, self.cls, self.zone = "", 0, ""
        self.open, self.classes, self.max, self.heroic = 1, b"", MAX_PARTY, 0
        self.ids = itertools.count(1)
        self.members = {}               # guest id -> lobby connection
        self.relay = {}                 # member id (0 = host) -> relay connection
        self.dropped = {}               # member id -> grace timer task

    def apply_update(self, r):
        self.hero, self.cls, self.zone = r.str(), r.u8(), r.str()
        self.open, self.classes, self.max, self.heroic = r.u8(), r.vec(), r.u8(), r.u8()

    def list_entry(self):
        return (p_str(self.code) + p_str(self.name) + p_str(self.description) + p_str(self.hero)
                + p_u8(self.cls) + p_str(self.zone) + p_u8(1 + len(self.members))
                + p_u8(1 if self.password else 0) + p_u8(self.open) + p_str("")
                + p_vec(self.classes) + p_vec(b""))

    def ticket(self, member_id, relay_url):
        return msg(S_TICKET, p_str(relay_url), p_str(self.code), self.token, p_u32(member_id),
                   p_str(self.version), struct.pack("<Q", int(time.time())), p_vec(bytes(64)))


class Server:
    def __init__(self, public_host):
        self.public_host = public_host
        self.lobbies = {}

    def relay_url(self, ws):
        host = self.public_host or ws.request.headers.get("Host", "127.0.0.1:8765")
        return f"ws://{host}/coop/relay/"

    def new_code(self):
        while True:
            code = "".join(random.choice(CODE_CHARS) for _ in range(5))
            if code not in self.lobbies:
                return code

    def list_message(self):
        shown = [lb for lb in self.lobbies.values() if lb.public and lb.open]
        return msg(S_LIST, p_u32(len(shown)), *(lb.list_entry() for lb in shown),
                   bytes(lb.max for lb in shown), bytes(lb.heroic for lb in shown))

    # ---- lobby ----

    async def lobby(self, ws):
        version, lobby, my_id = "", None, None
        try:
            async for data in ws:
                if not isinstance(data, bytes) or not data:
                    continue
                r, kind = Reader(data), data[0]
                trace(f"lobby {tag(ws)} {my_id} >", data)
                if kind == C_PING:
                    await safe_send(ws, msg(S_PONG))
                elif kind == C_HELLO:
                    r.u32()
                    version = r.str()
                elif kind == C_LIST:
                    await safe_send(ws, self.list_message())
                elif kind == C_HOST:
                    if lobby:
                        await send_error(ws, "already in a lobby")
                        continue
                    name, public, desc, password = r.str(), r.u8(), r.str(), r.str()
                    lobby = Lobby(self.new_code(), version, ws)
                    lobby.name, lobby.public, lobby.description, lobby.password = name, public, desc, password
                    lobby.apply_update(r)
                    if not lobby.name:
                        lobby.name = f"{lobby.hero}'s run"
                    my_id = HOST_ID
                    self.lobbies[lobby.code] = lobby
                    await safe_send(ws, lobby.ticket(HOST_ID, self.relay_url(ws)))
                    await safe_send(ws, msg(S_HOSTED, p_str(lobby.code)))
                    log(f"lobby {lobby.code} hosted by {lobby.hero} ({lobby.zone})")
                elif kind == C_UPDATE and lobby and my_id == HOST_ID:
                    lobby.apply_update(r)
                elif kind == C_JOIN:
                    code, password = r.str().upper(), r.str()
                    target = self.lobbies.get(code)
                    why = (None if lobby is None else "already in a lobby") or \
                          (None if target else "no lobby with that code")
                    if not why and target.version != version:
                        why = "that lobby plays a different version of the game"
                    if not why and target.password and password != target.password:
                        why = "wrong password"
                    if not why and 1 + len(target.members) >= min(target.max, MAX_PARTY):
                        why = "the party is full"
                    if not why and not target.open:
                        why = "the run is under way"
                    if why:
                        await send_error(ws, why)
                        continue
                    lobby, my_id = target, next(target.ids)
                    lobby.members[my_id] = ws
                    await safe_send(ws, lobby.ticket(my_id, self.relay_url(ws)))
                    await safe_send(ws, msg(S_JOINED, p_str(lobby.code)))
                    await safe_send(lobby.host_ws, msg(S_MEMBER_JOINED, p_u32(my_id)))
                    log(f"lobby {lobby.code}: guest {my_id} joined")
                elif kind == C_KICK and lobby and my_id == HOST_ID:
                    gid = r.u32()
                    guest = lobby.members.pop(gid, None)
                    if guest:
                        await safe_send(ws, msg(S_MEMBER_LEFT, p_u32(gid)))
                        await guest.close()
                elif kind == C_LEAVE:
                    break
        except websockets.ConnectionClosed:
            pass
        finally:
            if lobby:
                await self.leave_lobby(lobby, my_id)

    async def leave_lobby(self, lobby, my_id):
        if my_id == HOST_ID:
            if self.lobbies.get(lobby.code) is lobby:
                del self.lobbies[lobby.code]
                log(f"lobby {lobby.code} closed")
            for guest in list(lobby.members.values()):
                await guest.close()
            lobby.members.clear()
        elif lobby.members.pop(my_id, None):
            await safe_send(lobby.host_ws, msg(S_MEMBER_LEFT, p_u32(my_id)))
            log(f"lobby {lobby.code}: guest {my_id} left")

    # ---- relay ----

    async def relay(self, ws):
        lobby, my_id, left = None, None, False
        try:
            async for data in ws:
                if not isinstance(data, bytes) or not data:
                    continue
                r, kind = Reader(data), data[0]
                trace(f"relay {tag(ws)} {my_id} >", data)
                if kind == C_PING:
                    await safe_send(ws, msg(S_PONG))
                elif kind == C_AUTH:
                    code, token, mid = r.str(), r.raw(8), r.u32()
                    lobby = self.lobbies.get(code)
                    if not lobby or token != lobby.token:
                        log(f"relay: rejected ticket for {code}")
                        await ws.close()
                        return
                    my_id = mid
                    old = lobby.relay.get(mid)
                    lobby.relay[mid] = ws
                    timer = lobby.dropped.pop(mid, None)
                    if timer:
                        timer.cancel()
                        await self.tell_relay(lobby, mid, S_PEER_BACK)
                        log(f"relay {code}: {mid} reconnected")
                    else:
                        log(f"relay {code}: {mid} connected")
                    if old and old is not ws:
                        await old.close()
                elif kind == C_SEND and lobby:
                    to, payload = r.u32(), r.vec()
                    peer = lobby.relay.get(to)
                    if peer:
                        await safe_send(peer, msg(S_DELIVER, p_u32(my_id), p_vec(payload)))
                elif kind == C_KICK and lobby and my_id == HOST_ID:
                    gid = r.u32()
                    peer = lobby.relay.pop(gid, None)
                    await safe_send(ws, msg(S_MEMBER_LEFT, p_u32(gid)))
                    if peer:
                        await peer.close()
                elif kind == C_LEAVE:
                    left = True
                    break
        except websockets.ConnectionClosed:
            pass
        finally:
            if lobby and lobby.relay.get(my_id) is ws:
                if left:
                    await self.relay_gone(lobby, my_id)
                else:
                    await self.tell_relay(lobby, my_id, S_PEER_DROPPED)
                    lobby.dropped[my_id] = asyncio.create_task(self.grace(lobby, my_id))
                    log(f"relay {lobby.code}: {my_id} dropped, waiting {RECONNECT_GRACE:.0f}s")

    async def grace(self, lobby, mid):
        await asyncio.sleep(RECONNECT_GRACE)
        lobby.dropped.pop(mid, None)
        await self.relay_gone(lobby, mid)

    async def relay_gone(self, lobby, mid):
        lobby.relay.pop(mid, None)
        await self.tell_relay(lobby, mid, S_MEMBER_LEFT)
        log(f"relay {lobby.code}: {mid} gone")
        if mid == HOST_ID:
            for peer in list(lobby.relay.values()):
                await peer.close()
            lobby.relay.clear()

    async def tell_relay(self, lobby, mid, kind):
        """The host hears about guests; guests hear about the host."""
        targets = [p for i, p in lobby.relay.items() if i != mid and (mid == HOST_ID or i == HOST_ID)]
        for peer in targets:
            await safe_send(peer, msg(kind, p_u32(mid)))

    async def handle(self, ws):
        path = ws.request.path
        trace_event(f"open {tag(ws)} {path} from {ws.remote_address[0]}")
        try:
            if path.startswith("/coop/relay"):
                await self.relay(ws)
            elif path.startswith("/coop/lobby") or path == "/":
                await self.lobby(ws)
            else:
                await ws.close()
        finally:
            by = "game" if getattr(ws.protocol, "close_rcvd_then_sent", None) else "server"
            trace_event(f"close {tag(ws)} by {by} code={ws.close_code}")


async def send_error(ws, why):
    # message type 0x17 is a guess: it never showed up in the captures
    log(f"refused: {why}")
    await safe_send(ws, msg(S_ERROR, p_str(why)))


def tag(ws):
    return str(ws.id)[:4]


async def safe_send(ws, data):
    trace(f"      {tag(ws)}   <", data)
    try:
        await ws.send(data)
    except websockets.ConnectionClosed:
        pass


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--public-host", help="address:port guests use to reach this server "
                    "(default: whatever address the game connected to)")
    ap.add_argument("--no-upnp", action="store_true", help="don't ask the router to open the port")
    ap.add_argument("--no-check", action="store_true",
                    help="don't ask ifconfig.co whether the port is reachable from the internet")
    args = ap.parse_args()
    global LOG_FILE
    os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)
    path = os.path.join(HERE, "logs", time.strftime("server-%Y%m%d-%H%M%S.log"))
    LOG_FILE = open(path, "a", encoding="utf-8")
    server = Server(args.public_host)
    async with websockets.serve(server.handle, args.bind, args.port, max_size=None,
                                ping_interval=None, compression=None):
        log(f"log file: {path}")
        log(f"co-op server on port {args.port}; you use ws://127.0.0.1:{args.port}/coop/lobby/")
        # the server is already listening, so the outside check can connect back to it
        gw = await asyncio.to_thread(nat.setup, args.port, log, not args.no_upnp, not args.no_check)
        try:
            await asyncio.Future()
        finally:
            if gw:
                try:
                    gw.remove_mapping(args.port)
                    log("UPnP: port mapping removed")
                except Exception:
                    pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"server stopped: {e!r}")
        if FROZEN:  # keep a double-clicked window open long enough to read the error
            input("Press Enter to close.")
        raise SystemExit(1)
