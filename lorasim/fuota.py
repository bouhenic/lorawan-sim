"""FUOTA côté device : couches applicatives LoRaWAN de la LoRa Alliance, versions 1.0.0.

  TS003 Application Layer Clock Synchronization   FPort 202
  TS004 Fragmented Data Block Transport            FPort 201
  TS005 Remote Multicast Setup                     FPort 200

Déroulement piloté par un déploiement FUOTA ChirpStack : le serveur crée le groupe multicast
dans le device (TS005, clé chiffrée avec une clé dérivée de la GenAppKey), annonce la session
de fragmentation (TS004), programme une session multicast de classe C, envoie les fragments
puis demande l'état de la session. Chaque réponse part dans un uplink séparé : ChirpStack
attend une seule commande par trame.
"""

import asyncio
import hashlib
import random
import time

from .crypto import aes_encrypt

CLOCK_PORT, FRAG_PORT, MCSETUP_PORT = 202, 201, 200
PORTS = (CLOCK_PORT, FRAG_PORT, MCSETUP_PORT)

GPS_EPOCH_UNIX = 315_964_800   # 6 janvier 1980
GPS_LEAP_SECONDS = 18          # écart GPS - UTC depuis 2017
MAX_FILE = 64 * 1024           # mémoire « disponible » pour le fichier reçu
FW_MAGIC = b"LORASIM-FW "      # en-tête facultatif portant le numéro de version


def gps_now(offset: float = 0) -> float:
    return time.time() - GPS_EPOCH_UNIX + GPS_LEAP_SECONDS + offset


def mc_session_keys(gen_app_key: bytes, mc_addr: int, mc_key_encrypted: bytes) -> tuple[bytes, bytes]:
    """TS005 + LoRaWAN 1.0.x : McRootKey = aes(GenAppKey, 0…), McKEKey = aes(McRootKey, 0…).
    Le serveur chiffre McKey par un *déchiffrement* AES : le device la retrouve en chiffrant."""
    mc_ke_key = aes_encrypt(aes_encrypt(gen_app_key, bytes(16)), bytes(16))
    mc_key = aes_encrypt(mc_ke_key, mc_key_encrypted)
    addr = mc_addr.to_bytes(4, "little")
    mc_app_s_key = aes_encrypt(mc_key, b"\x01" + addr + bytes(11))
    mc_nwk_s_key = aes_encrypt(mc_key, b"\x02" + addr + bytes(11))
    return mc_nwk_s_key, mc_app_s_key


# --- TS004 : fragments de redondance -------------------------------------------------

def _prbs23(x: int) -> int:
    return (x >> 1) | (((x & 1) ^ ((x >> 5) & 1)) << 22)


def matrix_line(n: int, m: int) -> int:
    """Ligne n (1 = premier fragment de redondance) de la matrice de parité, en masque de bits :
    le bit i indique que le fragment i + 1 entre dans le XOR."""
    mm = 1 if m & (m - 1) == 0 else 0
    x = 1 + 1001 * n
    mask = 0
    for _ in range(m // 2):
        r = 1 << 16
        while r >= m:
            x = _prbs23(x)
            r = x % (m + mm)
        mask |= 1 << r
    return mask


class FragSession:
    """Reconstitution d'un fichier à partir de fragments simples et de fragments de redondance
    (XOR de plusieurs fragments) : élimination de Gauss sur GF(2), au fil de l'eau."""

    def __init__(self, index: int, nb_frag: int, frag_size: int, padding: int,
                 descriptor: bytes, block_ack_delay: int):
        self.index, self.nb_frag, self.frag_size = index, nb_frag, frag_size
        self.padding, self.descriptor, self.block_ack_delay = padding, descriptor, block_ack_delay
        self.rows = {}        # bit pivot -> (masque, données), forme réduite
        self.received = 0     # fragments reçus (utiles ou non)
        self.coded = 0        # dont fragments de redondance
        self.last_n = 0       # plus grand numéro de fragment vu
        self.data = None      # fichier reconstitué

    @property
    def missing(self) -> int:
        return self.nb_frag - len(self.rows)

    @property
    def lost(self) -> int:
        return max(0, self.last_n - self.received)

    def add(self, n: int, payload: bytes) -> bool:
        """Ajoute le fragment n (1…). Renvoie True si le fichier vient d'être reconstitué."""
        self.received += 1
        self.last_n = max(self.last_n, n)
        if n > self.nb_frag:
            self.coded += 1
        if self.data is not None or n == 0:
            return False
        mask = 1 << (n - 1) if n <= self.nb_frag else matrix_line(n - self.nb_frag, self.nb_frag)
        val = int.from_bytes(payload[:self.frag_size].ljust(self.frag_size, b"\0"), "big")
        for p, (pm, pv) in self.rows.items():
            if mask >> p & 1:
                mask ^= pm
                val ^= pv
        if not mask:
            return False  # fragment déjà connu ou combinaison de fragments connus
        pivot = (mask & -mask).bit_length() - 1
        for p, (pm, pv) in list(self.rows.items()):
            if pm >> pivot & 1:
                self.rows[p] = (pm ^ mask, pv ^ val)
        self.rows[pivot] = (mask, val)
        if self.missing:
            return False
        blob = b"".join(self.rows[i][1].to_bytes(self.frag_size, "big") for i in range(self.nb_frag))
        self.data = blob[:len(blob) - self.padding] if self.padding else blob
        return True


def firmware_version(data: bytes) -> str:
    """Version lue dans l'en-tête « LORASIM-FW x.y.z » du fichier, sinon début du SHA-256."""
    if data.startswith(FW_MAGIC):
        version = data[len(FW_MAGIC):].split(b"\n", 1)[0].decode(errors="replace").strip()
        if version:
            return version[:20]
    return "sha256:" + hashlib.sha256(data).hexdigest()[:8]


# --- l'agent FUOTA d'un device --------------------------------------------------------

class Fuota:
    def __init__(self, device, gen_app_key: bytes):
        self.dev = device
        self.gen_app_key = gen_app_key
        self.groups = {}      # McGroupID -> groupe multicast (même format que la config)
        self.sessions = {}    # FragIndex -> FragSession
        self.clock_offset = 0  # correction d'horloge reçue en TS003 (s)
        self.token = 0
        self.stage = "en attente d'un déploiement"
        self.result = None    # dernier fichier reçu : taille, empreinte, version

    def log(self, msg: str, level: str = "info"):
        self.dev.log(msg, level)

    def set_stage(self, stage: str, level: str = "info"):
        self.stage = stage
        self.log(f"FUOTA : {stage}", level)

    def reply(self, fport: int, payload: bytes, delay: float = 0):
        self.dev.queue_app_uplink(fport, payload, delay)

    # --- réception -------------------------------------------------------------------

    def handle(self, fport: int, data: bytes, multicast: bool = False):
        if not data:
            return
        handler = {MCSETUP_PORT: self._mcsetup, FRAG_PORT: self._frag, CLOCK_PORT: self._clock}[fport]
        try:
            handler(data, multicast)
        except (IndexError, ValueError) as e:
            self.log(f"FUOTA : commande FPort {fport} mal formée ({data.hex()}) : {e}", "warn")

    def _mcsetup(self, b: bytes, multicast: bool):
        cid = b[0]
        if cid == 0x00:     # PackageVersionReq
            self.reply(MCSETUP_PORT, bytes([0x00, 2, 1]))
        elif cid == 0x01:   # McGroupStatusReq
            mask = b[1] & 0x0F
            ids = [i for i in sorted(self.groups) if mask >> i & 1]
            ans = bytes([0x01, (len(self.groups) << 4) | sum(1 << i for i in ids)])
            for i in ids:
                ans += bytes([i]) + int(self.groups[i]["mc_addr"], 16).to_bytes(4, "little")
            self.reply(MCSETUP_PORT, ans)
        elif cid == 0x02:   # McGroupSetupReq
            gid = b[1] & 0x03
            mc_addr = int.from_bytes(b[2:6], "little")
            nwk, app = mc_session_keys(self.gen_app_key, mc_addr, b[6:22])
            fmin, fmax = int.from_bytes(b[22:26], "little"), int.from_bytes(b[26:30], "little")
            name = f"fuota-{gid}"
            self.groups[gid] = {
                "name": name, "mc_addr": f"{mc_addr:08x}", "mc_nwk_s_key": nwk.hex(),
                "mc_app_s_key": app.hex(), "fuota": True, "fcnt_max": fmax,
                "dr": None, "frequency": None, "start": None, "end": None,
            }
            self.dev.st["mc_fcnt"][name] = fmin - 1
            self.dev.state.save()
            self.set_stage(f"groupe multicast {gid} configuré à distance (McAddr {mc_addr:08x}, "
                           "clés dérivées de la GenAppKey)", "ok")
            self.reply(MCSETUP_PORT, bytes([0x02, gid]))
        elif cid == 0x03:   # McGroupDeleteReq
            gid = b[1] & 0x03
            known = self.groups.pop(gid, None) is not None
            if known:
                self.log(f"FUOTA : groupe multicast {gid} supprimé")
            self.reply(MCSETUP_PORT, bytes([0x03, gid | (0 if known else 0x04)]))
        elif cid == 0x04:   # McClassCSessionReq
            self._class_c_session(b)
        elif cid == 0x05:   # McClassBSessionReq : la classe B n'est pas simulée
            self.log("FUOTA : session de classe B demandée, non supportée (classe B non simulée)", "warn")
            self.reply(MCSETUP_PORT, bytes([0x05, (b[1] & 0x03) | 0x04]))
        else:
            self.log(f"FUOTA : commande TS005 inconnue 0x{cid:02x}", "warn")

    def _class_c_session(self, b: bytes):
        gid = b[1] & 0x03
        session_time = int.from_bytes(b[2:6], "little")
        timeout = 1 << (b[6] & 0x0F)
        freq = int.from_bytes(b[7:10], "little") * 100
        dr = b[10]
        group = self.groups.get(gid)
        status = gid
        if not 0 <= dr <= 5:
            status |= 0x04
        if not 863_000_000 <= freq <= 870_000_000:
            status |= 0x08
        if group is None:
            status |= 0x10
        if status != gid:
            reasons = [r for bit, r in ((0x04, f"DR{dr} invalide"), (0x08, f"fréquence {freq} Hz invalide"),
                                        (0x10, f"groupe {gid} inconnu")) if status & bit]
            self.set_stage("session multicast refusée : " + ", ".join(reasons), "warn")
            self.reply(MCSETUP_PORT, bytes([0x04, status]))
            return
        # SessionTime est en secondes GPS modulo 2^32
        to_start = (session_time - int(gps_now(self.clock_offset))) % (1 << 32)
        if to_start >= 1 << 31:
            to_start = 0   # instant déjà passé : session ouverte tout de suite
        to_start = min(to_start, (1 << 24) - 1)
        group.update(dr=dr, frequency=freq, start=time.time() + to_start, end=time.time() + to_start + timeout)
        loop = asyncio.get_running_loop()
        loop.call_later(to_start, self._session_open, gid, group["start"])
        loop.call_later(to_start + timeout, self._session_close, gid, group["start"])
        self.set_stage(f"session multicast de classe C programmée dans {to_start} s, durée {timeout} s, "
                       f"{freq / 1e6:.3f} MHz DR{dr}", "ok")
        self.reply(MCSETUP_PORT, bytes([0x04, gid]) + to_start.to_bytes(3, "little"))

    def _session_open(self, gid: int, start: float):
        group = self.groups.get(gid)
        if group is None or group["start"] != start:
            return  # session remplacée ou groupe supprimé entre-temps
        temporary = " (passage temporaire en classe C)" if self.dev.device_class != "C" else ""
        self.set_stage(f"session multicast ouverte{temporary} : écoute sur "
                       f"{group['frequency'] / 1e6:.3f} MHz DR{group['dr']}", "ok")

    def _session_close(self, gid: int, start: float):
        group = self.groups.get(gid)
        if group is None or group["start"] != start:
            return
        back = ", retour en classe A" if self.dev.device_class != "C" else ""
        incomplete = [s for s in self.sessions.values() if s.data is None]
        if incomplete:
            s = incomplete[0]
            self.set_stage(f"fin de la session multicast{back} : fichier incomplet, "
                           f"{s.missing} fragment(s) manquant(s) sur {s.nb_frag}", "warn")
        else:
            self.log(f"FUOTA : fin de la session multicast{back}")

    def listening(self, group: dict) -> bool:
        return group["start"] is not None and group["start"] <= time.time() <= group["end"]

    def _frag(self, b: bytes, multicast: bool):
        cid = b[0]
        if cid == 0x00:     # PackageVersionReq
            self.reply(FRAG_PORT, bytes([0x00, 3, 1]))
        elif cid == 0x01:   # FragSessionStatusReq
            participants, index = b[1] & 0x01, (b[1] >> 1) & 0x03
            s = self.sessions.get(index)
            if s is None or (not participants and s.missing == 0):
                return
            # Réponse à une requête multicast : délai aléatoire (BlockAckDelay) contre les collisions
            delay = random.uniform(0, 1 << (s.block_ack_delay + 4)) if multicast else 0
            received = min(s.received, 0x3FFF) | (index << 14)
            self.log(f"FUOTA : état demandé par le serveur : {s.received} fragment(s) reçu(s), "
                     f"{s.missing} manquant(s)")
            self.reply(FRAG_PORT, bytes([0x01]) + received.to_bytes(2, "little")
                       + bytes([min(s.missing, 255), 0]), delay)
        elif cid == 0x02:   # FragSessionSetupReq
            index = (b[1] >> 4) & 0x03
            nb_frag, frag_size = int.from_bytes(b[2:4], "little"), b[4]
            block_ack_delay, matrix = b[5] & 0x07, (b[5] >> 3) & 0x07
            padding, descriptor = b[6], bytes(b[7:11])
            status = index << 6
            if matrix != 0:
                status |= 0x01   # seul l'encodage par défaut de TS004 est connu
            if nb_frag * frag_size > MAX_FILE:
                status |= 0x02
            if status & 0x0F:
                self.set_stage(f"session de fragmentation refusée (matrice {matrix}, "
                               f"{nb_frag} × {frag_size} octets)", "warn")
            else:
                self.sessions[index] = FragSession(index, nb_frag, frag_size, padding, descriptor,
                                                   block_ack_delay)
                size = nb_frag * frag_size - padding
                self.set_stage(f"session de fragmentation {index} : fichier de {size} octets en "
                               f"{nb_frag} fragments de {frag_size} octets "
                               f"(descripteur {descriptor.hex()})", "ok")
            self.reply(FRAG_PORT, bytes([0x02, status]))
        elif cid == 0x03:   # FragSessionDeleteReq
            index = b[1] & 0x03
            known = self.sessions.pop(index, None) is not None
            self.reply(FRAG_PORT, bytes([0x03, index | (0 if known else 0x04)]))
        elif cid == 0x08:   # DataFragment
            v = int.from_bytes(b[1:3], "little")
            n, index = v & 0x3FFF, v >> 14
            s = self.sessions.get(index)
            if s is None:
                self.log(f"FUOTA : fragment {n} reçu sans session de fragmentation {index}", "warn")
                return
            first = s.received == 0
            done = s.add(n, b[3:])
            if first:
                self.set_stage(f"réception des fragments (session {index}, {s.nb_frag} attendus)")
            if done:
                self._complete(s)
        else:
            self.log(f"FUOTA : commande TS004 inconnue 0x{cid:02x}", "warn")

    def _complete(self, s: FragSession):
        digest = hashlib.sha256(s.data).hexdigest()
        version = firmware_version(s.data)
        old = self.dev.firmware
        self.result = {"size": len(s.data), "sha256": digest, "version": version}
        repaired = max(0, s.nb_frag - (s.received - s.coded))  # fragments simples jamais reçus
        how = (f", dont {repaired} fragment(s) perdu(s) reconstitué(s) grâce à la redondance"
               if repaired > 0 else "")
        self.set_stage(f"fichier reconstitué : {len(s.data)} octets avec {s.received} fragment(s) "
                       f"reçu(s){how}", "ok")
        self.log(f"FUOTA : vérification SHA-256 {digest[:16]}…, redémarrage simulé : "
                 f"firmware {old} → {version}", "ok")
        self.dev.set_firmware(version)
        self.stage = f"firmware {version} installé"

    def _clock(self, b: bytes, multicast: bool):
        cid = b[0]
        if cid == 0x00:     # PackageVersionReq
            self.reply(CLOCK_PORT, bytes([0x00, 1, 1]))
        elif cid == 0x01:   # AppTimeAns
            correction = int.from_bytes(b[1:5], "little", signed=True)
            if (b[5] & 0x0F) != self.token:
                return
            self.clock_offset += correction
            self.log(f"FUOTA : horloge synchronisée par le serveur (correction {correction:+d} s)", "ok")
        elif cid == 0x02:   # DeviceAppTimePeriodicityReq : accepté, pas de resynchronisation périodique
            self.reply(CLOCK_PORT, bytes([0x02, 0]) + (int(gps_now(self.clock_offset)) & 0xFFFFFFFF).to_bytes(4, "little"))
        elif cid == 0x03:   # ForceDeviceResyncReq
            self.request_time()

    def request_time(self):
        """AppTimeReq : heure du device en secondes GPS, réponse demandée."""
        self.token = (self.token + 1) & 0x0F
        t = int(gps_now(self.clock_offset)) & 0xFFFFFFFF
        self.reply(CLOCK_PORT, bytes([0x01]) + t.to_bytes(4, "little") + bytes([0x10 | self.token]))

    # --- tableau de bord ----------------------------------------------------------------

    def snapshot(self) -> dict:
        now = time.time()
        session = None
        for g in self.groups.values():
            if g["start"] is not None:
                session = {"start": g["start"], "end": g["end"], "frequency": g["frequency"], "dr": g["dr"],
                           "open": g["start"] <= now <= g["end"]}
        frag = None
        if self.sessions:
            s = self.sessions[max(self.sessions)]
            frag = {"nb_frag": s.nb_frag, "useful": len(s.rows), "received": s.received,
                    "coded": s.coded, "lost": s.lost, "done": s.data is not None}
        return {"stage": self.stage, "session": session, "frag": frag, "result": self.result,
                "groups": len(self.groups)}
