"""Simulateur de passerelle et de devices LoRaWAN pour ChirpStack.

  python -m lorasim init --host 192.168.1.10 --devices 3   # génère config.yaml
  python -m lorasim provision                               # enregistre tout dans ChirpStack
  python -m lorasim run                                     # lance la simulation
  python -m lorasim downlink sim-device-01 --interval 20     # envoie un downlink
  python -m lorasim downlink sim-relais --relay on           # commande un relais / un afficheur
  python -m lorasim reset                                   # efface DevNonces / compteurs
"""

import argparse
import asyncio
import os
import secrets
import signal
import sys

import yaml

from .config import load_config, state_path
from .log import log
from .state import State


def cmd_init(args):
    if os.path.exists(args.config) and not args.force:
        raise SystemExit(f"{args.config} existe déjà (utilisez --force pour l'écraser)")
    devices = []
    for i in range(args.devices):
        dev = {
            "name": f"sim-device-{i + 1:02d}",
            "dev_eui": "70b3d57ed0" + secrets.token_hex(3),
            "join_eui": "0000000000000000",
            "activation": "otaa",
            "app_key": secrets.token_hex(16),
            "interval": 30,
            "payload": "sensor",
            "confirmed": False,
        }
        devices.append(dev)
    if args.class_c:
        devices.append({
            "name": "sim-actionneur-c",
            "dev_eui": "70b3d57ed0" + secrets.token_hex(3),
            "join_eui": "0000000000000000",
            "activation": "otaa",
            "class": "C",
            "app_key": secrets.token_hex(16),
            "interval": 120,
            "payload": "relay",
        })
    if args.abp:
        devices.append({
            "name": "sim-device-abp",
            "dev_eui": "70b3d57ed0" + secrets.token_hex(3),
            "activation": "abp",
            "dev_addr": "01" + secrets.token_hex(3),
            "nwk_s_key": secrets.token_hex(16),
            "app_s_key": secrets.token_hex(16),
            "interval": 45,
            "payload": "text",
            "payload_text": "hello {n}",
            "fport": 10,
        })
    cfg = {
        "server": {
            "host": args.host,
            "udp_port": 1700,
            "api_port": 8080,
            "api_tls": False,
            "api_token": "",
            "tenant_id": "",
        },
        "radio": {"loss": 0.0},
        "gateways": [{
            "id": "0016c001ff" + secrets.token_hex(3),
            "name": "sim-gateway-01",
            "lat": 48.9047, "lon": 2.3057, "alt": 35,
        }],
        "devices": devices,
    }
    header = (
        "# Configuration du simulateur LoRaWAN\n"
        "# server.host     : adresse du serveur ChirpStack\n"
        "# server.udp_port : port du ChirpStack Gateway Bridge (Semtech UDP)\n"
        "# server.api_port : port de l'API gRPC ChirpStack (le même que l'interface web, 8080 par défaut)\n"
        "# server.api_token: clé API à créer dans l'interface ChirpStack (menu « API keys »)\n"
        "# devices[].class : A (défaut) ou C (écoute permanente, downlinks immédiats)\n"
        "# devices[].payload : sensor, relay (FPort 3), display (FPort 4), text ou hex\n"
        "# devices[].rssi  : RSSI moyen simulé (dBm), aléatoire si absent\n"
        "# gateways[].server_host / server_port : serveur propre à une passerelle\n"
        "#   (modifiable depuis le tableau de bord, défaut : server.host / udp_port)\n\n"
    )
    with open(args.config, "w") as f:
        f.write(header)
        yaml.safe_dump(cfg, f, sort_keys=False, allow_unicode=True)
    log("init", f"{args.config} créé : 1 passerelle, {len(devices)} device(s)", "ok")
    log("init", "renseignez server.api_token puis lancez « python -m lorasim provision »")


def cmd_provision(args):
    from .provision import provision
    cfg = load_config(args.config)
    provision(cfg, State(state_path(args.config)))


def cmd_downlink(args):
    from . import payloads
    from .provision import ChirpStack, enqueue
    cfg = load_config(args.config)
    dev = next((d for d in cfg["devices"] if d["name"] == args.device), None)
    if dev is None:
        raise SystemExit(f"device inconnu : {args.device}")
    try:
        if args.interval is not None:
            fport, data = 1, args.interval.to_bytes(2, "big")
        elif args.relay is not None:
            fport, data = payloads.relay_command(args.relay, args.duration)
        elif args.text is not None:
            fport, data = payloads.display_command(args.text.replace("\\n", "\n"))
        else:
            fport, data = args.fport, bytes.fromhex(args.hex or "")
    except ValueError as e:
        raise SystemExit(f"erreur : {e}")
    import grpc
    try:
        qid = enqueue(ChirpStack(cfg["server"]), dev["dev_eui"], fport, data, args.confirmed)
    except (grpc.RpcError, ValueError) as e:
        msg = f"{e.code().name} - {e.details()}" if isinstance(e, grpc.RpcError) else str(e)
        raise SystemExit(f"erreur : {msg}")
    when = ("immédiatement (classe C)" if str(dev.get("class", "A")).upper() == "C"
            else "après le prochain uplink du device (classe A)")
    log("downlink", f"{data.hex()} (FPort {fport}) en file d'attente, id {qid} : envoi {when}", "ok")


def cmd_reset(args):
    from .provision import reset
    cfg = load_config(args.config)
    reset(cfg, State(state_path(args.config)))


async def run(cfg: dict, state: State, only: list[str] | None, web_host: str, web_port: int,
              config_path: str):
    from .device import Device
    from .gateway import Gateway
    from .radio import RadioMedium

    server = cfg["server"]
    medium = RadioMedium(loss=float(cfg.get("radio", {}).get("loss", 0)))
    for gcfg in cfg["gateways"]:
        host = gcfg.get("server_host") or server["host"]
        port = int(gcfg.get("server_port") or server.get("udp_port", 1700))
        gw = Gateway(gcfg, host, port, medium)
        medium.gateways.append(gw)
        await gw.start()
    for dcfg in cfg["devices"]:
        if only and dcfg["name"] not in only:
            continue
        medium.devices.append(Device(dcfg, medium, state))
    if web_port:
        from .web import Dashboard
        await Dashboard(cfg, medium, state, config_path).start(web_host, web_port)
    log("sim", f"{len(medium.gateways)} passerelle(s), {len(medium.devices)} device(s) — Ctrl+C pour arrêter")
    # Une tâche par device : un device peut être ajouté ou supprimé en cours de route
    for d in medium.devices:
        d.start()
    # docker stop envoie SIGTERM : arrêt propre, comme avec Ctrl+C
    stop = asyncio.Event()
    try:
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop.set)
    except NotImplementedError:  # Windows
        pass
    await stop.wait()


def cmd_run(args):
    cfg = load_config(args.config)
    state = State(state_path(args.config))
    try:
        asyncio.run(run(cfg, state, args.device, args.web_host, args.web, args.config))
    except KeyboardInterrupt:
        pass
    finally:
        state.save()
        log("sim", "arrêt")


def main(argv=None):
    p = argparse.ArgumentParser(prog="lorasim", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-c", "--config", default="config.yaml", help="fichier de configuration")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="génère un fichier de configuration")
    s.add_argument("--host", default="localhost", help="adresse du serveur ChirpStack")
    s.add_argument("--devices", type=int, default=3, help="nombre de devices OTAA")
    s.add_argument("--abp", action="store_true", help="ajoute un device ABP")
    s.add_argument("--class-c", action="store_true", help="ajoute un device de classe C")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("provision", help="crée passerelle/application/devices dans ChirpStack")
    s.set_defaults(func=cmd_provision)

    s = sub.add_parser("run", help="lance la simulation")
    s.add_argument("--device", action="append", help="ne simuler que ce device (répétable)")
    s.add_argument("--web", type=int, default=8000, metavar="PORT",
                   help="port du tableau de bord web (0 = désactivé, défaut 8000)")
    s.add_argument("--web-host", default="127.0.0.1",
                   help="interface d'écoute du tableau de bord (0.0.0.0 = accessible sur le réseau)")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("downlink", help="met un downlink en file d'attente dans ChirpStack")
    s.add_argument("device", help="nom du device")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--interval", type=int, help="nouvel intervalle d'émission (s), FPort 1")
    g.add_argument("--hex", help="données brutes en hexadécimal")
    g.add_argument("--relay", choices=["on", "off", "toggle"], help="commande du relais (FPort 3)")
    g.add_argument("--text", help="texte de l'afficheur, \\n = retour à la ligne (FPort 4)")
    s.add_argument("--duration", type=int, default=0,
                   help="avec --relay : retour à l'état précédent après N secondes")
    s.add_argument("--fport", type=int, default=10)
    s.add_argument("--confirmed", action="store_true")
    s.set_defaults(func=cmd_downlink)

    s = sub.add_parser("reset", help="efface les DevNonces (serveur) et compteurs (local)")
    s.set_defaults(func=cmd_reset)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
