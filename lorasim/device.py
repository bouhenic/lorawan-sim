"""Device LoRaWAN 1.0.x simulé (OTAA ou ABP, classe A ou C)."""

import asyncio
import collections
import random
import time

from . import mac, payloads, phy
from .crypto import derive_session_keys
from .log import log
from .radio import EU868_DATR, EU868_DEFAULT_CHANNELS

JOIN_TIMEOUT = 8      # s : RX2 du Join Accept à 6 s + marge réseau
ACK_TIMEOUT = 4       # s : attente d'un ACK pour un uplink confirmé


class Device:
    def __init__(self, cfg: dict, medium, state):
        self.cfg = cfg
        self.name = cfg["name"]
        self.dev_eui = bytes.fromhex(cfg["dev_eui"])
        self.dev_eui_hex = cfg["dev_eui"].lower()
        self.join_eui = bytes.fromhex(cfg.get("join_eui", "0000000000000000"))
        self.activation = cfg.get("activation", "otaa").lower()
        self.device_class = str(cfg.get("class", "A")).upper()
        self.interval = float(cfg.get("interval", 60))
        self.confirmed = bool(cfg.get("confirmed", False))
        self.medium = medium
        self.state = state
        self.st = state.device(self.dev_eui_hex)
        self.payload = payloads.make(cfg, self.dev_eui_hex)
        self.payload.notify = self.report  # actionneur : changement d'état à confirmer

        self.data_rate = int(cfg.get("data_rate", 5))
        self.tx_power = 0  # index TXPower (0 = puissance max)
        self.channels = dict(EU868_DEFAULT_CHANNELS)
        self.last_snr = 0.0
        self.pending_mac = b""
        self.pending_ack = False
        self.joined = asyncio.Event()
        self.wake = asyncio.Event()
        self.paused = False
        self._resume = asyncio.Event()
        self._join_future = None
        self._ack_future = None
        self.task = None

        # Statistiques pour le tableau de bord
        self.status = "démarrage"
        self.uplinks = 0
        self.downlinks = 0
        self.last_up = None
        self.last_down = None
        self.last_down_info = ""
        self.last_reception = None   # (rssi, snr, nb passerelles) du dernier uplink
        self.rssi_history = collections.deque(maxlen=40)

        self.app_key = bytes.fromhex(cfg["app_key"]) if self.activation == "otaa" else None
        if self.activation == "abp":
            self.dev_addr = int(cfg["dev_addr"], 16)
            self.nwk_s_key = bytes.fromhex(cfg["nwk_s_key"])
            self.app_s_key = bytes.fromhex(cfg["app_s_key"])
            self.joined.set()
        else:
            self.dev_addr = None
            self.nwk_s_key = self.app_s_key = None

    # --- utilitaires -------------------------------------------------------

    def log(self, msg: str, level: str = "info"):
        log(self.name, msg, level, kind="dev")

    def battery_level(self) -> int:
        if getattr(self.payload, "external_power", False):
            return 0  # DevStatusAns : alimentation externe
        mv = getattr(self.payload, "battery_mv", 3300)
        return max(1, min(254, int((mv - 2800) / 800 * 253) + 1))

    def set_channel(self, index: int, freq: int):
        if freq == 0:
            self.channels.pop(index, None)
        elif index >= 3:  # les 3 canaux par défaut ne sont pas modifiables
            self.channels[index] = freq

    def _radio_params(self) -> tuple[int, str]:
        freq = random.choice(list(self.channels.values()))
        return freq, EU868_DATR.get(self.data_rate, "SF7BW125")

    def _transmit(self, frame: bytes) -> int:
        freq, datr = self._radio_params()
        receptions = self.medium.uplink(self, frame, freq, datr)
        if receptions:
            rssi, snr = max((r, s) for _, r, s in receptions)
            self.last_reception = (rssi, snr, len(receptions))
            self.rssi_history.append(rssi)
        else:
            self.last_reception = None
            self.rssi_history.append(None)
            self.log(f"aucune passerelle n'a reçu la trame ({freq / 1e6:.1f} MHz {datr})", "warn")
        return len(receptions)

    # --- commandes (tableau de bord) ----------------------------------------

    def start(self):
        self.task = asyncio.create_task(self.run())

    def stop(self):
        if self.task is not None:
            self.task.cancel()

    def trigger_uplink(self):
        self.wake.set()

    def rejoin(self):
        if self.activation != "otaa":
            return
        self.log("rejoin demandé")
        self.joined.clear()
        self.dev_addr = None
        self.wake.set()

    def set_paused(self, paused: bool):
        self.paused = paused
        if paused:
            self._resume.clear()
            self.log("éteint")
        else:
            self._resume.set()
            self.log("allumé")

    def report(self, msg: str):
        """L'état de l'actionneur a changé : on l'annonce par un uplink immédiat."""
        self.log(msg, "ok")
        if self.dev_addr is not None and not self.paused:
            self.wake.set()

    def press_button(self) -> bool:
        if not hasattr(self.payload, "press"):
            return False
        self.report(self.payload.press())
        return True

    def set_interval(self, seconds: float):
        self.interval = max(5.0, float(seconds))
        self.log(f"intervalle d'émission changé à {self.interval:g} s", "ok")
        self.wake.set()

    def snapshot(self) -> dict:
        datr = EU868_DATR.get(self.data_rate, "?")
        return {
            "name": self.name, "dev_eui": self.dev_eui_hex, "activation": self.activation,
            "payload": self.cfg.get("payload", "sensor"),
            "class": self.device_class, "status": "pause" if self.paused else self.status,
            "dev_addr": f"{self.dev_addr:08x}" if self.dev_addr is not None else None,
            "fcnt_up": self.st["fcnt_up"], "fcnt_down": max(self.st["fcnt_down"], 0),
            "dr": self.data_rate, "sf": datr[:datr.index("B")] if "B" in datr else datr,
            "tx_power": self.tx_power, "channels": len(self.channels),
            "interval": self.interval, "confirmed": self.confirmed,
            "uplinks": self.uplinks, "downlinks": self.downlinks,
            "last_up": self.last_up, "last_down": self.last_down, "last_down_info": self.last_down_info,
            "rssi": self.last_reception[0] if self.last_reception else None,
            "snr": self.last_reception[1] if self.last_reception else None,
            "gateways": self.last_reception[2] if self.last_reception else 0,
            "rssi_history": list(self.rssi_history),
            "values": getattr(self.payload, "last", {}),
            "credentials": self.credentials(),
        }

    def credentials(self) -> list[tuple[str, str]]:
        """Identifiants à recopier dans ChirpStack pour enregistrer le device."""
        creds = [("DevEUI", self.dev_eui_hex)]
        if self.activation == "otaa":
            creds += [("JoinEUI (AppEUI)", self.join_eui.hex()), ("AppKey", self.app_key.hex())]
            if self.nwk_s_key is not None:  # clés de session dérivées au join
                creds += [("NwkSKey (session)", self.nwk_s_key.hex()),
                          ("AppSKey (session)", self.app_s_key.hex())]
        else:
            creds += [("DevAddr", f"{self.dev_addr:08x}"), ("NwkSKey", self.nwk_s_key.hex()),
                      ("AppSKey", self.app_s_key.hex())]
        return creds

    # --- cycle de vie ------------------------------------------------------

    async def run(self):
        await asyncio.sleep(random.uniform(0.5, 5))  # étale les démarrages
        if self.activation == "abp":
            self.log(f"ABP classe {self.device_class}, DevAddr {self.dev_addr:08x}, FCnt {self.st['fcnt_up']}")
        while True:
            if self.paused:
                await self._resume.wait()
                continue
            if not self.joined.is_set():
                await self.join()
                continue
            self.status = f"actif (classe {self.device_class})"
            self.wake.clear()
            self.send_uplink()
            if self._ack_future is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(self._ack_future), ACK_TIMEOUT)
                except asyncio.TimeoutError:
                    self.log("pas d'ACK reçu pour l'uplink confirmé", "warn")
                self._ack_future = None
            jitter = random.uniform(0.9, 1.1)
            try:
                await asyncio.wait_for(self.wake.wait(), self.interval * jitter)
            except asyncio.TimeoutError:
                pass

    async def join(self):
        attempt = 0
        while not self.paused:
            attempt += 1
            self.status = "join en cours"
            dev_nonce = self.state.next_dev_nonce(self.dev_eui_hex)
            frame = phy.build_join_request(self.app_key, self.join_eui, self.dev_eui, dev_nonce)
            self._join_future = asyncio.get_running_loop().create_future()
            self.log(f"Join Request (DevNonce {dev_nonce}, essai {attempt})")
            self._transmit(frame)
            try:
                await asyncio.wait_for(self._join_future, JOIN_TIMEOUT)
                return
            except asyncio.TimeoutError:
                delay = min(10 * attempt, 60)
                self.status = "échec du join"
                self.log(f"pas de Join Accept, nouvel essai dans {delay} s "
                         "(device enregistré ? AppKey correcte ?)", "warn")
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), delay)
                except asyncio.TimeoutError:
                    pass

    def send_uplink(self):
        if self.dev_addr is None:
            return
        fcnt = self.st["fcnt_up"]
        data = self.payload.next()
        fopts, fport, body = b"", self.payload.fport, data
        if self.pending_mac:
            if len(self.pending_mac) <= 15:
                fopts = self.pending_mac
            else:  # trop long pour FOpts : envoi seul sur FPort 0
                fport, body = 0, self.pending_mac
        frame = phy.build_data_up(
            self.nwk_s_key, self.app_s_key, self.dev_addr, fcnt, fport, body,
            confirmed=self.confirmed, ack=self.pending_ack, fopts=fopts,
        )
        ack = self.pending_ack
        self.pending_mac = b""
        self.pending_ack = False
        self.st["fcnt_up"] = fcnt + 1
        self.state.save()
        self.uplinks += 1
        self.last_up = time.time()
        if self.confirmed:
            self._ack_future = asyncio.get_running_loop().create_future()
        extra = (" +ACK" if ack else "") + (f" +MAC {fopts.hex()}" if fopts else "")
        kind = "confirmé" if self.confirmed else "non confirmé"
        self.log(f"Uplink {kind} FCnt={fcnt} FPort={fport} {body.hex()}{extra}")
        self._transmit(frame)

    # --- réception ---------------------------------------------------------

    def on_downlink(self, frame: bytes, freq: int, datr: str, gateway):
        mtype = phy.mtype_of(frame)
        if mtype == phy.JOIN_ACCEPT:
            if self._join_future and not self._join_future.done():
                self._handle_join_accept(frame)
        elif mtype in (phy.UNCONFIRMED_DOWN, phy.CONFIRMED_DOWN):
            if self.dev_addr is not None and phy.peek_dev_addr(frame) == self.dev_addr:
                self._handle_data_down(frame, gateway)

    def _handle_join_accept(self, frame: bytes):
        ja = phy.parse_join_accept(self.app_key, frame)
        if ja is None:
            return  # Join Accept destiné à un autre device
        dev_nonce = self.st["dev_nonce"].to_bytes(2, "little")
        self.nwk_s_key, self.app_s_key = derive_session_keys(self.app_key, ja.app_nonce, ja.net_id, dev_nonce)
        self.dev_addr = ja.dev_addr
        self.data_rate = int(self.cfg.get("data_rate", 5))
        self.tx_power = 0
        self.channels = dict(EU868_DEFAULT_CHANNELS)
        self.pending_mac = b""
        self.pending_ack = False
        if len(ja.cf_list) == 16 and ja.cf_list[15] == 0:  # CFList EU868 : 5 fréquences
            for i in range(5):
                f = int.from_bytes(ja.cf_list[i * 3:i * 3 + 3], "little") * 100
                if f:
                    self.channels[3 + i] = f
        self.st["fcnt_up"] = 0
        self.st["fcnt_down"] = -1
        self.state.save()
        self.joined.set()
        net_id = ja.net_id[::-1].hex()
        self.log(f"JOIN OK : DevAddr {self.dev_addr:08x}, NetID {net_id}, "
                 f"{len(self.channels)} canaux, classe {self.device_class}", "ok")
        self._join_future.set_result(True)

    def _handle_data_down(self, frame: bytes, gateway):
        dd = phy.parse_data_down(self.nwk_s_key, self.app_s_key, frame, self.st["fcnt_down"])
        if dd is None:
            self.log("downlink reçu avec MIC invalide, ignoré", "warn")
            return
        if dd.fcnt <= self.st["fcnt_down"]:
            return  # doublon (plusieurs passerelles ou retransmission)
        self.st["fcnt_down"] = dd.fcnt
        self.state.save()
        self.last_snr = self.medium.link_budget(self, gateway)[1]  # SNR mesuré par le device
        self.downlinks += 1
        self.last_down = time.time()

        parts = [f"Downlink FCnt={dd.fcnt} via {gateway.name}"]
        if dd.ack:
            parts.append("ACK")
            if self._ack_future and not self._ack_future.done():
                self._ack_future.set_result(True)
        if dd.confirmed:
            parts.append("confirmé")
            self.pending_ack = True
        cmds = mac.parse(dd.mac_commands)
        if cmds:
            parts.append("MAC[" + ", ".join(f"{mac.name(c)}({p.hex()})" if p else mac.name(c)
                                            for c, p in cmds) + "]")
            self.pending_mac += mac.handle(self, cmds)
        app = dd.fport not in (None, 0)
        if app:
            parts.append(f"FPort={dd.fport} data={dd.payload.hex()}")
            self.last_down_info = f"FPort {dd.fport} : {dd.payload.hex() or '(vide)'}"
        self.log(" ".join(parts), "down")
        if app:
            self._handle_app_downlink(dd.fport, dd.payload)
        # Le serveur attend une réponse (ACK ou trames en attente) : uplink anticipé
        if dd.fpending or dd.confirmed:
            self.wake.set()

    def _handle_app_downlink(self, fport: int, data: bytes):
        """FPort 1, 2 octets : nouvel intervalle d'émission (secondes) ; sinon commande d'actionneur."""
        if fport == 1 and len(data) == 2:
            new = int.from_bytes(data, "big")
            if new >= 5:
                self.set_interval(new)
            return
        handler = getattr(self.payload, "on_downlink", None)
        if handler is None:
            return
        msg = handler(fport, data)
        if msg:
            self.report(msg)
        else:
            self.log(f"FPort {fport} : commande non reconnue ({data.hex() or 'vide'})", "warn")
