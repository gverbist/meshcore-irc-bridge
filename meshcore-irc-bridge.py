#!/usr/bin/env python3

import asyncio
import argparse
import configparser
import logging
import os
import re
import time

from meshcore import MeshCore, EventType

logger = logging.getLogger(__name__)

MAX_CHANNELS = 40
CONSECUTIVE_EMPTY_LIMIT = 5
BOT_NICK = "*MeshCore"
SERVER_NAME = "mesh"


def _to_irc_channel(name: str) -> str:
    sanitized = re.sub(r'[^a-z0-9\-_]', '', name.lower().replace(' ', '-'))
    return f"#{sanitized or 'channel'}"


def _to_irc_nick(name: str) -> str:
    """Convert a MeshCore node name to a valid IRC nick."""
    sanitized = re.sub(r'[^a-zA-Z0-9_\-\[\]\\`^{}|]', '_', name.strip())
    if not sanitized:
        return 'mesh'
    if sanitized[0].isdigit() or sanitized[0] == '-':
        sanitized = '_' + sanitized
    return sanitized


class Bridge:
    def __init__(self, meshcore, password=None, voice_timeout=600, max_msg_len=200):
        self.mesh = meshcore
        self.password = password
        self.voice_timeout = voice_timeout
        self.max_msg_len = max_msg_len

        self.clients = []

        self._chan_to_irc = {}  # channel_idx -> irc channel name
        self._irc_to_chan = {}  # irc channel name -> channel_idx

        # Nick registry: irc_nick <-> adv_name
        self._nick_to_adv = {}  # irc_nick -> adv_name
        self._adv_to_nick = {}  # adv_name -> irc_nick

        # Voice: (nick, channel) -> expiry timestamp
        self._voiced = {}

    # ------------------------------------------------------------------ #
    # Nick / mention helpers                                               #
    # ------------------------------------------------------------------ #

    def _register_nick(self, adv_name: str) -> str:
        """Register adv_name in the nick registry and return the irc nick."""
        nick = _to_irc_nick(adv_name)
        self._nick_to_adv[nick] = adv_name
        self._adv_to_nick[adv_name] = nick
        return nick

    def _resolve_contact_nick(self, pubkey_prefix: str) -> str:
        """Return an IRC nick for a pubkey prefix, using adv_name when available."""
        contact = self.mesh.get_contact_by_key_prefix(pubkey_prefix)
        if contact:
            adv_name = contact.get("adv_name", "").strip()
            if adv_name:
                return self._register_nick(adv_name)
        return pubkey_prefix[:12]

    def _translate_outgoing(self, msg: str) -> str:
        """Translate IRC @Nick mentions to MeshCore @[adv_name] format."""
        def replace(m):
            adv_name = self._nick_to_adv.get(m.group(1))
            return f"@[{adv_name}]" if adv_name else m.group(0)
        return re.sub(r'@([a-zA-Z0-9_\-\[\]\\`^{}|]+)', replace, msg)

    def _translate_incoming(self, msg: str) -> str:
        """Translate MeshCore @[adv_name] mentions to IRC @Nick format."""
        def replace(m):
            nick = self._adv_to_nick.get(m.group(1))
            return f"@{nick}" if nick else m.group(0)
        return re.sub(r'@\[([^\]]+)\]', replace, msg)

    # ------------------------------------------------------------------ #
    # Voice status                                                         #
    # ------------------------------------------------------------------ #

    def _grant_voice(self, nick: str, channel: str):
        key = (nick, channel)
        expiry = time.time() + self.voice_timeout
        already_voiced = key in self._voiced
        self._voiced[key] = expiry
        if not already_voiced:
            asyncio.create_task(self._broadcast_mode(channel, "+v", nick))
            asyncio.create_task(self._revoke_voice_after(nick, channel, expiry))

    async def _revoke_voice_after(self, nick: str, channel: str, expiry: float):
        await asyncio.sleep(self.voice_timeout)
        key = (nick, channel)
        if self._voiced.get(key, 0) <= expiry:
            self._voiced.pop(key, None)
            await self._broadcast_mode(channel, "-v", nick)

    async def _broadcast_mode(self, target: str, mode: str, arg: str):
        for client in self.clients:
            await client.send(f":{SERVER_NAME} MODE {target} {mode} {arg}")

    # ------------------------------------------------------------------ #
    # Broadcast helpers                                                    #
    # ------------------------------------------------------------------ #

    async def _broadcast(self, msg: str):
        for client in self.clients:
            await client.send(msg)

    async def _notice_all(self, text: str):
        for client in self.clients:
            await client.send(f":{SERVER_NAME} NOTICE {client.nick} :{text}")

    # ------------------------------------------------------------------ #
    # Startup                                                              #
    # ------------------------------------------------------------------ #

    async def start(self, host: str, port: int):
        await self.mesh.ensure_contacts()
        self._build_nick_registry()
        await self._discover_channels()

        self.mesh.subscribe(EventType.CHANNEL_MSG_RECV, self._on_mesh_channel_msg)
        self.mesh.subscribe(EventType.CONTACT_MSG_RECV, self._on_mesh_private_msg)
        self.mesh.subscribe(EventType.NEW_CONTACT, self._on_new_contact)
        self.mesh.subscribe(EventType.ADVERTISEMENT, self._on_advertisement)
        await self.mesh.start_auto_message_fetching()

        server = await asyncio.start_server(self._handle_client, host, port)
        logger.info(f"IRC server listening on {host}:{port}")
        async with server:
            await server.serve_forever()

    def _build_nick_registry(self):
        for contact in self.mesh.contacts.values():
            adv_name = contact.get("adv_name", "").strip()
            if adv_name:
                self._register_nick(adv_name)

    async def _discover_channels(self):
        consecutive_empty = 0
        for idx in range(MAX_CHANNELS):
            try:
                event = await self.mesh.commands.get_channel(idx)
            except Exception as e:
                logger.warning(f"Failed to query channel {idx}: {e}")
                consecutive_empty += 1
                if consecutive_empty >= CONSECUTIVE_EMPTY_LIMIT:
                    break
                continue

            if event.type != EventType.CHANNEL_INFO:
                consecutive_empty += 1
                if consecutive_empty >= CONSECUTIVE_EMPTY_LIMIT:
                    break
                continue

            name = event.payload.get("channel_name", "").strip("\x00").strip()
            if not name:
                consecutive_empty += 1
                if consecutive_empty >= CONSECUTIVE_EMPTY_LIMIT:
                    break
                continue

            consecutive_empty = 0
            irc_name = _to_irc_channel(name)
            if irc_name in self._irc_to_chan:
                irc_name = f"{irc_name}-{idx}"

            self._chan_to_irc[idx] = irc_name
            self._irc_to_chan[irc_name] = idx
            logger.info(f"Discovered channel {idx}: {name!r} -> {irc_name}")

        if not self._chan_to_irc:
            self._chan_to_irc[0] = "#public"
            self._irc_to_chan["#public"] = 0
            logger.info("No channels discovered, defaulting to #public")

    def _register_channel(self, idx: int) -> str:
        irc_name = f"#channel-{idx}"
        self._chan_to_irc[idx] = irc_name
        self._irc_to_chan[irc_name] = idx
        logger.info(f"Auto-registered unknown channel {idx} as {irc_name}")
        return irc_name

    # ------------------------------------------------------------------ #
    # Mesh event handlers                                                  #
    # ------------------------------------------------------------------ #

    async def _on_mesh_channel_msg(self, event):
        if not self.clients:
            return

        payload = event.payload
        if payload.get("type") != "CHAN" or payload.get("txt_type") != 0:
            return

        channel_idx = payload.get("channel_idx")
        raw_text = payload.get("text")
        if channel_idx is None or not raw_text:
            return

        irc_channel = self._chan_to_irc.get(channel_idx) or self._register_channel(channel_idx)

        # MeshCore channel messages are formatted as "SenderName: message text"
        colon_pos = raw_text.find(":")
        if colon_pos > 0:
            sender_name = raw_text[:colon_pos].strip()
            message_text = raw_text[colon_pos + 1:].strip()
            nick = _to_irc_nick(sender_name) if sender_name else "mesh"
        else:
            nick = "mesh"
            message_text = raw_text

        message_text = self._translate_incoming(message_text)
        message_text = message_text.replace("\r", "").replace("\n", " ").replace("\0", "")

        if nick != "mesh":
            self._grant_voice(nick, irc_channel)

        await self._broadcast(f":{nick}!{nick}@mesh PRIVMSG {irc_channel} :{message_text}")

    async def _on_mesh_private_msg(self, event):
        if not self.clients:
            return

        payload = event.payload
        if payload.get("type") != "PRIV" or payload.get("txt_type") != 0:
            return

        pubkey = payload.get("pubkey_prefix")
        text = payload.get("text")
        if not pubkey or not text:
            return

        nick = self._resolve_contact_nick(pubkey)
        message = self._translate_incoming(text.strip())
        message = message.replace("\r", "").replace("\n", " ").replace("\0", "")

        for client in self.clients:
            await client.send(f":{nick}!{nick}@mesh PRIVMSG {client.nick} :{message}")

    async def _on_new_contact(self, event):
        """Announce newly discovered nodes as server NOTICEs."""
        c = event.payload
        adv_name = c.get("adv_name", "").strip() or "unknown"
        pubkey_short = c.get("public_key", "")[:16]
        lat = c.get("adv_lat", 0.0)
        lon = c.get("adv_lon", 0.0)

        self._register_nick(adv_name)

        notice = f"New node: {adv_name} [{pubkey_short}]"
        if lat or lon:
            notice += f" at {lat:.4f},{lon:.4f}"
        notice += f" — use: /msg {BOT_NICK} addcontact {_to_irc_nick(adv_name)}"

        await self._notice_all(notice)

    async def _on_advertisement(self, event):
        """Announce advertisements from known contacts as server NOTICEs."""
        pubkey = event.payload.get("public_key", "")
        contact = self.mesh.contacts.get(pubkey)
        if not contact:
            return

        adv_name = contact.get("adv_name", "").strip()
        if not adv_name:
            return

        self._register_nick(adv_name)
        lat = contact.get("adv_lat", 0.0)
        lon = contact.get("adv_lon", 0.0)

        notice = f"Advert: {adv_name} [{pubkey[:16]}]"
        if lat or lon:
            notice += f" at {lat:.4f},{lon:.4f}"

        await self._notice_all(notice)

    # ------------------------------------------------------------------ #
    # IRC client connection                                                #
    # ------------------------------------------------------------------ #

    async def _handle_client(self, reader, writer):
        logger.debug("IRC client connected")
        client = Client(reader, writer)
        self.clients.append(client)

        try:
            async for line in reader:
                msg = line.decode("utf-8", errors="ignore").strip()
                if msg:
                    await self._handle_irc_msg(client, msg)
        except ConnectionResetError:
            pass
        except Exception as e:
            logger.exception(e)
        finally:
            await client.close()
            if client in self.clients:
                self.clients.remove(client)

        logger.debug("IRC client disconnected")

    async def _handle_irc_msg(self, client, msg):
        logger.debug(f" < {msg}")

        parts = msg.split(" ", 1)
        cmd = parts[0].upper()
        params = parts[1] if len(parts) > 1 else ""

        match cmd:
            case "PASS":
                await self._handle_pass(client, params)
            case "NICK":
                await self._handle_nick(client, params)
            case "USER":
                await self._handle_user(client, params)
            case "QUIT":
                await self._handle_quit(client, params)
            case "JOIN":
                await self._handle_join(client, params)
            case "PART":
                await self._handle_part(client, params)
            case "MODE":
                await self._handle_mode(client, params)
            case "LIST":
                await self._handle_list(client, params)
            case "PRIVMSG":
                await self._handle_privmsg(client, params)
            case "WHOIS":
                await self._handle_whois(client, params)
            case "WHO":
                await self._handle_who(client, params)
            case "PING":
                await self._handle_ping(client, params)
            case _:
                pass

    # ------------------------------------------------------------------ #
    # IRC command handlers                                                 #
    # ------------------------------------------------------------------ #

    async def _handle_pass(self, client, params):
        if client.registered:
            await client.send(f":{SERVER_NAME} 462 {client.nick} :You may not reregister")
            return
        client.password = params.split(" ")[0] if params else ""

    async def _handle_nick(self, client, params):
        if not params:
            await client.send(f":{SERVER_NAME} 431 {client.nick} :No nickname given")
            return

        nick = params.split(" ")[0]
        if nick.startswith("#"):
            await client.send(f":{SERVER_NAME} 432 {client.nick} {nick} :Erroneous nickname")
            return

        client.nick = nick

    async def _handle_user(self, client, params):
        if client.registered:
            await client.send(f":{SERVER_NAME} 462 {client.nick} :You may not reregister")
            return

        if not params:
            await client.send(f":{SERVER_NAME} 461 {client.nick} USER :Not enough parameters")
            return

        if self.password and client.password != self.password:
            await client.send(f":{SERVER_NAME} 464 {client.nick} :Password incorrect")
            await client.close()
            return

        client.user = params.split(" ")[0]

        self_info = self.mesh.self_info
        node_name = self_info.get("name", "unknown") if self_info else "unknown"
        contacts = self.mesh.contacts
        channels = sorted(self._irc_to_chan.keys())

        await client.send(f":{SERVER_NAME} 001 {client.nick} :Welcome to MeshCore IRC Bridge")
        await client.send(f":{SERVER_NAME} 002 {client.nick} :Node: {node_name} | Contacts: {len(contacts)}")
        await client.send(f":{SERVER_NAME} 003 {client.nick} :MeshCore IRC Bridge")
        await client.send(f":{SERVER_NAME} 004 {client.nick} {SERVER_NAME} MeshCore-IRC-Bridge o o")
        await client.send(f":{SERVER_NAME} 375 {client.nick} :- {SERVER_NAME} Message of the Day -")
        await client.send(f":{SERVER_NAME} 372 {client.nick} :- Channels:")
        for chan in channels:
            idx = self._irc_to_chan[chan]
            await client.send(f":{SERVER_NAME} 372 {client.nick} :-   [{idx}] {chan}")
        await client.send(f":{SERVER_NAME} 372 {client.nick} :-")
        await client.send(f":{SERVER_NAME} 372 {client.nick} :- DM /msg {BOT_NICK} help for bot commands")
        await client.send(f":{SERVER_NAME} 376 {client.nick} :End of MOTD")

        # Auto-join all channels
        for chan in channels:
            client.channels.add(chan)
            await client.send(f":{client.prefix} JOIN {chan}")
            await client.send(f":{SERVER_NAME} 332 {client.nick} {chan} :MeshCore {chan.lstrip('#').title()} Channel")
            await client.send(f":{SERVER_NAME} 353 {client.nick} = {chan} :{client.nick} {BOT_NICK}")
            await client.send(f":{SERVER_NAME} 366 {client.nick} {chan} :End of /NAMES list")

    async def _handle_quit(self, client, params):
        await client.send(f":{client.prefix} QUIT :{client.nick}")

    async def _handle_join(self, client, params):
        if not params:
            await client.send(f":{SERVER_NAME} 461 {client.nick} JOIN :Not enough parameters")
            return

        channels = params.split(" ", 1)[0]
        for chan in channels.split(","):
            if self._irc_to_chan.get(chan) is not None:
                client.channels.add(chan)
                await client.send(f":{client.prefix} JOIN {chan}")
                await client.send(f":{SERVER_NAME} 332 {client.nick} {chan} :MeshCore {chan.lstrip('#').title()} Channel")
                await client.send(f":{SERVER_NAME} 353 {client.nick} = {chan} :{client.nick} {BOT_NICK}")
                await client.send(f":{SERVER_NAME} 366 {client.nick} {chan} :End of /NAMES list")
            else:
                await client.send(f":{SERVER_NAME} 403 {client.nick} {chan} :No such channel")

    async def _handle_part(self, client, params):
        if not params:
            await client.send(f":{SERVER_NAME} 461 {client.nick} PART :Not enough parameters")
            return

        channels = params.split(" ", 1)[0]
        for chan in channels.split(","):
            if chan not in client.channels:
                await client.send(f":{SERVER_NAME} 442 {client.nick} {chan} :You're not on that channel")
                continue
            await client.send(f":{client.prefix} PART {chan}")
            client.channels.remove(chan)

    async def _handle_mode(self, client, params):
        if not params:
            await client.send(f":{SERVER_NAME} 461 {client.nick} MODE :Not enough parameters")
            return

        target = params.split(" ")[0]
        if target.startswith("#"):
            if target not in client.channels:
                await client.send(f":{SERVER_NAME} 442 {client.nick} {target} :You're not on that channel")
                return
            if target in self._irc_to_chan:
                await client.send(f":{SERVER_NAME} 324 {client.nick} {target} +nt")
        else:
            await client.send(f":{SERVER_NAME} 502 {client.nick} :Cant change mode for other users")

    async def _handle_list(self, client, params):
        channels_to_list = sorted(self._irc_to_chan.keys())

        if params:
            requested = params.split(",")
            for chan in requested:
                if chan not in self._irc_to_chan:
                    await client.send(f":{SERVER_NAME} 403 {client.nick} {chan} :No such channel")
            channels_to_list = [c for c in requested if c in self._irc_to_chan]

        await client.send(f":{SERVER_NAME} 321 {client.nick} Channel :Users Name")
        for chan in channels_to_list:
            await client.send(f":{SERVER_NAME} 322 {client.nick} {chan} 0 :{chan.lstrip('#').title()}")
        await client.send(f":{SERVER_NAME} 323 {client.nick} :End of LIST")

    async def _handle_privmsg(self, client, params):
        target, _, msg = params.partition(" ")
        msg = msg.lstrip(":")

        if not msg:
            await client.send(f":{SERVER_NAME} 412 {client.nick} :No text to send")
            return

        if target == BOT_NICK:
            await self._handle_bot_command(client, msg)
            return

        if target.startswith("#"):
            channel_idx = self._irc_to_chan.get(target)
            if channel_idx is None:
                await client.send(f":{SERVER_NAME} 401 {client.nick} {target} :No such nick/channel")
                return

            translated = self._translate_outgoing(msg)
            if len(translated.encode("utf-8")) > self.max_msg_len:
                await client.send(f":{SERVER_NAME} NOTICE {client.nick} :Msg too long: {len(translated.encode('utf-8'))}/{self.max_msg_len}")
                return

            result = await self.mesh.commands.send_chan_msg(channel_idx, translated)
            if result.type == EventType.ERROR:
                await client.send(f":{SERVER_NAME} NOTICE {client.nick} :Failed to send message to {target}")

        elif target == client.nick:
            await client.send(f":{client.prefix} PRIVMSG {client.nick} :{msg}")

        else:
            adv_name = self._nick_to_adv.get(target)
            contact = self.mesh.get_contact_by_name(adv_name) if adv_name else None
            if not contact:
                contact = self.mesh.get_contact_by_key_prefix(target)

            if not contact:
                await client.send(f":{SERVER_NAME} 401 {client.nick} {target} :No such nick/channel")
                return

            translated = self._translate_outgoing(msg)
            if len(translated.encode("utf-8")) > self.max_msg_len:
                await client.send(f":{SERVER_NAME} NOTICE {client.nick} :Msg too long: {len(translated.encode('utf-8'))}/{self.max_msg_len}")
                return

            result = await self.mesh.commands.send_msg_with_retry(contact, translated)
            if not result:
                await client.send(f":{SERVER_NAME} NOTICE {client.nick} :Failed to send message to {target}")

    # ------------------------------------------------------------------ #
    # Bot commands (/msg *MeshCore <cmd>)                                  #
    # ------------------------------------------------------------------ #

    async def _handle_bot_command(self, client, msg):
        parts = msg.strip().split()
        if not parts:
            return

        cmd = parts[0].lower()
        args = parts[1:]

        def bot(text):
            return f":{BOT_NICK}!bot@mesh PRIVMSG {client.nick} :{text}"

        match cmd:
            case "help":
                for line in [
                    "Available commands:",
                    "  nodeinfo              — show own node details",
                    "  contacts [filter]     — list saved contacts",
                    "  advert                — send advertisement",
                    "  addcontact <nick>     — save a discovered node",
                    "  removecontact <nick>  — remove a saved contact",
                ]:
                    await client.send(bot(line))

            case "nodeinfo":
                if not self.mesh.self_info:
                    await self.mesh.commands.send_device_query()
                info = self.mesh.self_info
                if info:
                    name = info.get("name", "unknown")
                    pubkey = info.get("public_key", "")
                    lat = info.get("adv_lat", 0.0)
                    lon = info.get("adv_lon", 0.0)
                    freq = info.get("radio_freq", 0)
                    bw = info.get("radio_bw", 0)
                    sf = info.get("radio_sf", 0)
                    tx = info.get("tx_power", 0)
                    await client.send(bot(f"Name: {name}"))
                    await client.send(bot(f"Key:  {pubkey}"))
                    await client.send(bot(f"Coords: {lat:.6f}, {lon:.6f}"))
                    await client.send(bot(f"Radio: {freq} MHz  BW {bw} kHz  SF{sf}  TX {tx} dBm"))
                else:
                    await client.send(bot("Node info unavailable"))

            case "contacts":
                filter_str = args[0].lower() if args else ""
                count = 0
                for c in self.mesh.contacts.values():
                    adv_name = c.get("adv_name", "").strip()
                    if not adv_name:
                        continue
                    if filter_str and filter_str not in adv_name.lower():
                        continue
                    nick = _to_irc_nick(adv_name)
                    pk = c.get("public_key", "")[:16]
                    lat = c.get("adv_lat", 0.0)
                    lon = c.get("adv_lon", 0.0)
                    coords = f"  {lat:.4f},{lon:.4f}" if (lat or lon) else ""
                    await client.send(bot(f"  {nick}  [{pk}]{coords}"))
                    count += 1
                if count == 0:
                    await client.send(bot("No contacts found"))
                else:
                    await client.send(bot(f"Total: {count}"))

            case "advert":
                result = await self.mesh.commands.send_advert()
                if result.type == EventType.ERROR:
                    await client.send(bot("Failed to send advertisement"))
                else:
                    await client.send(bot("Advertisement sent"))

            case "addcontact":
                if not args:
                    await client.send(bot("Usage: addcontact <nick>"))
                    return
                nick = args[0]
                adv_name = self._nick_to_adv.get(nick, nick)
                found = next(
                    (c for c in self.mesh.pending_contacts.values()
                     if c.get("adv_name", "").strip() == adv_name),
                    None,
                )
                if not found:
                    await client.send(bot(f"Node {nick!r} not found in discovered nodes"))
                    return
                result = await self.mesh.commands.add_contact(found)
                if result.type == EventType.ERROR:
                    await client.send(bot(f"Failed to add {nick}"))
                else:
                    await self.mesh.ensure_contacts(follow=True)
                    await client.send(bot(f"Contact {nick} added"))

            case "removecontact":
                if not args:
                    await client.send(bot("Usage: removecontact <nick>"))
                    return
                nick = args[0]
                adv_name = self._nick_to_adv.get(nick, nick)
                contact = self.mesh.get_contact_by_name(adv_name)
                if not contact:
                    await client.send(bot(f"Contact {nick!r} not found"))
                    return
                result = await self.mesh.commands.remove_contact(contact["public_key"])
                if result.type == EventType.ERROR:
                    await client.send(bot(f"Failed to remove {nick}"))
                else:
                    await self.mesh.ensure_contacts(follow=True)
                    await client.send(bot(f"Contact {nick} removed"))

            case _:
                await client.send(bot(f"Unknown command: {cmd!r}  — try 'help'"))

    # ------------------------------------------------------------------ #
    # WHOIS / WHO / PING                                                   #
    # ------------------------------------------------------------------ #

    async def _handle_whois(self, client, params):
        parts = params.split(" ")
        if not parts or not parts[-1]:
            await client.send(f":{SERVER_NAME} 431 {client.nick} :No nickname given")
            return

        target = parts[-1]

        if target == client.nick:
            await client.send(f":{SERVER_NAME} 311 {client.nick} {client.nick} {client.user} mesh * :{client.nick}")
            await client.send(f":{SERVER_NAME} 312 {client.nick} {client.nick} mesh :MeshCore IRC Bridge")
            if client.channels:
                await client.send(f":{SERVER_NAME} 319 {client.nick} {client.nick} :{' '.join(sorted(client.channels))}")
            await client.send(f":{SERVER_NAME} 318 {client.nick} {client.nick} :End of /WHOIS list")
            return

        if target == BOT_NICK:
            await client.send(f":{SERVER_NAME} 311 {client.nick} {BOT_NICK} bot mesh * :MeshCore Bot")
            await client.send(f":{SERVER_NAME} 312 {client.nick} {BOT_NICK} mesh :MeshCore IRC Bridge")
            await client.send(f":{SERVER_NAME} 318 {client.nick} {BOT_NICK} :End of /WHOIS list")
            return

        adv_name = self._nick_to_adv.get(target, target)
        contact = self.mesh.get_contact_by_name(adv_name) or self.mesh.get_contact_by_key_prefix(target)

        if contact:
            adv_name = contact.get("adv_name", target).strip()
            nick = _to_irc_nick(adv_name)
            pubkey = contact.get("public_key", "")
            lat = contact.get("adv_lat", 0.0)
            lon = contact.get("adv_lon", 0.0)
            path_len = contact.get("out_path_len", -1)

            await client.send(f":{SERVER_NAME} 311 {client.nick} {nick} {adv_name} mesh * :{adv_name}")
            await client.send(f":{SERVER_NAME} 312 {client.nick} {nick} mesh :MeshCore Node")
            if pubkey:
                await client.send(f":{SERVER_NAME} 320 {client.nick} {nick} :Key: {pubkey}")
            if lat or lon:
                osm = f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}"
                await client.send(f":{SERVER_NAME} 320 {client.nick} {nick} :Location: {lat:.6f},{lon:.6f}  {osm}")
            if path_len >= 0:
                hops = "direct" if path_len == 0 else f"{path_len} hop(s)"
                await client.send(f":{SERVER_NAME} 320 {client.nick} {nick} :Path: {hops}")
            await client.send(f":{SERVER_NAME} 318 {client.nick} {nick} :End of /WHOIS list")
        else:
            await client.send(f":{SERVER_NAME} 401 {client.nick} {target} :No such nick/channel")
            await client.send(f":{SERVER_NAME} 318 {client.nick} {target} :End of /WHOIS list")

    async def _handle_who(self, client, params):
        parts = params.split(" ")
        target = parts[0] if parts else "*"

        if target in client.channels:
            await client.send(f":{SERVER_NAME} 352 {client.nick} {target} {client.user} mesh mesh {client.nick} H :0 {client.nick}")
        elif target in ("*", "0"):
            for chan in sorted(client.channels):
                await client.send(f":{SERVER_NAME} 352 {client.nick} {chan} {client.user} mesh mesh {client.nick} H :0 {client.nick}")
        elif target == client.nick:
            for chan in sorted(client.channels):
                await client.send(f":{SERVER_NAME} 352 {client.nick} {chan} {client.user} mesh mesh {client.nick} H :0 {client.nick}")

        await client.send(f":{SERVER_NAME} 315 {client.nick} {target} :End of /WHO list")

    async def _handle_ping(self, client, params):
        await client.send(f":{SERVER_NAME} PONG {SERVER_NAME} :{params}")


# ------------------------------------------------------------------ #
# IRC Client                                                           #
# ------------------------------------------------------------------ #

class Client:
    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        self.nick = "*"
        self.user = None
        self.password = None
        self.channels = set()

    @property
    def registered(self) -> bool:
        return self.user is not None

    async def send(self, msg: str):
        logger.debug(f" > {msg}")
        self.writer.write(f"{msg}\r\n".encode())
        await self.writer.drain()

    async def close(self):
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except Exception:
            pass

    @property
    def prefix(self) -> str:
        return f"{self.nick}!{self.user or self.nick}@mesh"


# ------------------------------------------------------------------ #
# Config + entry point                                                 #
# ------------------------------------------------------------------ #

def _load_config(path: str | None) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    if path and os.path.exists(path):
        cfg.read(path)
    return cfg


async def main():
    parser = argparse.ArgumentParser(description="MeshCore IRC bridge")

    parser.add_argument("--config", help="Path to INI config file")

    group = parser.add_mutually_exclusive_group()
    group.add_argument("--serial", help="Serial port (e.g. /dev/ttyACM0)")
    group.add_argument("--ble", help="BLE device address")
    group.add_argument("--tcp", help="TCP address as host:port")

    parser.add_argument("--host", help="IRC server bind address")
    parser.add_argument("--port", type=int, help="IRC server port")
    parser.add_argument("--password", help="IRC server password")
    parser.add_argument("--voice-timeout", type=int, help="Voice status timeout (seconds)")
    parser.add_argument("--max-msg-len", type=int, help="Max outgoing message length (bytes)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug output")

    args = parser.parse_args()
    cfg = _load_config(args.config)

    def get(section, key, default):
        return cfg.get(section, key, fallback=default) if cfg.has_section(section) else default

    serial = args.serial or get("meshcore", "serial", None)
    ble = args.ble or get("meshcore", "ble", None)
    tcp = args.tcp or get("meshcore", "tcp", None)

    if not any([serial, ble, tcp]):
        parser.error("Specify a device: --serial, --ble, --tcp, or set one in the config file")

    irc_host = args.host or get("irc", "host", "127.0.0.1")
    irc_port = args.port or int(get("irc", "port", "6667"))
    password = args.password or get("irc", "password", None) or None
    voice_timeout = args.voice_timeout or int(get("irc", "voice_timeout", "600"))
    max_msg_len = args.max_msg_len or int(get("meshcore", "max_msg_len", "200"))
    baudrate = int(get("meshcore", "baudrate", "115200"))
    verbose = args.verbose or cfg.getboolean("log", "debug", fallback=False)

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
    )

    if serial:
        mc = await MeshCore.create_serial(serial, baudrate=baudrate)
    elif ble:
        mc = await MeshCore.create_ble(ble)
    else:
        host_part, _, port_part = tcp.partition(":")
        mc = await MeshCore.create_tcp(host_part, int(port_part))

    bridge = Bridge(mc, password=password, voice_timeout=voice_timeout, max_msg_len=max_msg_len)

    try:
        await bridge.start(irc_host, irc_port)
    finally:
        await mc.stop_auto_message_fetching()
        mc.stop()
        await mc.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
