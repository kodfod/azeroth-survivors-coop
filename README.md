# Azeroth Survivors co-op server

Host your own co-op server for **Azeroth Survivors**, so you and your
friends play together without the official co-op servers.

One small program does both jobs the official servers do: the **lobby**
(list, host, join by code) and the **relay** (passes the game data between
the host and the guests). The game itself still runs on the host's PC, as
usual.

> **Unofficial.** Not made by or affiliated with the game's developer. It
> was built by watching the game's own network messages (see
> [PROTOCOL.md](PROTOCOL.md)) and works with game version **0.127.0**. A game
> update can change the protocol and break it.

---

## Who needs what

| | Server PC (one person) | Every player (including the host) |
|---|---|---|
| Needs | `AzerothCoopServer.exe` | the game, plus `LaunchCustomRelay.bat` in the game folder |
| OS | Windows 10/11 | Windows 10/11 |
| Python | not needed (only if you run from source) | not needed |
| Network | friends must be able to reach port **8765** (TCP), see [Networking](#networking) | can reach the server PC |

The server can run on the host's own PC (the usual setup) or on any other
PC the players can reach.

---

## 1. Start the server (one person)

1. Download `AzerothCoopServer.exe` from the
   [Releases](../../releases) page and put it in any folder.
2. Double-click it.
   - Windows SmartScreen may warn that the app is unrecognized: click
     **More info** → **Run anyway** (the exe isn't code-signed).
   - Windows Firewall asks whether to allow it: allow **Private networks**
     (and **Public** too if friends connect over the internet).
3. Leave the window open while you play. It prints the addresses to give
   your friends, for example:

   ```text
   co-op server on port 8765; you use ws://127.0.0.1:8765/coop/lobby/
   UPnP: router forwards TCP 8765 to 192.168.0.10:8765
   reachability: port 8765 IS reachable from the internet
     friends use: ws://203.0.113.7:8765/coop/lobby/
     same network: ws://192.168.0.10:8765/coop/lobby/
     over Tailscale: ws://100.101.102.103:8765/coop/lobby/
   ```

Close the window (or press Ctrl+C) to stop the server.

## 2. Launch the game through the server (every player)

1. Download `LaunchCustomRelay.bat` (from [client/](client/) or the
   Releases page).
2. Put it in your **game folder**, next to `AzerothSurvivors.exe`.
3. Double-click it. It asks for the server address:
   - **The person running the server:** just press **Enter**
     (the default `ws://127.0.0.1:8765/coop/lobby/` means "this PC").
   - **Everyone else:** type the address the server window printed, for
     example `ws://203.0.113.7:8765/coop/lobby/`, and press Enter.
4. The game starts. Open co-op and **host** or **join** as usual: the lobby
   list now shows only the lobbies on your server.

Starting the game the normal way still uses the official servers. Only
launches through `LaunchCustomRelay.bat` use yours.

---

## Networking

Friends must be able to open a connection to the server PC on TCP port
8765. Pick whichever works for you:

| Situation | What to do |
|---|---|
| Everyone on the same home network | Use the **same network** address. Nothing else needed. |
| Over the internet, router has UPnP | Nothing: the server asks the router to open the port at start-up and closes it again on exit. |
| Over the internet, no UPnP | Forward **TCP 8765** on your router to the server PC (search "port forward" plus your router model). |
| Carrier-grade NAT, mobile internet, or nothing above works | Use [Tailscale](https://tailscale.com/download) (free): everyone installs it and joins the same tailnet, then uses the **over Tailscale** address. No router changes. |

The server checks this for you at start-up:

- **UPnP:** asks the router to forward the port. If the router's own
  internet address isn't public, it tells you that forwarding can't work
  there (carrier-grade NAT), so use Tailscale.
- **Reachability:** asks [ifconfig.co](https://ifconfig.co) to connect back
  to the port and reports whether it got through. This shares your public IP
  with that site; skip it with `--no-check`.

NAT punch-through isn't possible: the game only opens a plain WebSocket
connection to the address it's given, so it can't take part in hole
punching.

**Only share your public address with people you trust**, and close the
server when you're done.

---

## Options

Run from a command prompt, for example
`AzerothCoopServer.exe --port 9000 --no-check`:

| Option | Meaning |
|---|---|
| `--port 8765` | port to listen on (players then use that port in the address) |
| `--bind 0.0.0.0` | network address to listen on (`127.0.0.1` = this PC only) |
| `--public-host ADDR:PORT` | address put in relay tickets; by default, whatever address each player used to reach the server |
| `--no-upnp` | don't ask the router to open the port |
| `--no-check` | skip the reachability check with ifconfig.co |

Each run writes a log to `logs\server-<date>-<time>.log` next to the exe:
connections, joins, drops and every control message (not the game data).

---

## Troubleshooting

| Problem | Try |
|---|---|
| "the co-op service cannot be reached" | The server isn't running, the address is mistyped (it must start with `ws://` and end with `/coop/lobby/`), or the port is blocked. On the server PC, check the address with the default `127.0.0.1` first. |
| Friends can't connect, you can | See [Networking](#networking). Check the Windows Firewall allowed the exe (Windows Security → Firewall → Allow an app). |
| The server window closes right away | Another program already uses the port: run it from a command prompt to read the error, or pick another `--port`. |
| "a different version of the game" | Everyone must be on the same game version. |
| Antivirus deletes the exe | Exes built with PyInstaller are sometimes falsely flagged. Run from source instead (below), or allow the file. |

## Known limitations

- **A guest ending the run mid-game doesn't leave.** Closing that game
  window works; the server frees the slot after 60 seconds.
- Error replies (wrong password, party full, ...) use a guessed message type
  and may show a generic error in the game.
- No country flags in the lobby.
- Tested with game version 0.127.0 only.

---

## Running from source

Needs [Python 3.10+](https://www.python.org/downloads/).

```bash
pip install -r requirements.txt
python coop_server.py
```

`StartServer.bat` does the same with a double-click.

### Building the exe

```bash
build.bat
```

This installs PyInstaller and writes `dist\AzerothCoopServer.exe`.

## Files

| Path | What |
|---|---|
| `coop_server.py` | the server: lobby + relay |
| `nat.py` | UPnP, reachability check, addresses to share |
| `client/LaunchCustomRelay.bat` | game launcher for players |
| `tools/relay_proxy.py` | developer tool: logs the traffic between the game and the **official** servers, used to work out the protocol ([PROTOCOL.md](PROTOCOL.md)) |

The proxy's logs contain lobby names, player names and passwords of
everyone in the lobbies it saw. Don't share them.
