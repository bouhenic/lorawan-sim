"""Canal radio simulé entre devices et passerelles (EU868)."""

import hashlib
import random

# Data rates EU868 (DR -> datr Semtech) et sensibilité approximative (dBm)
EU868_DATR = {
    0: "SF12BW125", 1: "SF11BW125", 2: "SF10BW125",
    3: "SF9BW125", 4: "SF8BW125", 5: "SF7BW125", 6: "SF7BW250",
}
SENSITIVITY = {
    "SF12": -137, "SF11": -134.5, "SF10": -132,
    "SF9": -129, "SF8": -126, "SF7": -123,
}
EU868_DEFAULT_CHANNELS = {0: 868_100_000, 1: 868_300_000, 2: 868_500_000}


def stable_random(*parts: str) -> random.Random:
    """Générateur pseudo-aléatoire reproductible pour un couple device/passerelle."""
    h = hashlib.sha256("|".join(parts).encode()).digest()
    return random.Random(int.from_bytes(h[:8], "big"))


class RadioMedium:
    """Relie les devices aux passerelles : chaque uplink est « entendu » par
    toutes les passerelles à portée, chaque downlink est reçu par tous les
    devices (qui filtrent selon leur DevAddr / état de join)."""

    def __init__(self, loss: float = 0.0):
        self.gateways = []
        self.devices = []
        self.loss = loss

    def link_budget(self, device, gateway) -> tuple[float, float]:
        """RSSI/SNR simulés pour ce couple, avec un peu de fading."""
        conf = device.cfg.get("rssi")
        if isinstance(conf, dict):
            base = conf.get(gateway.name, conf.get(gateway.gateway_id))
        else:
            base = conf
        if base is None:
            base = stable_random(device.dev_eui_hex, gateway.gateway_id).uniform(-110, -60)
        # EU868 : chaque palier TXPower retire 2 dB à la puissance émise
        rssi = base - 2 * device.tx_power + random.gauss(0, 2)
        snr = min(12.0, rssi + 117 + random.gauss(0, 1))
        return round(rssi), round(snr, 1)

    def uplink(self, device, phy: bytes, freq: int, datr: str) -> list:
        """Diffuse un uplink. Renvoie [(passerelle, rssi, snr)] des réceptions."""
        heard = []
        for gw in self.gateways:
            rssi, snr = self.link_budget(device, gw)
            if rssi < SENSITIVITY.get(datr[:4].rstrip("B"), -123):
                continue
            if self.loss and random.random() < self.loss:
                continue
            gw.forward_uplink(phy, freq, datr, rssi, snr)
            heard.append((gw, rssi, snr))
        return heard

    def downlink(self, gateway, phy: bytes, freq: int, datr: str):
        for d in self.devices:
            d.on_downlink(phy, freq, datr, gateway)
