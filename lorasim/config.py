"""Lecture et écriture du fichier de configuration YAML."""

import os

import yaml


def load_config(path: str) -> dict:
    if not os.path.exists(path):
        raise SystemExit(f"{path} introuvable : lancez d'abord « python -m lorasim init »")
    with open(path) as f:
        cfg = yaml.safe_load(f)
    cfg.setdefault("server", {}).setdefault("host", "localhost")
    if not cfg.get("gateways"):
        raise SystemExit("aucune passerelle définie dans la configuration")
    if not cfg.get("devices"):
        cfg["devices"] = []
    # Un EUI non quoté composé uniquement de chiffres serait lu comme un entier par YAML
    for item in cfg["gateways"] + cfg["devices"]:
        for k in ("id", "dev_eui", "join_eui", "app_key", "dev_addr", "nwk_s_key", "app_s_key"):
            if k in item and not isinstance(item[k], str):
                raise SystemExit(f"{item.get('name', '?')} : la valeur de « {k} » doit être entre guillemets")
    return cfg


def save_config(path: str, cfg: dict):
    """Réécrit la configuration en conservant les commentaires d'en-tête."""
    header = ""
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                if line.startswith("#") or not line.strip():
                    header += line
                else:
                    break
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(header)
        yaml.safe_dump(cfg, f, sort_keys=False, allow_unicode=True)
    os.replace(tmp, path)


def state_path(config_path: str) -> str:
    return os.path.splitext(config_path)[0] + ".state.json"
