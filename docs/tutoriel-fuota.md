# Tutoriel : mise à jour de firmware par radio (FUOTA), de zéro

Ce tutoriel part d'une machine vide et va jusqu'à la mise à jour du firmware de devices
LoRaWAN simulés par un déploiement FUOTA de ChirpStack. Tout est configuré à la main, comme
avec du vrai matériel : la passerelle, l'application, le device profile et les devices.

Durée : environ 45 min la première fois, dont 10 à 15 min pour le déploiement lui-même.

```
 devices simulés ──▶ passerelle simulée ──UDP 1700──▶ Gateway Bridge ──MQTT──▶ ChirpStack
 (lorawan-sim, tableau de bord :8000)                  (chirpstack-docker, interface :8080)
```

## Sommaire

1. [Prérequis](#1-prérequis)
2. [Installer ChirpStack](#2-installer-chirpstack)
3. [Installer le simulateur](#3-installer-le-simulateur)
4. [Déclarer la passerelle](#4-déclarer-la-passerelle)
5. [Créer l'application et le device profile](#5-créer-lapplication-et-le-device-profile)
6. [Créer les devices](#6-créer-les-devices)
7. [Préparer le firmware](#7-préparer-le-firmware)
8. [Créer et lancer le déploiement FUOTA](#8-créer-et-lancer-le-déploiement-fuota)
9. [Suivre et comprendre le déroulement](#9-suivre-et-comprendre-le-déroulement)
10. [En cas de problème](#10-en-cas-de-problème)

---

## 1. Prérequis

- **Docker** avec Docker Compose : Docker Desktop ou OrbStack sur Mac, Docker Engine sous Linux.
- **git**.
- Un navigateur.

Dans ce tutoriel, ChirpStack et le simulateur tournent sur **la même machine**. Le cas de deux
machines est traité à l'étape 4.

## 2. Installer ChirpStack

```bash
git clone https://github.com/chirpstack/chirpstack-docker.git
cd chirpstack-docker
docker compose up -d
```

Ouvrez http://localhost:8080 et connectez-vous avec `admin` / `admin`. Le tenant
« ChirpStack » existe déjà.

Ce dépôt est déjà configuré pour l'Europe : le Gateway Bridge écoute en UDP sur le port
**1700** et publie sur les topics `eu868/…`.

> Si `docker compose up` échoue parce que le port 8090 est occupé, mettez en commentaire le
> service `chirpstack-rest-api` dans `docker-compose.yml`. Le simulateur n'en a pas besoin.

## 3. Installer le simulateur

Dans un autre dossier :

```bash
git clone https://github.com/bouhenic/lorawan-sim.git
cd lorawan-sim
docker compose up -d --build
```

Ouvrez le tableau de bord : http://localhost:8000.

Au premier démarrage, le simulateur crée `data/config.yaml` avec **une passerelle et aucun
device**. La passerelle vise `host.docker.internal:1700`, c'est-à-dire le ChirpStack de la
machine hôte.

Pour mettre à jour le simulateur plus tard : `git pull && docker compose up -d --build`.
Ne supprimez pas `data/`, qui contient la configuration et les compteurs des devices.

## 4. Déclarer la passerelle

**Dans le tableau de bord**, sur la carte de la passerelle, copiez son EUI avec le bouton
*copier*.

**Dans ChirpStack**, ouvrez *Gateways › Add gateway* et remplissez :

| Champ | Valeur |
|---|---|
| Name | `passerelle-sim` |
| Gateway ID (EUI64) | l'EUI copié |

Validez avec *Submit*. Au bout de quelques secondes, la passerelle passe « en ligne » dans
ChirpStack et « connectée » dans le tableau de bord.

> **ChirpStack sur une autre machine** : sur la carte de la passerelle, ouvrez *Configuration du
> packet forwarder* et saisissez l'adresse IP de cette machine, avec le port `1700`. C'est
> l'équivalent de `serv_address` / `serv_port_up` dans le `global_conf.json` d'une vraie passerelle.

## 5. Créer l'application et le device profile

### 5.1 Application

Ouvrez *Applications › Add application*, nommez-la `fuota-demo`, puis validez avec *Submit*.

### 5.2 Device profile

Ouvrez *Device profiles › Add device profile*. Chaque onglet se remplit avant de valider.

**Onglet General**

| Champ | Valeur |
|---|---|
| Name | `capteur-fuota` |
| Region | `EU868` |
| MAC version | `LoRaWAN 1.0.3` |
| Regional parameters revision | `A` |
| ADR algorithm | valeur par défaut |
| Expected uplink interval (secs) | **`60`** |

L'*Expected uplink interval* a un rôle important : ChirpStack attend cette durée entre deux
étapes du déploiement. Avec la valeur par défaut de 3600 s, chaque étape prendrait une heure.

**Onglet Join (OTAA / ABP)** : cochez *Device supports OTAA*.

**Onglet Class-C** : laissez *Device supports Class-C* décoché. Les devices sont de classe A et
ne passeront en classe C que le temps de la mise à jour, à la demande du serveur.

**Onglet Codec**, facultatif : pour que ChirpStack affiche la température et l'humidité, choisissez
*JavaScript functions* et collez le codec. Le tableau de bord le fournit : cliquez sur
*+ Ajouter un device*, puis ouvrez *Codec JavaScript capteur / relais / afficheur* et cliquez
sur *copier*.

**Onglet Application layer** : c'est cet onglet qui active le FUOTA.

| Champ | Valeur |
|---|---|
| Clock sync version (TS003) | `v1.0.0` |
| Clock sync fPort (TS003) | `202` |
| Fragmented data block transport (TS004) | `v1.0.0` |
| Fragmented data block transport fPort (TS004) | `201` |
| Remote multicast setup version (TS005) | `v1.0.0` |
| Remote multicast setup fPort (TS005) | `200` |

Prenez bien `v1.0.0` : le simulateur n'implémente pas la v2.0.0. Validez avec *Submit*.

## 6. Créer les devices

Créez trois devices pour comparer : un sans perte, un avec 20 % de perte, un avec 60 %.

### 6.1 Dans le simulateur

Cliquez sur **+ Ajouter un device** et remplissez :

| Champ | Valeur |
|---|---|
| Nom | `capteur-1` (puis `capteur-2`, `capteur-3`) |
| Activation | OTAA (join) |
| DevEUI, AppKey, **GenAppKey** | générés automatiquement (bouton ↻ pour en tirer d'autres) |
| Classe | A (écoute après chaque uplink) |
| Intervalle d'émission (s) | `30` |
| Payload | Capteur temp./humidité (FPort 2) |

Laissez *allumer tout de suite* décoché, puis cliquez sur *Créer le device*. Le device est
créé **éteint**.

La **GenAppKey** est la clé propre au FUOTA. Le device s'en sert pour calculer les clés du
groupe multicast que le serveur lui enverra. Sans elle, pas de FUOTA.

### 6.2 Dans ChirpStack

Sur la carte du device, ouvrez **Identifiants à saisir dans ChirpStack**. Chaque valeur y a son
bouton *copier*.

Ouvrez *Applications › fuota-demo › Add device* et remplissez :

| Champ | Valeur |
|---|---|
| Name | `capteur-1` |
| Device EUI (EUI64) | DevEUI du simulateur |
| Join EUI (EUI64) | `0000000000000000` |
| Device profile | `capteur-fuota` |

Validez avec *Submit*. ChirpStack ouvre alors l'onglet **OTAA keys** :

| Champ | Valeur |
|---|---|
| Application key | AppKey du simulateur |
| Gen App Key (for Remote Multicast Setup) | GenAppKey du simulateur |

Validez, puis recommencez pour `capteur-2` et `capteur-3`.

### 6.3 Allumer les devices

Dans le tableau de bord, cliquez sur **Allumer** sur chaque carte. Le journal montre alors :
- la Join Request ;
- le Join Accept ;
- `AppTimeReq`, la première trame FUOTA : le device demande l'heure au serveur ;
- la réponse avec la correction d'horloge.

Dans ChirpStack, l'onglet *LoRaWAN frames* du device montre les mêmes trames vues du serveur.

### 6.4 Régler les pertes

Chaque carte de device comporte un bloc **FUOTA**. Le firmware de départ y est indiqué : `1.0.0`.
Réglez **Perte multicast** puis cliquez sur *Appliquer* :

| Device | Perte multicast |
|---|---|
| capteur-1 | 0 % |
| capteur-2 | 20 % |
| capteur-3 | 60 % |

Ce réglage simule la perte des trames multicast, c'est-à-dire des fragments du firmware.

## 7. Préparer le firmware

Sur la machine qui a le navigateur :

```bash
{ printf 'LORASIM-FW 1.1.0\n'; head -c 400 /dev/urandom; } > firmware-v1.1.bin
```

Le fichier contient un en-tête, puis 400 octets aléatoires qui tiennent lieu de code. Le
simulateur lit le numéro de version dans l'en-tête : `LORASIM-FW 1.1.0` suivi d'un retour à la
ligne. Sans cet en-tête, il affiche le début de l'empreinte SHA-256 du fichier.

Gardez un fichier petit : à SF12, un fragment ne transporte que 48 octets. Ce fichier de
417 octets fait 9 fragments.

## 8. Créer et lancer le déploiement FUOTA

### 8.1 Créer le déploiement

Ouvrez *Applications › fuota-demo › onglet FUOTA › New FUOTA deployment* et remplissez :

| Champ | Valeur | Explication |
|---|---|---|
| Name | `maj-1.1.0` | |
| Device profile | `capteur-fuota` | |
| Unicast retry count (max) | valeur par défaut | nouvelles tentatives des commandes unicast |
| Multicast group-type | `Class-C` | la classe B n'est pas simulée |
| Class-C scheduling type | `Delay` | |
| Multicast data-rate | `0` | SF12, la portée maximale |
| Multicast frequency (Hz) | **`869525000`** | **obligatoire** : laissé vide, ChirpStack émet sur 0 Hz et rien n'est reçu |
| Fragmentation redundancy (%) | `50` | 50 % de fragments de redondance en plus |
| Fragmentation status request | `After session timeout` | |
| Calculate multicast-timeout | décoché | |
| Multicast timeout | `256 seconds` | durée de la session de classe C |
| Calculate fragment size | décoché | |
| Fragment size | `48` | 51 octets max à SF12, moins 3 octets d'en-tête |
| Payload | `firmware-v1.1.bin` (*Click to upload*) | |

Validez avec *Submit*.

### 8.2 Ajouter les devices

Ouvrez *Applications › fuota-demo › onglet Devices*. Cochez les trois devices, puis choisissez
*Selected devices › **Add to FUOTA deployment*** et sélectionnez `maj-1.1.0`.

Les passerelles ne sont pas obligatoires : ChirpStack émet par celles qui entendent les devices.

### 8.3 Lancer

Ouvrez le déploiement et cliquez sur **Start deployment**.

## 9. Suivre et comprendre le déroulement

Gardez deux fenêtres côte à côte :
- dans ChirpStack, le déploiement et son tableau des jobs ;
- dans le simulateur, les blocs FUOTA et le journal.

| # | Étape (job ChirpStack) | Ce qui se passe | Dans le simulateur |
|---|---|---|---|
| 1 | Multicast group setup (TS005, port 200) | Le serveur crée un groupe multicast **dans** chaque device : adresse, clé chiffrée. Le device déchiffre la clé avec une clé dérivée de sa GenAppKey. | `McGroupSetupReq` / `Ans` |
| 2 | Fragmentation session setup (TS004, port 201) | Le serveur annonce le fichier : nombre de fragments, taille, redondance. | `FragSessionSetupReq` / `Ans` |
| 3 | Multicast session setup (TS005) | Le serveur programme une session à une heure GPS précise, d'où l'intérêt de la synchronisation d'horloge (TS003). | `McClassCSessionReq` : session programmée dans *n* s |
| 4 | Enqueue fragments | À l'heure prévue, les devices passent **en classe C** et le serveur émet les fragments **une seule fois** pour tous, en multicast, au moins 5 s d'intervalle. | barre de progression, « fragments reçus / manquants » |
| 5 | Fragmentation status (TS004) | Après la session, chaque device revient en classe A et indique ce qu'il a reçu. | `FragSessionStatusAns` |
| 6 | Fin | Le device vérifie le fichier (SHA-256) et « redémarre » sur le nouveau firmware. | Firmware **1.1.0** |

Durée indicative avec ces réglages : environ 1 min par étape unicast, puis environ 2 min de
fragments, puis la fin du timeout de 256 s, soit 10 à 15 min en tout.

### Résultat attendu

| Device | Perte | Résultat |
|---|---|---|
| capteur-1 | 0 % | ✓ firmware 1.1.0 |
| capteur-2 | 20 % | généralement ✓, parfois ✗ : il manque 1 ou 2 fragments |
| capteur-3 | 60 % | ✗, ChirpStack signale `missing_frag` |

### Points à discuter avec les élèves

- **Pourquoi le multicast ?** Une trame pour tous les devices au lieu d'une par device : à SF12,
  chaque trame occupe le canal environ 2,5 s, et le temps d'émission est limité (duty cycle).
- **Pourquoi la redondance ?** Il n'y a pas d'accusé de réception en multicast. Les fragments
  de redondance sont des combinaisons (XOR) de plusieurs fragments : ils remplacent n'importe
  quel fragment perdu.
- **Pourquoi 9 fragments reçus ne suffisent-ils pas toujours ?** Il faut 9 fragments **utiles**,
  c'est-à-dire indépendants. Un fragment de redondance qui combine des fragments déjà reçus
  n'apporte rien.
- **Pourquoi synchroniser l'horloge ?** Tous les devices doivent passer en classe C au même
  moment, à la même heure GPS.
- **Pourquoi la GenAppKey ?** Le serveur ne transmet jamais la clé du groupe en clair : elle est
  chiffrée avec une clé que seul le device sait calculer.
- **Pourquoi repasser en classe A ?** En classe C, le récepteur est allumé en permanence, ce qui
  vide une pile en quelques jours.

## 10. En cas de problème

| Symptôme | Cause probable |
|---|---|
| Passerelle « hors ligne » | Mauvaise adresse dans *Configuration du packet forwarder*, port UDP 1700 bloqué, ou EUI différent dans ChirpStack. |
| Join qui échoue en boucle | DevEUI ou AppKey mal recopiée, ou device absent de ChirpStack. |
| Aucune trame sur les ports 200 à 202 | *Application layer* non renseigné dans le device profile, ou le device n'utilise pas ce profil. |
| Journal « commande FUOTA ignorée, pas de GenAppKey » | Device créé avec une ancienne version du simulateur : recréez-le. |
| `McGroupSetupAns` en erreur, ou MIC invalide pendant la session | La *Gen App Key* de ChirpStack diffère de celle du simulateur. |
| Les étapes durent une heure | *Expected uplink interval (secs)* du device profile resté à 3600. |
| Aucun fragment reçu, journal « émis sur 0 Hz » | *Multicast frequency (Hz)* vide dans le déploiement. |
| Journal « émis sur … MHz … mais le device écoute sur … : non reçu » | Fréquence ou data rate des fragments différents de ceux de la session annoncée au device. |
| Échec malgré 0 % de perte | Device éteint ou redémarré pendant la session : relancez un déploiement. |

### Raccourci pour l'enseignant

Pour préparer une démo sans tout saisir à la main, utilisez `provision`. Il faut d'abord créer
une clé dans *API keys › Add API key* et la coller dans `data/config.yaml` (`server.api_token`).

```bash
docker compose run --rm lorasim provision
```

Cette commande enregistre la passerelle, l'application, le device profile (codec, *Application
layer* et uplink interval de 60 s compris) et tous les devices de la configuration avec leurs clés.
Il reste à créer le déploiement (étape 8).
