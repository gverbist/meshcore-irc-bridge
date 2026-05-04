# meshcore-irc-bridge

An IRC server bridge on top of MeshCore — use your favourite IRC client to join the mesh.

## Features

- All MeshCore channels discovered automatically and mapped to IRC channels (`#public`, `#radio-actief`, …)
- All channels auto-joined on connect
- Private messages using contact names as IRC nicks
- Bidirectional `@mention` translation between IRC and MeshCore
- Node advertisements and new-node discoveries delivered as IRC `NOTICE` messages
- `/whois` shows full public key, GPS coordinates with OpenStreetMap link, and hop count
- `*MeshCore` bot for node and contact management (`nodeinfo`, `contacts`, `addcontact`, …)
- Voice status (`+v`) for active mesh senders
- Server password authentication
- Multiple simultaneous IRC clients
- Serial, BLE, and TCP connection support
- INI config file support

## Quick Start

```bash
git clone https://github.com/gverbist/meshcore-irc-bridge.git
cd meshcore-irc-bridge
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python meshcore-irc-bridge.py --serial /dev/ttyACM0
```

Connect your IRC client to `127.0.0.1:6667`. All channels are auto-joined on connect.

## Docker

The recommended way to run in production. Docker files live in a separate directory:

```bash
cd ~/Projects/meshcore/irc-bridge
docker compose up -d
```

Edit `bridge.cfg` and run `docker compose restart` to apply config changes without rebuilding. To update the code:

```bash
docker compose build --no-cache && docker compose up -d
```

## Configuration

All options can be set via a config file or command line. Command line takes precedence.

```ini
[log]
debug = false

[meshcore]
serial = /dev/ttyACM0
baudrate = 115200
max_msg_len = 200

[irc]
host = 127.0.0.1
port = 6667
# password = secret
voice_timeout = 600
```

```bash
python meshcore-irc-bridge.py --config bridge.cfg
```

See `bridge.cfg.example` for a full template. Full option reference in the [wiki](https://github.com/gverbist/meshcore-irc-bridge/wiki/Configuration).

### Command line options

| Option | Default | Description |
|---|---|---|
| `--serial <path>` | | USB serial port |
| `--ble <address>` | | BLE device address |
| `--tcp <host:port>` | | TCP address |
| `--host` | `127.0.0.1` | IRC server bind address |
| `--port` | `6667` | IRC server port |
| `--password` | | IRC server password |
| `--voice-timeout` | `600` | Seconds before revoking +v |
| `--max-msg-len` | `200` | Max outgoing message length (bytes) |
| `--config` | | Path to INI config file |
| `-v` / `--verbose` | | Debug output |

## IRC Client Setup

Channels are auto-joined on connect — no `autojoin` setting needed.

**weechat**
```
/server add meshcore 127.0.0.1/6667 -notls
/set irc.server.meshcore.autoconnect on
/set irc.server.meshcore.nicks yournick
/connect meshcore
```

**irssi**
```
/network add meshcore -nick yournick
/server add -network meshcore -nocap -auto 127.0.0.1 6667
/connect meshcore
```

## Bot Commands

Send commands to the `*MeshCore` bot via direct message:

```
/msg *MeshCore help
/msg *MeshCore nodeinfo
/msg *MeshCore contacts
/msg *MeshCore advert
/msg *MeshCore addcontact <nick>
/msg *MeshCore removecontact <nick>
```

## Wiki

Full documentation in the [GitHub wiki](https://github.com/gverbist/meshcore-irc-bridge/wiki):

- [Architecture](https://github.com/gverbist/meshcore-irc-bridge/wiki/Architecture)
- [Configuration](https://github.com/gverbist/meshcore-irc-bridge/wiki/Configuration)
- [Channel Mapping](https://github.com/gverbist/meshcore-irc-bridge/wiki/Channel-Mapping)
- [Bot Commands](https://github.com/gverbist/meshcore-irc-bridge/wiki/Bot-Commands)
- [Docker](https://github.com/gverbist/meshcore-irc-bridge/wiki/Docker)

## Acknowledgements

Forked from [daniel-j-h/meshcore-irc-bridge](https://github.com/daniel-j-h/meshcore-irc-bridge).

Additional features inspired by the independent implementation at [meshcore.on1aff.be](https://meshcore.on1aff.be/MeshCoreIRC.html) by ON1AFF.

## License

Copyright © 2026 Daniel J. Hofmann

Distributed under the MIT License (MIT).
