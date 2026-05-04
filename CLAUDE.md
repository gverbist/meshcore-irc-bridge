# meshcore-irc-bridge

## Overview
Fork of [daniel-j-h/meshcore-irc-bridge](https://github.com/daniel-j-h/meshcore-irc-bridge) — a Python IRC server bridge on top of MeshCore.

- **Fork:** https://github.com/gverbist/meshcore-irc-bridge
- **Upstream:** https://github.com/daniel-j-h/meshcore-irc-bridge
- **Language:** Python

## Goal
Extend the bridge to support multiple Meshcore channels, not just `#public`.

## How It Works
The bridge implements a basic IRC server (RFC 1459) that translates between IRC client commands and the Meshcore mesh network over USB serial.

- Serial device: `/dev/ttyACM0` (LilyGO T-Echo running USB companion firmware)
- IRC server listens on `127.0.0.1:6667`
- Currently: `#public` maps to Meshcore Public channel only

## Running Locally (without Docker)
```bash
python meshcore-irc-bridge.py --serial /dev/ttyACM0
```
Add `--verbose` for protocol-level debug output.

## Running via Docker (production)
Docker compose lives in the meshcore admin project:
- `~/Projects/meshcore/irc-bridge/`

## Environment
- **OS:** Arch Linux
- **Python:** 3.12
- **IRC client in use:** weechat

## Rules

### Git & GitHub
- Never mention Claude or AI in commit messages, PR titles, or descriptions
- Never push, merge, or open/close PRs without explicit user confirmation — every time, no exceptions
- Follow git best practices: atomic commits, descriptive messages in imperative mood, feature branches for all changes, PRs for merging into main

### Wiki
- Maintain the GitHub wiki for this repo and keep it up to date as features are added or changed
- Document architecture, configuration, channel mapping, and usage

### Docker
- The final deliverable must run in a Docker container
- The Docker setup lives in `~/Projects/meshcore/irc-bridge/` and must be kept in sync with code changes
