"""Traitement minimal des commandes MAC LoRaWAN 1.0.x envoyées par le serveur.

Le simulateur répond aux requêtes du serveur pour que ChirpStack considère
le device comme conforme (sinon il renvoie les mêmes commandes en boucle).
"""

# CID -> (nom, longueur de la charge utile côté descendant)
DOWNLINK_COMMANDS = {
    0x02: ("LinkCheckAns", 2),
    0x03: ("LinkADRReq", 4),
    0x04: ("DutyCycleReq", 1),
    0x05: ("RXParamSetupReq", 4),
    0x06: ("DevStatusReq", 0),
    0x07: ("NewChannelReq", 5),
    0x08: ("RXTimingSetupReq", 1),
    0x09: ("TxParamSetupReq", 1),
    0x0A: ("DlChannelReq", 4),
    0x0D: ("DeviceTimeAns", 5),
}


def parse(data: bytes) -> list[tuple[int, bytes]]:
    """Découpe une suite de commandes MAC en (CID, payload)."""
    cmds, i = [], 0
    while i < len(data):
        cid = data[i]
        if cid not in DOWNLINK_COMMANDS:
            break  # CID inconnu : impossible de connaître la longueur, on s'arrête
        n = DOWNLINK_COMMANDS[cid][1]
        cmds.append((cid, data[i + 1:i + 1 + n]))
        i += 1 + n
    return cmds


def name(cid: int) -> str:
    return DOWNLINK_COMMANDS.get(cid, (f"0x{cid:02X}", 0))[0]


def handle(device, cmds: list[tuple[int, bytes]]) -> bytes:
    """Applique les commandes au device et renvoie les réponses à émettre."""
    answers = bytearray()
    for cid, p in cmds:
        if cid == 0x03:  # LinkADRReq : DR / puissance / masque de canaux
            dr, power = p[0] >> 4, p[0] & 0x0F
            if dr != 0x0F:
                device.data_rate = dr
            if power != 0x0F:
                device.tx_power = power
            answers += bytes([0x03, 0x07])  # power, DR et channel mask acceptés
        elif cid == 0x04:
            answers += bytes([0x04])
        elif cid == 0x05:
            answers += bytes([0x05, 0x07])
        elif cid == 0x06:  # DevStatusReq : batterie (1..254) + marge SNR (6 bits signés)
            margin = max(-32, min(31, int(device.last_snr))) & 0x3F
            answers += bytes([0x06, device.battery_level(), margin])
        elif cid == 0x07:  # NewChannelReq : ajout d'un canal
            ch_index = p[0]
            freq = int.from_bytes(p[1:4], "little") * 100
            device.set_channel(ch_index, freq)
            answers += bytes([0x07, 0x03])
        elif cid == 0x08:
            answers += bytes([0x08])
        elif cid == 0x09:
            answers += bytes([0x09])
        elif cid == 0x0A:
            answers += bytes([0x0A, 0x03])
        elif cid == 0x02:
            device.log(f"LinkCheckAns : marge {p[0]} dB, {p[1]} passerelle(s)")
    return bytes(answers)
