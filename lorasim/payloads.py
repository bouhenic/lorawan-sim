"""Générateurs de charge utile applicative pour les devices simulés.

Capteur (FPort 2), relais (FPort 3) et afficheur (FPort 4). Les actionneurs reçoivent leurs
commandes en downlink (on_downlink) et préviennent le device d'un changement d'état par
notify(message) pour qu'il le confirme aussitôt en uplink.
"""

import asyncio
import math
import random
import textwrap
import time

# Codec JavaScript installé dans le device profile ChirpStack (tous les types de payload)
CODEC_JS = """\
// Codec du simulateur LoRaWAN (lorawan-sim) : capteur, relais, afficheur
//
// Uplinks
//   FPort 2 capteur   : temp int16 (x10, °C) | humidité uint8 (%) | batterie uint16 (mV) | compteur uint8
//   FPort 3 relais    : état uint8 (0/1) | commutations uint16 | origine uint8
//   FPort 4 afficheur : messages reçus uint16 | octets du dernier message uint8
// Downlinks (indiquer le FPort correspondant lors de l'envoi)
//   FPort 1 : {"interval": 60}          intervalle d'émission en secondes (tous les devices)
//   FPort 3 : {"relay": 1}              0 = OFF, 1 = ON, "toggle" = basculer
//             {"relay": 1, "duration": 30}  puis retour à l'état précédent après 30 s
//   FPort 4 : {"text": "Salle B12\\n21 °C"}   texte UTF-8, 51 octets max (SF12), "" efface

var SOURCES = ["démarrage", "commande réseau", "bouton local", "fin de temporisation"];

function decodeUplink(input) {
  var b = input.bytes;
  if (input.fPort === 2 && b.length >= 6) {
    var t = (b[0] << 8) | b[1];
    if (t & 0x8000) t -= 0x10000;
    return {
      data: {
        temperature: t / 10,
        humidity: b[2],
        battery_mv: (b[3] << 8) | b[4],
        counter: b[5]
      }
    };
  }
  if (input.fPort === 3 && b.length >= 4) {
    return {
      data: {
        relay: b[0],
        switches: (b[1] << 8) | b[2],
        source: SOURCES[b[3]] || b[3]
      }
    };
  }
  if (input.fPort === 4 && b.length >= 3) {
    return { data: { messages: (b[0] << 8) | b[1], last_length: b[2] } };
  }
  return { data: { raw: b } };
}

function utf8(s) {
  var out = [];
  for (var i = 0; i < s.length; i++) {
    var c = s.charCodeAt(i);
    if (c >= 0xd800 && c < 0xdc00 && i + 1 < s.length) {
      c = 0x10000 + ((c - 0xd800) << 10) + (s.charCodeAt(++i) - 0xdc00);
    }
    if (c < 0x80) out.push(c);
    else if (c < 0x800) out.push(0xc0 | (c >> 6), 0x80 | (c & 63));
    else if (c < 0x10000) out.push(0xe0 | (c >> 12), 0x80 | ((c >> 6) & 63), 0x80 | (c & 63));
    else out.push(0xf0 | (c >> 18), 0x80 | ((c >> 12) & 63), 0x80 | ((c >> 6) & 63), 0x80 | (c & 63));
  }
  return out;
}

function encodeDownlink(input) {
  var d = input.data;
  if (d.interval !== undefined) {
    return { fPort: 1, bytes: [(d.interval >> 8) & 0xff, d.interval & 0xff] };
  }
  if (d.relay !== undefined) {
    var cmd = d.relay === "toggle" ? 2 : (d.relay === 1 || d.relay === true || d.relay === "on") ? 1 : 0;
    var bytes = [cmd];
    if (d.duration) bytes.push((d.duration >> 8) & 0xff, d.duration & 0xff);
    return { fPort: 3, bytes: bytes };
  }
  if (d.text !== undefined) {
    var text = utf8(String(d.text));
    if (text.length > 51) {
      return { errors: ["texte trop long : " + text.length + " octets, 51 max (downlink à SF12)"] };
    }
    return { fPort: 4, bytes: text };
  }
  return { errors: ["champ attendu : interval, relay ou text"] };
}
"""


class SensorPayload:
    """Simule un capteur température/humidité avec variation journalière."""

    fport = 2

    def __init__(self, seed: str):
        rnd = random.Random(seed)
        self.base_temp = rnd.uniform(15, 25)
        self.base_hum = rnd.uniform(40, 70)
        self.battery_mv = rnd.randint(3300, 3600)
        self.counter = 0
        self.last = {}

    def next(self) -> bytes:
        phase = (time.time() % 86400) / 86400 * 2 * math.pi
        temp = self.base_temp + 4 * math.sin(phase) + random.gauss(0, 0.2)
        hum = self.base_hum - 10 * math.sin(phase) + random.gauss(0, 1)
        if random.random() < 0.05:
            self.battery_mv = max(2800, self.battery_mv - 1)
        self.counter = (self.counter + 1) & 0xFF
        t = int(round(temp * 10)) & 0xFFFF
        h = max(0, min(100, int(round(hum))))
        self.last = {"temperature": round(temp, 1), "humidity": h, "battery_mv": self.battery_mv}
        return t.to_bytes(2, "big") + bytes([h]) + self.battery_mv.to_bytes(2, "big") + bytes([self.counter])


class RelayPayload:
    """Relais (prise, vanne…) commandé par downlink FPort 3 ; état renvoyé en uplink FPort 3."""

    fport = 3
    external_power = True
    SOURCES = ("démarrage", "commande réseau", "bouton local", "fin de temporisation")

    def __init__(self):
        self.state = 0
        self.switches = 0
        self.source = 0
        self.pulse_end = None
        self.notify = None
        self._timer = None
        self._update()

    def _update(self):
        self.last = {"relay": self.state, "switches": self.switches,
                     "source": self.SOURCES[self.source], "pulse_end": self.pulse_end}

    def next(self) -> bytes:
        return bytes([self.state]) + self.switches.to_bytes(2, "big") + bytes([self.source])

    def _set(self, state: int, source: int) -> str:
        if state != self.state:
            self.state = state
            self.switches = (self.switches + 1) & 0xFFFF
        self.source = source
        self._update()
        return f"relais {'ON' if state else 'OFF'} ({self.SOURCES[source]})"

    def _cancel_timer(self):
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self.pulse_end = None

    def on_downlink(self, fport: int, data: bytes) -> str | None:
        """1 octet (0 OFF, 1 ON, 2 basculer) + durée optionnelle uint16 (s)."""
        if fport != self.fport or len(data) not in (1, 3) or data[0] > 2:
            return None
        self._cancel_timer()
        before = self.state
        msg = self._set(1 - self.state if data[0] == 2 else data[0], 1)
        duration = int.from_bytes(data[1:], "big") if len(data) == 3 else 0
        if duration:
            self.pulse_end = time.time() + duration
            self._timer = asyncio.get_running_loop().call_later(duration, self._timer_done, before)
            self._update()
            msg += f", retour à {'ON' if before else 'OFF'} dans {duration} s"
        return msg

    def _timer_done(self, state: int):
        self._timer = None
        self.pulse_end = None
        msg = self._set(state, 3)
        if self.notify:
            self.notify(msg)

    def press(self) -> str:
        """Appui sur le bouton physique du relais : bascule."""
        self._cancel_timer()
        return self._set(1 - self.state, 2)


class DisplayPayload:
    """Afficheur OLED 128x64 (type Heltec) : texte reçu en downlink FPort 4, accusé en uplink FPort 4."""

    fport = 4
    external_power = True
    COLS, ROWS = 21, 5
    MAX_BYTES = 51   # downlink le plus long possible à SF12 (RX2 en EU868)

    def __init__(self):
        self.messages = 0
        self.length = 0
        self.lines = ["En attente", "d'un message..."]
        self.notify = None
        self._update()

    def _update(self):
        self.last = {"lines": self.lines, "messages": self.messages}

    def next(self) -> bytes:
        return self.messages.to_bytes(2, "big") + bytes([min(self.length, 255)])

    def on_downlink(self, fport: int, data: bytes) -> str | None:
        if fport != self.fport:
            return None
        text = data.decode("utf-8", errors="replace")
        lines = []
        for raw in text.splitlines():
            lines += textwrap.wrap(raw, self.COLS) or [""]
        self.lines = lines[:self.ROWS]
        self.messages = (self.messages + 1) & 0xFFFF
        self.length = len(data)
        self._update()
        if not data:
            return "afficheur effacé"
        shown = " | ".join(self.lines)
        cut = " (tronqué)" if len(lines) > self.ROWS else ""
        return f"afficheur : « {shown} »{cut}"


class HexPayload:
    def __init__(self, hex_str: str, fport: int = 1):
        self.data = bytes.fromhex(hex_str)
        self.fport = fport
        self.last = {"hex": hex_str}

    def next(self) -> bytes:
        return self.data


class TextPayload:
    def __init__(self, text: str, fport: int = 1):
        self.text = text
        self.fport = fport
        self.n = 0
        self.last = {}

    def next(self) -> bytes:
        self.n += 1
        text = self.text.format(n=self.n)[:51]
        self.last = {"text": text}
        return text.encode()


def make(cfg: dict, seed: str):
    kind = cfg.get("payload", "sensor")
    if kind == "sensor":
        return SensorPayload(seed)
    if kind == "relay":
        return RelayPayload()
    if kind == "display":
        return DisplayPayload()
    if kind == "hex":
        return HexPayload(cfg.get("payload_hex", "00"), cfg.get("fport", 1))
    if kind == "text":
        return TextPayload(cfg.get("payload_text", "hello {n}"), cfg.get("fport", 1))
    raise ValueError(f"type de payload inconnu : {kind}")


# --- commandes envoyées aux actionneurs (via la file de downlinks ChirpStack) ----

def relay_command(action: str, duration: int = 0) -> tuple[int, bytes]:
    cmd = {"off": 0, "on": 1, "toggle": 2}.get(action)
    if cmd is None:
        raise ValueError("relais : on, off ou toggle")
    if not 0 <= duration <= 0xFFFF:
        raise ValueError("durée : 0 à 65535 s")
    return RelayPayload.fport, bytes([cmd]) + (duration.to_bytes(2, "big") if duration else b"")


def display_command(text: str) -> tuple[int, bytes]:
    data = text.encode()
    if len(data) > DisplayPayload.MAX_BYTES:
        raise ValueError(f"texte trop long : {len(data)} octets, {DisplayPayload.MAX_BYTES} max "
                         "(taille maximale d'un downlink à SF12)")
    return DisplayPayload.fport, data
