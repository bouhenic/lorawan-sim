"""Tableau de bord web temps réel (aiohttp).

  GET  /                          page HTML
  GET  /api/state                 état des passerelles et devices (JSON)
  GET  /api/events                flux des événements (Server-Sent Events)
  GET  /api/codec                 codec JavaScript (capteur, relais, afficheur)
  POST /api/gateways/{id}/server  {host, port} : change le serveur de la passerelle
  POST /api/devices               crée un device (à enregistrer soi-même dans ChirpStack)
  DELETE /api/devices/{name}      supprime un device
  POST /api/devices/{name}/{action}
       uplink | rejoin | pause | resume | interval {seconds} | downlink {hex, fport, confirmed}
       button (bouton local du relais) | command {relay: on|off|toggle, duration} ou {text}
       mc_loss {percent} (perte simulée des trames multicast, FUOTA)
  POST /api/groups                crée un groupe multicast {name, mc_addr, mc_nwk_s_key, mc_app_s_key, dr, frequency}
  DELETE /api/groups/{name}       supprime un groupe
  POST /api/groups/{name}/members {device, member} : ajoute / retire un device
  POST /api/groups/{name}/command {relay, duration} | {text} | {hex, fport} : downlink multicast
"""

import asyncio
import json
import pathlib
import re
import secrets
import time

from aiohttp import web

from . import events, payloads
from .config import save_config
from .log import log

STATIC = pathlib.Path(__file__).parent / "static"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,49}$")
HOST_RE = re.compile(r"^[A-Za-z0-9.:_-]{1,253}$")


def _hex_field(body: dict, key: str, label: str, size: int, default: str | None = None) -> str:
    v = re.sub(r"[\s:-]", "", str(body.get(key) or "")).lower()
    if not v and default is not None:
        return default
    if len(v) != 2 * size or not re.fullmatch(r"[0-9a-f]+", v):
        raise ValueError(f"{label} : {size} octets attendus, soit {2 * size} caractères hexadécimaux")
    return v


class Dashboard:
    def __init__(self, cfg: dict, medium, state, config_path: str):
        self.cfg = cfg
        self.medium = medium
        self.sim_state = state
        self.config_path = config_path
        self.started = time.time()
        self._chirpstack = None
        app = web.Application()
        app.router.add_get("/", self.index)
        app.router.add_get("/api/state", self.state)
        app.router.add_get("/api/events", self.stream)
        app.router.add_get("/api/codec", self.codec)
        app.router.add_post("/api/gateways/{id}/server", self.gateway_server)
        app.router.add_post("/api/devices", self.create_device)
        app.router.add_delete("/api/devices/{name}", self.delete_device)
        app.router.add_post("/api/devices/{name}/{action}", self.action)
        app.router.add_post("/api/groups", self.create_group)
        app.router.add_delete("/api/groups/{name}", self.delete_group)
        app.router.add_post("/api/groups/{name}/members", self.group_members)
        app.router.add_post("/api/groups/{name}/command", self.group_command)
        self.app = app

    async def start(self, host: str, port: int):
        runner = web.AppRunner(self.app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, host, port).start()
        shown = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
        log("web", f"tableau de bord : http://{shown}:{port}", "ok")

    # --- pages ----------------------------------------------------------------

    async def index(self, request):
        return web.FileResponse(STATIC / "index.html")

    async def state(self, request):
        server = self.cfg["server"]
        return web.json_response({
            "now": time.time(), "started": self.started,
            "server": {"host": server["host"], "api_port": server.get("api_port", 8080),
                       "api": bool(server.get("api_token"))},
            "gateways": [g.snapshot() for g in self.medium.gateways],
            "devices": [d.snapshot() for d in self.medium.devices],
            "groups": [{**g, "devices": list(g.get("devices", []))} for g in self.medium.groups],
        })

    async def stream(self, request):
        resp = web.StreamResponse(headers={
            "Content-Type": "text/event-stream", "Cache-Control": "no-cache",
        })
        await resp.prepare(request)
        q = events.subscribe()
        try:
            for ev in list(events.HISTORY):
                await resp.write(f"data: {json.dumps(ev)}\n\n".encode())
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), 15)
                    await resp.write(f"data: {json.dumps(ev)}\n\n".encode())
                except asyncio.TimeoutError:
                    await resp.write(b": ping\n\n")
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            events.unsubscribe(q)
        return resp

    async def codec(self, request):
        return web.Response(text=payloads.CODEC_JS, content_type="text/plain")

    def _save(self):
        save_config(self.config_path, self.cfg)

    # --- passerelles ------------------------------------------------------------

    async def gateway_server(self, request):
        gw = next((g for g in self.medium.gateways if g.gateway_id == request.match_info["id"]), None)
        if gw is None:
            raise web.HTTPNotFound(text="passerelle inconnue")
        body = await request.json()
        host = str(body.get("host", "")).strip()
        try:
            port = int(body.get("port", 1700))
        except (TypeError, ValueError):
            port = 0
        if not HOST_RE.match(host):
            return web.json_response({"ok": False, "error": "adresse du serveur invalide"}, status=400)
        if not 1 <= port <= 65535:
            return web.json_response({"ok": False, "error": "port UDP invalide (1 à 65535)"}, status=400)
        try:
            await gw.set_server(host, port)
        except OSError as e:
            return web.json_response({"ok": False, "error": f"adresse injoignable : {e}"}, status=400)
        gw.cfg["server_host"], gw.cfg["server_port"] = host, port
        self._save()
        return web.json_response({"ok": True})

    # --- création / suppression de devices ---------------------------------------

    def _device_config(self, body: dict) -> dict:
        name = str(body.get("name", "")).strip()
        if not NAME_RE.match(name):
            raise ValueError("nom invalide : lettres, chiffres, « - _ . », 50 caractères max")
        if any(d["name"] == name for d in self.cfg["devices"]):
            raise ValueError(f"le nom « {name} » est déjà utilisé")
        dev_eui = _hex_field(body, "dev_eui", "DevEUI", 8)
        if any(d["dev_eui"].lower() == dev_eui for d in self.cfg["devices"]):
            raise ValueError(f"le DevEUI {dev_eui} est déjà utilisé")
        activation = str(body.get("activation", "otaa")).lower()
        dcfg = {"name": name, "dev_eui": dev_eui, "activation": activation}
        if activation == "otaa":
            dcfg["join_eui"] = _hex_field(body, "join_eui", "JoinEUI", 8, "0000000000000000")
            dcfg["app_key"] = _hex_field(body, "app_key", "AppKey", 16)
            gen_app_key = _hex_field(body, "gen_app_key", "GenAppKey", 16, "")
            if gen_app_key:  # facultative : sans elle, pas de FUOTA
                dcfg["gen_app_key"] = gen_app_key
        elif activation == "abp":
            dcfg["dev_addr"] = _hex_field(body, "dev_addr", "DevAddr", 4)
            dcfg["nwk_s_key"] = _hex_field(body, "nwk_s_key", "NwkSKey", 16)
            dcfg["app_s_key"] = _hex_field(body, "app_s_key", "AppSKey", 16)
        else:
            raise ValueError("activation : otaa ou abp")
        dev_class = str(body.get("class", "A")).upper()
        if dev_class not in ("A", "C"):
            raise ValueError("classe : A ou C")
        if dev_class == "C":
            dcfg["class"] = "C"
        try:
            dcfg["interval"] = max(5, int(body.get("interval", 30)))
        except (TypeError, ValueError):
            raise ValueError("intervalle invalide")
        kind = body.get("payload", "sensor")
        dcfg["payload"] = kind
        if kind == "text":
            text = str(body.get("payload_text") or "hello {n}")
            try:
                text.format(n=1)
            except (ValueError, KeyError, IndexError):
                raise ValueError("texte invalide : seul {n} (compteur) est autorisé entre accolades")
            dcfg["payload_text"], dcfg["fport"] = text, 10
        elif kind not in ("sensor", "relay", "display"):
            raise ValueError("payload : sensor, text, relay ou display")
        dcfg["confirmed"] = bool(body.get("confirmed", False))
        return dcfg

    async def create_device(self, request):
        from .device import Device
        body = await request.json()
        try:
            dcfg = self._device_config(body)
        except ValueError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        dev = Device(dcfg, self.medium, self.sim_state)
        # Comme un vrai capteur sorti du carton : éteint tant qu'on ne l'allume pas,
        # le temps de l'enregistrer dans ChirpStack
        dev.paused = not body.get("start")
        self.medium.devices.append(dev)
        self.cfg["devices"].append(dcfg)
        self._save()
        dev.start()
        log("web", f"device {dev.name} créé ({dcfg['activation'].upper()}, DevEUI {dcfg['dev_eui']})"
            " : à enregistrer dans ChirpStack", "ok")
        return web.json_response({"ok": True, "name": dev.name})

    async def delete_device(self, request):
        dev = self._device(request.match_info["name"])
        dev.stop()
        self.medium.devices.remove(dev)
        self.cfg["devices"] = [d for d in self.cfg["devices"] if d["name"] != dev.name]
        for g in self.medium.groups:
            if dev.name in g.get("devices", []):
                g["devices"].remove(dev.name)
        self._save()
        log("web", f"device {dev.name} supprimé du simulateur (pensez à le supprimer dans ChirpStack)", "warn")
        return web.json_response({"ok": True})

    # --- actions ----------------------------------------------------------------

    def _device(self, name: str):
        for d in self.medium.devices:
            if d.name == name:
                return d
        raise web.HTTPNotFound(text=f"device inconnu : {name}")

    async def action(self, request):
        dev = self._device(request.match_info["name"])
        action = request.match_info["action"]
        body = await request.json() if request.can_read_body else {}
        if action == "uplink":
            dev.trigger_uplink()
        elif action == "rejoin":
            dev.rejoin()
        elif action in ("pause", "resume"):
            dev.set_paused(action == "pause")
        elif action == "interval":
            dev.set_interval(float(body.get("seconds", 60)))
        elif action == "downlink":
            try:
                data = bytes.fromhex(str(body.get("hex", "")).replace(" ", ""))
                fport = int(body.get("fport", 10))
            except ValueError as e:
                return web.json_response({"ok": False, "error": str(e)}, status=400)
            return await self._downlink(dev, fport, data, bool(body.get("confirmed", False)))
        elif action == "command":
            try:
                if "relay" in body:
                    fport, data = payloads.relay_command(str(body["relay"]), int(body.get("duration") or 0))
                else:
                    fport, data = payloads.display_command(str(body.get("text", "")))
            except ValueError as e:
                return web.json_response({"ok": False, "error": str(e)}, status=400)
            return await self._downlink(dev, fport, data, False)
        elif action == "mc_loss":
            try:
                percent = float(body.get("percent", 0))
            except (TypeError, ValueError):
                return web.json_response({"ok": False, "error": "pourcentage invalide"}, status=400)
            if not 0 <= percent <= 100:
                return web.json_response({"ok": False, "error": "pourcentage : 0 à 100"}, status=400)
            dev.set_mc_loss(percent / 100)
            dcfg = next(d for d in self.cfg["devices"] if d["name"] == dev.name)
            if percent:
                dcfg["mc_loss"] = percent / 100
            else:
                dcfg.pop("mc_loss", None)
            self._save()
        elif action == "button":
            if not dev.press_button():
                return web.json_response({"ok": False, "error": "ce device n'a pas de bouton"}, status=400)
        else:
            raise web.HTTPNotFound(text=f"action inconnue : {action}")
        return web.json_response({"ok": True})

    # --- groupes multicast ----------------------------------------------------------

    def _group(self, name: str) -> dict:
        for g in self.medium.groups:
            if g["name"] == name:
                return g
        raise web.HTTPNotFound(text=f"groupe inconnu : {name}")

    async def create_group(self, request):
        body = await request.json()
        try:
            name = str(body.get("name", "")).strip()
            if not NAME_RE.match(name):
                raise ValueError("nom invalide : lettres, chiffres, « - _ . », 50 caractères max")
            if any(g["name"] == name for g in self.medium.groups):
                raise ValueError(f"le groupe « {name} » existe déjà")
            mc_addr = _hex_field(body, "mc_addr", "McAddr", 4, "e0" + secrets.token_hex(3))
            if any(g["mc_addr"].lower() == mc_addr for g in self.medium.groups):
                raise ValueError(f"le McAddr {mc_addr} est déjà utilisé")
            try:
                dr, freq = int(body.get("dr", 0)), int(body.get("frequency", 869525000))
            except (TypeError, ValueError):
                raise ValueError("data rate ou fréquence invalide")
            if not 0 <= dr <= 5:
                raise ValueError("data rate : 0 (SF12) à 5 (SF7)")
            if not 863_000_000 <= freq <= 870_000_000:
                raise ValueError("fréquence hors de la bande EU868 (863 à 870 MHz, en Hz)")
            group = {
                "name": name, "mc_addr": mc_addr,
                "mc_nwk_s_key": _hex_field(body, "mc_nwk_s_key", "McNwkSKey", 16, secrets.token_hex(16)),
                "mc_app_s_key": _hex_field(body, "mc_app_s_key", "McAppSKey", 16, secrets.token_hex(16)),
                "dr": dr, "frequency": freq, "devices": [],
            }
        except ValueError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        for dev in self.medium.devices:
            dev.forget_multicast(mc_addr)  # McAddr saisi à la main, déjà vu par un ancien groupe
        self.medium.groups.append(group)  # même liste que cfg["multicast_groups"]
        self._save()
        log("web", f"groupe multicast « {name} » créé (McAddr {mc_addr}) : à créer aussi dans ChirpStack", "ok")
        return web.json_response({"ok": True, "name": name})

    async def delete_group(self, request):
        group = self._group(request.match_info["name"])
        self.medium.groups.remove(group)
        for dev in self.medium.devices:
            dev.forget_multicast(group["mc_addr"])
        self._save()
        log("web", f"groupe multicast « {group['name']} » supprimé du simulateur "
            "(pensez à le supprimer dans ChirpStack)", "warn")
        return web.json_response({"ok": True})

    async def group_members(self, request):
        group = self._group(request.match_info["name"])
        body = await request.json()
        dev = self._device(str(body.get("device", "")))
        members = group.setdefault("devices", [])
        if body.get("member") and dev.name not in members:
            members.append(dev.name)
            log("web", f"{dev.name} ajouté au groupe « {group['name']} » (à faire aussi dans ChirpStack)", "ok")
        elif not body.get("member") and dev.name in members:
            members.remove(dev.name)
            log("web", f"{dev.name} retiré du groupe « {group['name']} »", "warn")
        self._save()
        return web.json_response({"ok": True})

    async def group_command(self, request):
        import grpc
        from .provision import ChirpStack, enqueue_multicast
        group = self._group(request.match_info["name"])
        body = await request.json()
        try:
            if "relay" in body:
                fport, data = payloads.relay_command(str(body["relay"]), int(body.get("duration") or 0))
            elif "text" in body:
                fport, data = payloads.display_command(str(body["text"]))
            else:
                fport = int(body.get("fport", 10))
                data = bytes.fromhex(str(body.get("hex", "")).replace(" ", ""))
            if self._chirpstack is None:
                self._chirpstack = ChirpStack(self.cfg["server"])
            fcnt = await asyncio.to_thread(enqueue_multicast, self._chirpstack, group,
                                           self.cfg["server"].get("tenant_id"), fport, data)
        except SystemExit as e:  # clé API absente
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        except ValueError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        except grpc.RpcError as e:
            return web.json_response({"ok": False, "error": f"{e.code().name} : {e.details()}"}, status=502)
        log("web", f"downlink multicast {data.hex() or '(vide)'} FPort {fport} mis en file pour le groupe "
            f"« {group['name']} » (FCnt {fcnt})", "ok")
        return web.json_response({"ok": True, "fcnt": fcnt})

    async def _downlink(self, dev, fport: int, data: bytes, confirmed: bool):
        import grpc
        from .provision import ChirpStack, enqueue
        try:
            if self._chirpstack is None:
                self._chirpstack = ChirpStack(self.cfg["server"])
            qid = await asyncio.to_thread(
                enqueue, self._chirpstack, dev.dev_eui_hex, fport, data, confirmed)
        except SystemExit as e:  # clé API absente
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        except ValueError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        except grpc.RpcError as e:
            return web.json_response({"ok": False, "error": f"{e.code().name} : {e.details()}"}, status=502)
        log("web", f"downlink {data.hex() or '(vide)'} FPort {fport} mis en file pour {dev.name}", "ok")
        return web.json_response({"ok": True, "id": qid})
