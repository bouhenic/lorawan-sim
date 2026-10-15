"""Passerelle LoRaWAN simulée : Semtech UDP Packet Forwarder (protocole v2).

Échanges avec le ChirpStack Gateway Bridge (UDP 1700) :
  PUSH_DATA  (0x00) -> uplinks (rxpk) et statistiques (stat)   <- PUSH_ACK (0x01)
  PULL_DATA  (0x02) -> keepalive ouvrant le chemin descendant <- PULL_ACK (0x04)
  PULL_RESP  (0x03) <- downlink à émettre (txpk)              -> TX_ACK   (0x05)
"""

import asyncio
import base64
import datetime as dt
import json
import random
import socket
import time

from . import phy
from .log import log

PROTOCOL_VERSION = 2
PUSH_DATA, PUSH_ACK, PULL_DATA, PULL_RESP, PULL_ACK, TX_ACK = 0, 1, 2, 3, 4, 5
KEEPALIVE_INTERVAL = 10
STAT_INTERVAL = 30


class Gateway(asyncio.DatagramProtocol):
    def __init__(self, cfg: dict, host: str, port: int, medium):
        self.cfg = cfg
        self.name = cfg.get("name", cfg["id"])
        self.gateway_id = cfg["id"].lower()
        self.eui = bytes.fromhex(cfg["id"])
        self.host, self.port = host, port
        self.medium = medium
        self.transport = None
        self.t0 = time.monotonic()
        self.connected = False
        self.last_ack = 0.0
        self.stats = {"rxnb": 0, "rxok": 0, "rxfw": 0, "dwnb": 0, "txnb": 0, "push": 0, "ack": 0}
        self.total_up = 0
        self.total_down = 0
        self.total_rejected = 0

    def snapshot(self) -> dict:
        return {
            "name": self.name, "id": self.gateway_id, "server": f"{self.host}:{self.port}",
            "server_host": self.host, "server_port": self.port,
            "connected": self.connected,
            "last_ack": time.time() - (time.monotonic() - self.last_ack) if self.last_ack else None,
            "uplinks": self.total_up, "downlinks": self.total_down, "rejected": self.total_rejected,
            "lat": self.cfg.get("lat"), "lon": self.cfg.get("lon"),
        }

    def log(self, msg: str, level: str = "info"):
        log(self.name, msg, level, kind="gw")

    # --- horloge de la passerelle (compteur µs 32 bits, comme le SX1301) ----

    def tmst(self) -> int:
        return int((time.monotonic() - self.t0) * 1_000_000) & 0xFFFFFFFF

    # --- démarrage ----------------------------------------------------------

    async def start(self):
        loop = asyncio.get_running_loop()
        await loop.create_datagram_endpoint(lambda: self, remote_addr=(self.host, self.port))
        self.log(f"EUI {self.gateway_id} -> {self.host}:{self.port}/udp")
        asyncio.create_task(self._keepalive_loop())
        asyncio.create_task(self._stat_loop())

    async def set_server(self, host: str, port: int):
        """Change l'adresse du serveur (comme serv_address / serv_port_up dans
        global_conf.json d'un vrai packet forwarder) sans redémarrer la simulation."""
        loop = asyncio.get_running_loop()
        await loop.getaddrinfo(host, port, type=socket.SOCK_DGRAM)  # OSError si nom inconnu
        old = self.transport
        # Le nouveau socket est créé avant de fermer l'ancien : en cas d'échec, rien ne change
        await loop.create_datagram_endpoint(lambda: self, remote_addr=(host, port))
        if old is not None:
            old.close()
        self.host, self.port = host, port
        self.connected = False
        self.last_ack = 0.0
        self.log(f"serveur changé : {host}:{port}/udp", "ok")
        self.transport.sendto(self._header(PULL_DATA) + self.eui)

    def connection_made(self, transport):
        self.transport = transport

    def error_received(self, exc):
        self.log(f"erreur UDP : {exc}", "error")

    def _header(self, ident: int) -> bytes:
        token = random.getrandbits(16).to_bytes(2, "big")
        return bytes([PROTOCOL_VERSION]) + token + bytes([ident])

    def _push(self, obj: dict):
        self.stats["push"] += 1
        self.transport.sendto(self._header(PUSH_DATA) + self.eui + json.dumps(obj).encode())

    async def _keepalive_loop(self):
        while True:
            self.transport.sendto(self._header(PULL_DATA) + self.eui)
            await asyncio.sleep(KEEPALIVE_INTERVAL)
            if self.connected and time.monotonic() - self.last_ack > 3 * KEEPALIVE_INTERVAL:
                self.connected = False
                self.log("plus de réponse du serveur", "warn")

    async def _stat_loop(self):
        await asyncio.sleep(1)
        while True:
            s = self.stats
            ackr = 100.0 * s["ack"] / s["push"] if s["push"] else 0.0
            stat = {
                "time": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                "rxnb": s["rxnb"], "rxok": s["rxok"], "rxfw": s["rxfw"],
                "ackr": round(ackr, 1), "dwnb": s["dwnb"], "txnb": s["txnb"],
            }
            if "lat" in self.cfg and "lon" in self.cfg:
                stat.update(lati=self.cfg["lat"], long=self.cfg["lon"], alti=self.cfg.get("alt", 0))
            self._push({"stat": stat})
            for k in ("rxnb", "rxok", "rxfw", "dwnb", "txnb"):
                s[k] = 0
            await asyncio.sleep(STAT_INTERVAL)

    # --- uplink -------------------------------------------------------------

    def forward_uplink(self, frame: bytes, freq: int, datr: str, rssi: float, snr: float):
        rxpk = {
            "tmst": self.tmst(),
            "time": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
            "chan": 0, "rfch": 0, "freq": round(freq / 1e6, 6),
            "stat": 1, "modu": "LORA", "datr": datr, "codr": "4/5",
            "rssi": int(rssi), "lsnr": snr,
            "size": len(frame), "data": base64.b64encode(frame).decode(),
        }
        self.stats["rxnb"] += 1
        self.stats["rxok"] += 1
        self.stats["rxfw"] += 1
        self.total_up += 1
        self._push({"rxpk": [rxpk]})
        mt = phy.MTYPE_NAMES.get(phy.mtype_of(frame), "?")
        self.log(f"-> {mt} {len(frame)} o, {freq / 1e6:.1f} MHz {datr} RSSI {rssi} SNR {snr}")

    # --- réception des paquets du serveur ----------------------------------

    def datagram_received(self, data: bytes, addr):
        if len(data) < 4 or data[0] != PROTOCOL_VERSION:
            return
        token, ident = data[1:3], data[3]
        if ident in (PUSH_ACK, PULL_ACK):
            self.last_ack = time.monotonic()
            if ident == PUSH_ACK:
                self.stats["ack"] += 1
            if not self.connected:
                self.connected = True
                self.log("connectée au serveur (ACK reçu)", "ok")
        elif ident == PULL_RESP:
            try:
                txpk = json.loads(data[4:])["txpk"]
            except (ValueError, KeyError):
                self.log("PULL_RESP invalide", "error")
                return
            self._handle_txpk(token, txpk)

    def _handle_txpk(self, token: bytes, txpk: dict):
        self.stats["dwnb"] += 1
        frame = base64.b64decode(txpk["data"])
        freq = int(round(float(txpk.get("freq", 0)) * 1e6))
        datr = txpk.get("datr", "")
        delay, error = 0.0, "NONE"  # "imme" (classe C) : émission immédiate
        if not txpk.get("imme") and "tmst" in txpk:
            delta = (int(txpk["tmst"]) - self.tmst()) & 0xFFFFFFFF
            if delta > 0x80000000:          # instant déjà passé
                error = "TOO_LATE"
            elif delta > 10_000_000:        # plus de 10 s : incohérent
                error = "TOO_EARLY"
            else:
                delay = delta / 1e6
        ack = bytes([PROTOCOL_VERSION]) + token + bytes([TX_ACK]) + self.eui
        ack += json.dumps({"txpk_ack": {"error": error}}).encode()
        self.transport.sendto(ack)

        mt = phy.MTYPE_NAMES.get(phy.mtype_of(frame), "?")
        if error != "NONE":
            self.total_rejected += 1
            self.log(f"<- {mt} refusé ({error})", "warn")
            return
        self.stats["txnb"] += 1
        self.total_down += 1
        self.log(f"<- {mt} {len(frame)} o, {freq / 1e6:.3f} MHz {datr}, émission dans {delay:.2f} s", "down")
        asyncio.get_running_loop().call_later(delay, self.medium.downlink, self, frame, freq, datr)
