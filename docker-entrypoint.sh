#!/bin/sh
# Point d'entrée du conteneur : toutes les commandes lorasim travaillent sur /data/config.yaml.
#   docker compose up                          -> lorasim run (tableau de bord sur le port 8000)
#   docker compose run --rm lorasim provision  -> lorasim provision
set -e

CONFIG=/data/config.yaml

# Lancé en root : sous Linux, Docker crée ./data (absent du dépôt) en root et lorasim ne
# pourrait pas y écrire. On lui donne ce qui ne lui appartient pas, puis on se relance
# sous l'utilisateur lorasim.
if [ "$(id -u)" = 0 ]; then
    find /data ! -user lorasim -exec chown lorasim:lorasim {} +
    exec setpriv --reuid=lorasim --regid=lorasim --init-groups "$0" "$@"
fi

# Premier démarrage : configuration générée (1 passerelle, LORASIM_DEVICES devices OTAA)
if [ ! -f "$CONFIG" ] && [ "$1" != "init" ]; then
    python -m lorasim -c "$CONFIG" init --host "$LORASIM_SERVER" --devices "$LORASIM_DEVICES"
fi

exec python -m lorasim -c "$CONFIG" "$@"
