"""Persistance des compteurs (DevNonce, FCnt) entre deux lancements.

ChirpStack refuse un DevNonce déjà utilisé et un FCnt qui recule : il faut
donc s'en souvenir d'une exécution à l'autre.
"""

import json
import os
import random


class State:
    def __init__(self, path: str):
        self.path = path
        self.data = {}
        if os.path.exists(path):
            with open(path) as f:
                self.data = json.load(f)

    def device(self, dev_eui: str) -> dict:
        d = self.data.setdefault(dev_eui, {})
        # Départ aléatoire : limite les collisions si le fichier d'état est perdu
        d.setdefault("dev_nonce", random.randint(0, 30000))
        d.setdefault("fcnt_up", 0)
        d.setdefault("fcnt_down", -1)
        d.setdefault("mc_fcnt", {})  # dernier FCnt reçu par groupe multicast
        return d

    def next_dev_nonce(self, dev_eui: str) -> int:
        d = self.device(dev_eui)
        d["dev_nonce"] = (d["dev_nonce"] + 1) & 0xFFFF
        self.save()
        return d["dev_nonce"]

    def reset(self, dev_eui: str):
        self.data.pop(dev_eui, None)

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=2)
        os.replace(tmp, self.path)
