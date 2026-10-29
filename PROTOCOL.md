# Azeroth Survivors co-op protocol (game v0.127.0)

Reconstructed from proxy captures on 2026-10-28. Every WebSocket binary frame
is one message: first byte = type, integers little-endian, `str`/`vec` = u32
length + bytes.

## Lobby (`/coop/lobby/`, official `wss://wow.aethus.pl/coop/lobby/`)

| Type | Dir | Body |
|---|---|---|
| 01 | c>s | hello: u32 proto (3), str version, then `00 00 00 00 01` (unknown) |
| 02 | c>s | request lobby list |
| 03 | c>s | host: str name, u8 public, str description, str password, *update body* |
| 04 | c>s | update: str hero, u8 class, str zone, u8 open, vec classes, u8 max, u8 heroic |
| 05 | c>s | join: str code, str password |
| 07 | c>s | host removes member: u32 id |
| 08 | c>s | leave |
| 09 / 1c | c>s / s>c | ping / pong (every 30 s) |
| 14 | s>c | list: u32 n, n × entry, n × u8 max, n × u8 heroic |
| 15 | s>c | you host: str code |
| 16 | s>c | you joined: str code |
| 17 | s>c | **guess** (never captured): error, str English reason ("wrong password", ...) |
| 18 | s>c | member joined: u32 id |
| 19 | s>c | member left: u32 id |
| 1e | s>c | relay ticket: str relay url, str code, 8 token, u32 member id (host 0), str version, u64 unix time, vec 64-byte signature |
| 21 | s>c | member country: u32 id, str country code |

List entry: str code, str name, str description, str host hero, u8 class,
str zone, u8 players, u8 locked, u8 open, str country, vec classes, vec lacks.

The host keeps its lobby connection open for the whole run.

## Relay (ticket url, official `wss://coop3.aethus.pl:8443/coop/`)

| Type | Dir | Body |
|---|---|---|
| 0b | c>s | auth: the ticket from code onward (str code, 8 token, u32 id, str version, u64 time, vec sig) |
| 06 | c>s | send: u32 to, vec payload |
| 1a | s>c | deliver: u32 from, vec payload |
| 07 | c>s | host kicks: u32 id (relay answers 19) |
| 08 | c>s | leave |
| 19 | s>c | peer gone: u32 id |
| 1f | s>c | peer dropped, may reconnect: u32 id |
| 20 | s>c | peer back: u32 id |
| 09 / 1c | | ping / pong |

Star topology: guests send to id 0 (host), the host sends to each guest.
Payloads are opaque to the relay. A dropped guest was held for about 50 s
before 19 arrived. What guests receive when the host drops is unconfirmed
(the server sends them 1f/20/19 with id 0).
