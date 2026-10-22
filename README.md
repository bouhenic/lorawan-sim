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
- **Actionneurs** : relais et afficheur OLED commandés par downlink (voir plus bas).
- **Multicast** : groupes de devices de classe C commandés par un seul downlink (voir plus bas).
- **FUOTA** : mise à jour de firmware par radio, pilotée par un déploiement FUOTA ChirpStack
  (TS003, TS004, TS005 v1.0.0, voir plus bas).
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
.venv/bin/python -m lorasim downlink --group eclairage --relay off     # multicast

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
| `devices[].gen_app_key` | clé FUOTA (*Gen App Key* dans ChirpStack) ; sans elle, pas de FUOTA |
| `devices[].mc_loss` | perte simulée des trames multicast, fragments FUOTA compris (0 à 1) |
| `devices[].firmware` | version de firmware de départ (défaut `1.0.0`) |
| `multicast_groups[]` | `name`, `mc_addr`, `mc_nwk_s_key`, `mc_app_s_key`, `dr`, `frequency` (Hz), `devices` (noms des membres) |

Les EUI et clés doivent être **entre guillemets** si elles ne contiennent que des chiffres.
Les compteurs (DevNonce, FCnt, FCnt multicast) et la version de firmware installée sont
sauvegardés dans `config.state.json`.

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

JSON à saisir dans ChirpStack (*Queue*) : `{"relay": 1}`, `{"relay": 0}`,
`{"relay": "toggle", "duration": 30}`. Le bouton *Bouton local* du tableau de bord simule un
appui sur le bouton physique : l'état change et remonte au serveur.

**Afficheur** (`payload: display`, modèle : carte Heltec avec écran OLED 128×64)

| Sens | Octets | Contenu |
|---|---|---|
| downlink | 0-50 | texte UTF-8, `\n` = nouvelle ligne, vide = effacer l'écran |
| uplink | 0-1 | nombre de messages reçus |
| uplink | 2 | longueur du dernier message (octets) |

JSON (*Queue*) : `{"text": "Salle B12\n21 °C"}`. L'écran affiche 5 lignes de
21 caractères (les lignes trop longues sont coupées). Le texte est limité à **51 octets** :
c'est la taille maximale d'un downlink à SF12, utilisé en RX2 et donc par la classe C.

En **JSON**, ChirpStack v4 envoie la trame sur le `fPort` renvoyé par `encodeDownlink` (1, 3 ou 4
selon la clé `interval`, `relay` ou `text`) : le FPort saisi dans *Queue* est ignoré. En **HEX** ou
**Base64**, c'est le FPort saisi qui compte, et un actionneur refuse une commande reçue sur un autre
port que le sien (« commande non reconnue » dans le journal).

## Multicast (groupes de classe C)

Un groupe multicast a sa propre adresse (**McAddr**) et ses propres clés de session
(**McNwkSKey**, **McAppSKey**), partagées par tous ses membres. ChirpStack émet le downlink
**une seule fois**, sur la fréquence et le data rate du groupe (par défaut 869,525 MHz, DR0/SF12),
et tous les membres qui écoutent le reçoivent en même temps. Exemple : un seul downlink
`--relay off` éteint tous les relais du groupe « eclairage ».

```bash
.venv/bin/python -m lorasim init --devices 0 --multicast   # 2 relais classe C + groupe « eclairage »
.venv/bin/python -m lorasim provision                       # crée aussi le groupe dans ChirpStack
.venv/bin/python -m lorasim downlink --group eclairage --relay on
```

Comportement des devices simulés, conforme à LoRaWAN :

- seuls les membres de **classe C**, allumés et joints, reçoivent (un membre de classe A est
  signalé dans le journal : « ne recevra pas ») ;
- le device n'entend la trame que si la **fréquence et le data rate** du groupe dans ChirpStack
  sont ceux qu'il attend (sinon : « non reçu » dans le journal) ;
- MIC vérifié avec la McNwkSKey, compteur de trames propre au groupe (une trame plus ancienne,
  par exemple après avoir recréé le groupe dans ChirpStack, est rejetée) ;
- un downlink multicast n'est jamais confirmé et ne porte pas de commandes MAC ;
- un actionneur confirme son nouvel état par un uplink **après un délai aléatoire de 0 à 10 s**,
  pour que tous les membres ne répondent pas en même temps.

Tableau de bord : section **Groupes multicast** pour créer un groupe (adresse et clés générées),
cocher ses membres, envoyer une commande (relais, texte pour les afficheurs, octets bruts) et
afficher les paramètres à recopier dans ChirpStack (*Applications › Multicast groups ›
Add multicast-group*, *Group type* `Class-C`, *Class-C scheduling type* `Delay` ; membres :
*Devices*, cocher, *Selected devices › Add to multicast-group*).

À savoir côté ChirpStack (vérifié avec la version 4.16) :

- *Frequency (Hz)* est **obligatoire en pratique** : laissé vide, il vaut 0 et ChirpStack émet
  quand même, sur 0 Hz ; aucun membre ne reçoit (le journal du simulateur l'indique) ;
- la file d'un groupe n'accepte que des octets (*Data (HEX)*) : pas de JSON ni de codec ;
- associer une passerelle au groupe est facultatif : sans passerelle, ChirpStack émet par celles
  qui entendent les membres (`provision` associe quand même les passerelles de la config) ;
- un groupe créé à la main est retrouvé par son McAddr, quelle que soit l'application.

## FUOTA (mise à jour de firmware par radio)

Le FUOTA réutilise le multicast, mais **c'est le serveur qui configure tout à distance**, avec trois
spécifications de la LoRa Alliance (implémentées en version 1.0.0 côté device) :

| Spécification | FPort | Rôle |
|---|---|---|
| TS005 Remote Multicast Setup | 200 | crée le groupe dans le device (McAddr, clé chiffrée avec une clé dérivée de la **GenAppKey**) et programme une session de classe C |
| TS004 Fragmented Data Block Transport | 201 | annonce le fichier (nombre et taille des fragments), transporte les fragments, renvoie l'état de la réception |
| TS003 Clock Synchronization | 202 | synchronise l'horloge du device (heure GPS) pour l'heure de début de session |

Déroulement d'un déploiement ChirpStack, tel qu'on le voit dans le journal et sur la carte du device :

1. au join, le device demande l'heure (`AppTimeReq`) et applique la correction reçue ;
2. `McGroupSetupReq` : le device dérive les clés du groupe de sa GenAppKey (aucune clé saisie) ;
3. `FragSessionSetupReq` : taille du fichier, nombre et taille des fragments ;
4. `McClassCSessionReq` : session multicast programmée (heure GPS, durée, fréquence, DR). Un device
   de **classe A passe temporairement en classe C** pendant la session, puis y revient ;
5. les fragments arrivent en multicast, suivis des **fragments de redondance** (XOR de plusieurs
   fragments) : un device qui a perdu des fragments reconstitue quand même le fichier tant
   qu'il a reçu assez de fragments utiles (élimination de Gauss) ;
6. `FragSessionStatusReq` : le device indique le nombre de fragments reçus et manquants ;
   ChirpStack marque le device comme réussi ou en erreur ;
7. fichier complet : empreinte SHA-256, « redémarrage » simulé et nouvelle version de firmware
   (lue dans un en-tête `LORASIM-FW x.y.z` en début de fichier, sinon début du SHA-256).

Chaque réponse du device part dans un uplink séparé (ChirpStack attend une commande par trame).

**Préparer ChirpStack** (`provision` fait les deux premiers points) :

- device profile, onglet *Application layer* : TS005, TS004 et TS003 en `v1.0.0`, FPorts
  `200`, `201`, `202`. Onglet *General* : *Uplink interval* court (`provision` met 60 s), car
  ChirpStack attend cette durée entre deux étapes et programme la session (tentatives + 1) ×
  uplink interval plus tard ;
- device, onglet *OTAA keys* : *Gen App Key (for Remote Multicast Setup)* = `gen_app_key` ;
- application, onglet *FUOTA* › *New FUOTA deployment* : même device profile, *Multicast group-type*
  `Class-C`, *Multicast frequency (Hz)* `869525000` (obligatoire), *Multicast data-rate* `0`,
  *Fragmentation redundancy (%)* (par ex. 50), *Fragmentation status request* `After session
  timeout`, *Payload* = le fichier ;
- onglet *Devices* de l'application : cocher les devices, *Selected devices › Add to FUOTA
  deployment*, puis *Start deployment* dans le déploiement (onglet des jobs pour suivre les étapes).

Fichier de démonstration :

```bash
{ printf 'LORASIM-FW 1.1.0\n'; head -c 400 /dev/urandom; } > firmware-v1.1.bin
```

Le champ **Perte multicast** de la carte d'un device (`mc_loss`) fait perdre une partie des
fragments : avec 50 % de redondance, la reconstitution réussit presque toujours à 10 % de pertes
et environ une fois sur deux à 30 %. Ordre de grandeur de durée : ChirpStack espace les trames
multicast d'au moins 5 s (`multicast_class_c_margin`), soit environ 2 min pour 15 fragments à
SF12, plus une minute par étape unicast.

## Organisation du code

| Fichier | Contenu |
|---|---|
| `lorasim/crypto.py` | AES, CMAC, MIC, chiffrement FRMPayload, dérivation des clés de session |
| `lorasim/phy.py` | construction / décodage des trames (Join Request/Accept, data up/down) |
| `lorasim/mac.py` | commandes MAC et réponses |
| `lorasim/device.py` | machine à états d'un device (classes A et C, réception multicast) |
| `lorasim/fuota.py` | FUOTA côté device : TS003, TS004 (décodage des fragments de redondance), TS005 |
| `lorasim/gateway.py` | Semtech UDP Packet Forwarder |
| `lorasim/radio.py` | canal radio simulé (RSSI, SNR, sensibilité par SF, pertes) |
| `lorasim/payloads.py` | capteur, relais, afficheur et codec JavaScript |
| `lorasim/provision.py` | API gRPC ChirpStack (devices, groupes multicast, files de downlinks) |
| `lorasim/config.py` | lecture / écriture de `config.yaml` |
| `lorasim/web.py`, `static/index.html` | tableau de bord (aiohttp, Server-Sent Events) |
| `lorasim/events.py` | bus d'événements alimentant le journal web |

## Limites

- Région EU868 uniquement, LoRaWAN 1.0.3, classes A et C (pas de classe B, donc pas de
  session FUOTA de classe B).
- FUOTA : spécifications v1.0.0 uniquement (pas de v2.0.0) ; ABP non provisionné pour le FUOTA.
- Les fenêtres RX ne sont pas vérifiées côté device : un downlink est accepté dès que la
  passerelle l'« émet » (à l'instant `tmst` demandé par le serveur).
