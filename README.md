# lorawan-sim — simulateur de passerelle et de devices LoRaWAN

Simule une (ou plusieurs) passerelle(s) LoRaWAN et des devices classe A ou C
(LoRaWAN 1.0.3, EU868) qui dialoguent avec **n'importe quel serveur ChirpStack v4**.

```
 devices simulés ──(canal radio simulé)──▶ passerelle simulée ──UDP 1700──▶ ChirpStack Gateway Bridge ──MQTT──▶ ChirpStack
   OTAA / ABP          RSSI, SNR, pertes       Semtech Packet Forwarder                                         API gRPC :8080
```

## Fonctionnalités

- **Passerelle** : protocole Semtech UDP v2 (`PUSH_DATA`, `PULL_DATA`, `PULL_RESP`, `TX_ACK`),
  statistiques périodiques, position GPS, horloge `tmst` µs, downlinks émis à l'instant demandé
  (RX1/RX2) et `TOO_LATE` si le serveur répond trop tard.
- **Devices** : join OTAA (DevNonce persistant) ou ABP, uplinks confirmés ou non,
  chiffrement/MIC conformes, réception des downlinks, ACK, réponses aux commandes MAC
  (`LinkADRReq`, `DevStatusReq`, `NewChannelReq`, `RXParamSetupReq`…), canaux CFList, ADR
  (la puissance d'émission demandée par le serveur modifie le RSSI simulé).
- **Provisionnement automatique** via l'API gRPC : passerelle, application, device profiles
  (avec décodeur JavaScript) et devices avec leurs clés.
- **Classe C** (`class: C`) : le device écoute en permanence, ChirpStack lui envoie ses
  downlinks immédiatement (RX2, 869,525 MHz) sans attendre d'uplink.
- **Tableau de bord web temps réel** (http://localhost:8000) : état des passerelles,
  carte par device (DevAddr, FCnt, DR/SF, TXPower, RSSI/SNR + historique, mesures),
  journal en direct filtrable, et actions : uplink immédiat, rejoin, pause, intervalle,
  envoi d'un downlink via ChirpStack.
- **Mode « comme en vrai »** depuis le tableau de bord (voir plus bas) : adresse du serveur
  de la passerelle modifiable, création de devices à enregistrer soi-même dans ChirpStack.
- Plusieurs passerelles possibles : un device est alors reçu par toutes celles à portée.

## Installation

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Utilisation

```bash
# 1. Générer une configuration (EUI et clés aléatoires)
.venv/bin/python -m lorasim init --host 192.168.1.50 --devices 3 --abp --class-c

# 2. Dans l'interface ChirpStack : « API keys » > Add, puis coller le jeton dans
#    config.yaml (server.api_token)

# 3. Enregistrer passerelle + devices dans ChirpStack
.venv/bin/python -m lorasim provision

# 4. Lancer la simulation (Ctrl+C pour arrêter) puis ouvrir http://localhost:8000
.venv/bin/python -m lorasim run
.venv/bin/python -m lorasim run --device sim-device-01   # un seul device
.venv/bin/python -m lorasim run --web-host 0.0.0.0       # tableau de bord visible sur le réseau
.venv/bin/python -m lorasim run --web 0                  # sans tableau de bord

# Envoyer un downlink (il part après le prochain uplink, classe A)
.venv/bin/python -m lorasim downlink sim-device-01 --interval 20 --confirmed
.venv/bin/python -m lorasim downlink sim-device-01 --hex 0102 --fport 10
.venv/bin/python -m lorasim downlink sim-actionneur-c --relay on --duration 30
.venv/bin/python -m lorasim downlink mon-afficheur --text "Salle B12\n21 °C"

# Repartir de zéro (efface les DevNonces côté serveur et les compteurs locaux)
.venv/bin/python -m lorasim reset && .venv/bin/python -m lorasim provision
```

## Déploiement avec Docker

```bash
docker compose up -d --build        # construit l'image et lance le simulateur
open http://localhost:8000          # tableau de bord
docker compose logs -f              # journal
docker compose down                 # arrêt (propre : compteurs sauvegardés)
```

- La configuration et l'état sont dans **`data/`** (volume monté sur `/data`) : `config.yaml`
  et `config.state.json` survivent aux redémarrages et peuvent être modifiés à la main.
- Au premier démarrage, `data/config.yaml` est généré avec 1 passerelle et
  `LORASIM_DEVICES` devices (0 par défaut : tout se crée depuis le tableau de bord), pointant
  vers `LORASIM_SERVER` (`host.docker.internal` = la machine hôte, donc le ChirpStack de
  `chirpstack-docker`). Pour un serveur distant, changez l'adresse depuis le tableau de bord
  ou dans `data/config.yaml`.
- Dans le conteneur, `localhost` désigne le conteneur lui-même : pour joindre l'hôte, utilisez
  `host.docker.internal`.
- Reprendre une configuration existante : copiez-la dans `data/config.yaml` (et son
  `config.state.json`) en remplaçant `server.host: localhost` par `host.docker.internal`.
- Les autres commandes passent par le même conteneur :

```bash
docker compose run --rm lorasim provision
docker compose run --rm lorasim downlink capteur-01 --interval 20
docker compose run --rm lorasim reset
```

Le port 8000 est publié sur toutes les interfaces (visible en classe) ; mettez
`"127.0.0.1:8000:8000"` dans `docker-compose.yml` pour le réserver à la machine.

## Mode « comme en vrai » : tout configurer depuis le tableau de bord

Sans passer par `provision`, on reproduit la démarche d'une vraie installation :

1. **Passerelle** — carte de la passerelle › *Configuration du packet forwarder* : saisir
   l'adresse IP et le port UDP du serveur LoRaWAN (équivalent de `serv_address` /
   `serv_port_up` dans `global_conf.json`). La passerelle se reconnecte aussitôt ; elle passe
   « connectée » dès que le Gateway Bridge répond. Dans ChirpStack : *Gateways › Add gateway*
   avec l'EUI affiché (bouton *copier*).
2. **Devices** — bouton *+ Ajouter un device* : nom, OTAA/ABP, classe, intervalle, payload ;
   DevEUI et clés sont générés (↻) ou saisis. Le device est créé **éteint**.
3. Dans ChirpStack, créer un device profile et le device en recopiant les identifiants
   affichés sur sa carte (*Identifiants à saisir dans ChirpStack*, avec la marche à suivre).
   Le codec JavaScript (capteur, relais, afficheur) est copiable depuis le formulaire de création.
4. *Allumer* le device : Join Request (OTAA) ou uplinks directs (ABP). Sans enregistrement,
   le join échoue et le device réessaie, comme un vrai capteur.

Les changements sont enregistrés dans `config.yaml` (clés `gateways[].server_host` /
`server_port`, nouveaux `devices[]`) et retrouvés au prochain lancement. *Supprimer* retire
un device du simulateur seulement : pensez à le supprimer aussi dans ChirpStack.
Si le tableau de bord est ouvert sur le réseau (`--web-host 0.0.0.0`), les clés y sont visibles.

`-c autre.yaml` permet de gérer plusieurs configurations (une par serveur, par exemple).

## Configuration (`config.yaml`)

| Clé | Rôle |
|---|---|
| `server.host` | adresse du serveur ChirpStack |
| `server.udp_port` | port du Gateway Bridge Semtech UDP (1700) |
| `server.api_port` / `api_tls` | API gRPC (port de l'interface web, 8080 par défaut) |
| `server.api_token` | clé API ChirpStack |
| `server.tenant_id` | tenant cible (vide = le premier visible) |
| `radio.loss` | taux de perte de trames (0 à 1) |
| `gateways[]` | `id` (EUI 64 bits), `name`, `lat`, `lon`, `alt` |
| `gateways[].server_host` / `server_port` | serveur propre à la passerelle (défaut : `server.host` / `udp_port`) |
| `devices[].activation` | `otaa` (`app_key`, `join_eui`) ou `abp` (`dev_addr`, `nwk_s_key`, `app_s_key`) |
| `devices[].interval` | période d'émission (s) |
| `devices[].payload` | `sensor` (temp/humidité/batterie), `relay` (relais), `display` (afficheur), `text` (`payload_text`, `{n}` = compteur) ou `hex` (`payload_hex`) |
| `devices[].confirmed` | uplinks confirmés |
| `devices[].class` | `A` (défaut) ou `C` |
| `devices[].rssi` | RSSI moyen (dBm), ou un dictionnaire `{nom_passerelle: rssi}` |
| `devices[].data_rate` | DR initial (0 = SF12 … 5 = SF7) |

Les EUI et clés doivent être **entre guillemets** si elles ne contiennent que des chiffres.
Les compteurs (DevNonce, FCnt) sont sauvegardés dans `config.state.json`.

## Payload `sensor` (FPort 2)

| Octets | Champ |
|---|---|
| 0-1 | température int16 ×10 (°C) |
| 2 | humidité (%) |
| 3-4 | batterie (mV) |
| 5 | compteur |

Le codec JavaScript est installé automatiquement dans le device profile : ChirpStack
affiche directement `temperature`, `humidity`, `battery_mv`. Un downlink FPort 1 de 2 octets
change l'intervalle d'émission de n'importe quel device (`{"interval": 20}` en JSON depuis
l'interface fonctionne aussi).

## Actionneurs : relais (FPort 3) et afficheur (FPort 4)

Un actionneur reçoit ses commandes en downlink et **confirme aussitôt son nouvel état par un
uplink**. Il est en général alimenté sur secteur (batterie « externe » dans `DevStatusAns`) et
en classe C pour exécuter les commandes sans attendre. Le tableau de bord affiche un voyant
pour le relais et un écran OLED 128×64 pour l'afficheur, avec des boutons qui passent par la
file de downlinks ChirpStack (clé API nécessaire).

**Relais** (`payload: relay`)

| Sens | Octets | Contenu |
|---|---|---|
| downlink | 0 | `0` OFF, `1` ON, `2` basculer |
| downlink | 1-2 (facultatif) | durée en s : retour à l'état précédent ensuite |
| uplink | 0 | état (0/1) |
| uplink | 1-2 | nombre de commutations |
| uplink | 3 | origine : 0 démarrage, 1 commande réseau, 2 bouton local, 3 fin de temporisation |

JSON à saisir dans ChirpStack (*Queue*, FPort 3) : `{"relay": 1}`, `{"relay": 0}`,
`{"relay": "toggle", "duration": 30}`. Le bouton *Bouton local* du tableau de bord simule un
appui sur le bouton physique : l'état change et remonte au serveur.

**Afficheur** (`payload: display`, modèle : carte Heltec avec écran OLED 128×64)

| Sens | Octets | Contenu |
|---|---|---|
| downlink | 0-50 | texte UTF-8, `\n` = nouvelle ligne, vide = effacer l'écran |
| uplink | 0-1 | nombre de messages reçus |
| uplink | 2 | longueur du dernier message (octets) |

JSON (*Queue*, FPort 4) : `{"text": "Salle B12\n21 °C"}`. L'écran affiche 5 lignes de
21 caractères (les lignes trop longues sont coupées). Le texte est limité à **51 octets** :
c'est la taille maximale d'un downlink à SF12, utilisé en RX2 et donc par la classe C.

## Organisation du code

| Fichier | Contenu |
|---|---|
| `lorasim/crypto.py` | AES, CMAC, MIC, chiffrement FRMPayload, dérivation des clés de session |
| `lorasim/phy.py` | construction / décodage des trames (Join Request/Accept, data up/down) |
| `lorasim/mac.py` | commandes MAC et réponses |
| `lorasim/device.py` | machine à états d'un device classe A |
| `lorasim/gateway.py` | Semtech UDP Packet Forwarder |
| `lorasim/radio.py` | canal radio simulé (RSSI, SNR, sensibilité par SF, pertes) |
| `lorasim/provision.py` | API gRPC ChirpStack |
| `lorasim/config.py` | lecture / écriture de `config.yaml` |
| `lorasim/web.py`, `static/index.html` | tableau de bord (aiohttp, Server-Sent Events) |
| `lorasim/events.py` | bus d'événements alimentant le journal web |

## Limites

- Région EU868 uniquement, LoRaWAN 1.0.3, classes A et C (pas de classe B).
- Les fenêtres RX ne sont pas vérifiées côté device : un downlink est accepté dès que la
  passerelle l'« émet » (à l'instant `tmst` demandé par le serveur).
