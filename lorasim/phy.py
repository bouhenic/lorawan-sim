"""Construction et décodage des trames LoRaWAN 1.0.x (PHYPayload)."""

from dataclasses import dataclass, field

from . import crypto

# Types de message (MType, 3 bits de poids fort du MHDR)
JOIN_REQUEST = 0
JOIN_ACCEPT = 1
UNCONFIRMED_UP = 2
UNCONFIRMED_DOWN = 3
CONFIRMED_UP = 4
CONFIRMED_DOWN = 5

MTYPE_NAMES = {
    0: "JoinRequest", 1: "JoinAccept", 2: "UnconfirmedUp",
    3: "UnconfirmedDown", 4: "ConfirmedUp", 5: "ConfirmedDown",
}


def mtype_of(phy: bytes) -> int:
    return phy[0] >> 5


def build_join_request(app_key: bytes, join_eui: bytes, dev_eui: bytes, dev_nonce: int) -> bytes:
    """join_eui / dev_eui en big-endian (format lisible), transmis inversés."""
    msg = (
        bytes([JOIN_REQUEST << 5])
        + join_eui[::-1]
        + dev_eui[::-1]
        + dev_nonce.to_bytes(2, "little")
    )
    return msg + crypto.mic_join_request(app_key, msg)


@dataclass
class JoinAccept:
    app_nonce: bytes  # 3 octets, ordre sur l'air
    net_id: bytes     # 3 octets, ordre sur l'air
    dev_addr: int
    dl_settings: int
    rx_delay: int
    cf_list: bytes


def parse_join_accept(app_key: bytes, phy: bytes) -> JoinAccept | None:
    """Déchiffre un Join Accept ; renvoie None si le MIC est invalide."""
    mhdr, enc = phy[:1], phy[1:]
    if len(enc) not in (16, 32):
        return None
    # Le serveur "déchiffre" avec AES : le device chiffre pour retrouver le clair
    clear = crypto.aes_encrypt(app_key, enc)
    body, mic = clear[:-4], clear[-4:]
    if crypto.aes_cmac(app_key, mhdr + body)[:4] != mic:
        return None
    return JoinAccept(
        app_nonce=body[0:3],
        net_id=body[3:6],
        dev_addr=int.from_bytes(body[6:10], "little"),
        dl_settings=body[10],
        rx_delay=body[11],
        cf_list=body[12:],
    )


def build_data_up(
    nwk_s_key: bytes, app_s_key: bytes, dev_addr: int, fcnt: int,
    fport: int | None, payload: bytes, confirmed: bool = False,
    ack: bool = False, adr: bool = True, fopts: bytes = b"",
) -> bytes:
    mtype = CONFIRMED_UP if confirmed else UNCONFIRMED_UP
    fctrl = (0x80 if adr else 0) | (0x20 if ack else 0) | (len(fopts) & 0x0F)
    fhdr = dev_addr.to_bytes(4, "little") + bytes([fctrl]) + (fcnt & 0xFFFF).to_bytes(2, "little") + fopts
    msg = bytes([mtype << 5]) + fhdr
    if fport is not None:
        key = nwk_s_key if fport == 0 else app_s_key
        msg += bytes([fport]) + crypto.crypt_frm_payload(key, payload, dev_addr, fcnt, uplink=True)
    return msg + crypto.mic_data(nwk_s_key, msg, dev_addr, fcnt, uplink=True)


@dataclass
class DataDown:
    mtype: int
    dev_addr: int
    fcnt: int
    adr: bool
    ack: bool
    fpending: bool
    fopts: bytes
    fport: int | None
    payload: bytes
    mac_commands: bytes = field(default=b"")

    @property
    def confirmed(self) -> bool:
        return self.mtype == CONFIRMED_DOWN


def peek_dev_addr(phy: bytes) -> int | None:
    if len(phy) < 12 or mtype_of(phy) not in (UNCONFIRMED_DOWN, CONFIRMED_DOWN):
        return None
    return int.from_bytes(phy[1:5], "little")


def parse_data_down(nwk_s_key: bytes, app_s_key: bytes, phy: bytes, last_fcnt_down: int) -> DataDown | None:
    """Vérifie le MIC et déchiffre une trame descendante. None si invalide."""
    msg, mic = phy[:-4], phy[-4:]
    dev_addr = int.from_bytes(msg[1:5], "little")
    fctrl = msg[5]
    fcnt16 = int.from_bytes(msg[6:8], "little")
    # Reconstitue le compteur 32 bits à partir des 16 bits transmis
    base = max(last_fcnt_down, 0)
    fcnt = (base & 0xFFFF0000) | fcnt16
    if fcnt < base:
        fcnt += 0x10000
    if crypto.mic_data(nwk_s_key, msg, dev_addr, fcnt, uplink=False) != mic:
        return None
    fopts_len = fctrl & 0x0F
    fopts = msg[8:8 + fopts_len]
    rest = msg[8 + fopts_len:]
    fport, payload = None, b""
    if rest:
        fport = rest[0]
        key = nwk_s_key if fport == 0 else app_s_key
        payload = crypto.crypt_frm_payload(key, rest[1:], dev_addr, fcnt, uplink=False)
    return DataDown(
        mtype=mtype_of(phy), dev_addr=dev_addr, fcnt=fcnt,
        adr=bool(fctrl & 0x80), ack=bool(fctrl & 0x20), fpending=bool(fctrl & 0x10),
        fopts=fopts, fport=fport, payload=payload,
        mac_commands=payload if fport == 0 else fopts,
    )
