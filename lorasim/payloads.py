"""Générateurs de charge utile applicative pour les devices simulés."""

import math
import random
import time

# Décodeur JavaScript installé dans le device profile ChirpStack (type "sensor")
SENSOR_CODEC_JS = """\
// Décodeur du simulateur LoRaWAN (payload "sensor", FPort 2)
// Octets : temp int16 (x10, °C) | humidité uint8 (%) | batterie uint16 (mV) | compteur uint8
function decodeUplink(input) {
  var b = input.bytes;
  if (input.fPort !== 2 || b.length < 6) {
    return { data: { raw: b } };
  }
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

// Downlink FPort 1 : nouvel intervalle d'émission en secondes (uint16)
function encodeDownlink(input) {
  var s = input.data.interval || 60;
  return { fPort: 1, bytes: [(s >> 8) & 0xff, s & 0xff] };
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
    if kind == "hex":
        return HexPayload(cfg.get("payload_hex", "00"), cfg.get("fport", 1))
    if kind == "text":
        return TextPayload(cfg.get("payload_text", "hello {n}"), cfg.get("fport", 1))
    raise ValueError(f"type de payload inconnu : {kind}")
