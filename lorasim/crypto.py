"""Primitives cryptographiques LoRaWAN 1.0.x (AES-128, AES-CMAC)."""

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.cmac import CMAC


def aes_encrypt(key: bytes, block: bytes) -> bytes:
    """Chiffrement AES-128 ECB d'un ou plusieurs blocs de 16 octets."""
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return enc.update(block) + enc.finalize()


def aes_cmac(key: bytes, data: bytes) -> bytes:
    c = CMAC(algorithms.AES(key))
    c.update(data)
    return c.finalize()


def mic_join_request(app_key: bytes, msg: bytes) -> bytes:
    """MIC d'un Join Request : CMAC(AppKey, MHDR|JoinEUI|DevEUI|DevNonce)[0:4]."""
    return aes_cmac(app_key, msg)[:4]


def mic_data(nwk_s_key: bytes, msg: bytes, dev_addr: int, fcnt: int, uplink: bool) -> bytes:
    """MIC d'une trame de données (bloc B0 + message)."""
    b0 = (
        bytes([0x49, 0, 0, 0, 0, 0 if uplink else 1])
        + dev_addr.to_bytes(4, "little")
        + fcnt.to_bytes(4, "little")
        + bytes([0, len(msg)])
    )
    return aes_cmac(nwk_s_key, b0 + msg)[:4]


def crypt_frm_payload(key: bytes, payload: bytes, dev_addr: int, fcnt: int, uplink: bool) -> bytes:
    """Chiffre/déchiffre le FRMPayload (XOR avec un flux AES-CTR "maison")."""
    out = bytearray()
    for i in range((len(payload) + 15) // 16):
        a = (
            bytes([0x01, 0, 0, 0, 0, 0 if uplink else 1])
            + dev_addr.to_bytes(4, "little")
            + fcnt.to_bytes(4, "little")
            + bytes([0, i + 1])
        )
        s = aes_encrypt(key, a)
        chunk = payload[i * 16:(i + 1) * 16]
        out += bytes(x ^ y for x, y in zip(chunk, s))
    return bytes(out)


def derive_session_keys(app_key: bytes, app_nonce: bytes, net_id: bytes, dev_nonce: bytes):
    """Dérive (NwkSKey, AppSKey) après un Join Accept (LoRaWAN 1.0.x).

    app_nonce, net_id et dev_nonce sont fournis tels que transmis sur l'air (little-endian).
    """
    def derive(prefix: int) -> bytes:
        block = bytes([prefix]) + app_nonce + net_id + dev_nonce
        return aes_encrypt(app_key, block.ljust(16, b"\x00"))

    return derive(0x01), derive(0x02)
