"""Enregistrement de la passerelle et des devices via l'API gRPC ChirpStack v4."""

import grpc
from chirpstack_api import api, common

from . import payloads
from .log import log

APP_NAME = "Simulateur LoRaWAN"
PROFILE_PREFIX = "Simulateur LoRaWAN"


def profile_name(otaa: bool, class_c: bool) -> str:
    return f"{PROFILE_PREFIX} {'OTAA' if otaa else 'ABP'}" + (" classe C" if class_c else "")


def is_class_c(dev: dict) -> bool:
    return str(dev.get("class", "A")).upper() == "C"


def is_otaa(dev: dict) -> bool:
    return dev.get("activation", "otaa").lower() == "otaa"


def _p(msg, level="info"):
    log("provision", msg, level)


class ChirpStack:
    def __init__(self, server: dict):
        target = f"{server['host']}:{server.get('api_port', 8080)}"
        if server.get("api_tls"):
            channel = grpc.secure_channel(target, grpc.ssl_channel_credentials())
        else:
            channel = grpc.insecure_channel(target)
        token = server.get("api_token") or ""
        if not token:
            raise SystemExit("server.api_token manquant : créez une clé API dans l'interface "
                             "ChirpStack (menu « API keys ») et collez-la dans le fichier de config.")
        self.md = [("authorization", f"Bearer {token}")]
        self.target = target
        self.tenants = api.TenantServiceStub(channel)
        self.gateways = api.GatewayServiceStub(channel)
        self.profiles = api.DeviceProfileServiceStub(channel)
        self.apps = api.ApplicationServiceStub(channel)
        self.devices = api.DeviceServiceStub(channel)

    def call(self, fn, req):
        return fn(req, metadata=self.md, timeout=10)

    def exists(self, fn, req) -> bool:
        try:
            self.call(fn, req)
            return True
        except grpc.RpcError as e:
            if e.code() == grpc.StatusCode.NOT_FOUND:
                return False
            raise

    def tenant_id(self, wanted: str | None) -> str:
        if wanted:
            return wanted
        resp = self.call(self.tenants.List, api.ListTenantsRequest(limit=10))
        if not resp.result:
            raise SystemExit("aucun tenant visible avec cette clé API")
        t = resp.result[0]
        _p(f"tenant : {t.name} ({t.id})")
        return t.id

    def ensure_gateway(self, tenant_id: str, gw: dict):
        g = api.Gateway(
            gateway_id=gw["id"].lower(), name=gw.get("name", gw["id"]),
            description="Passerelle simulée (lorawan-sim)",
            tenant_id=tenant_id, stats_interval=30,
        )
        if "lat" in gw and "lon" in gw:
            g.location.CopyFrom(common.Location(
                latitude=gw["lat"], longitude=gw["lon"], altitude=gw.get("alt", 0)))
        if self.exists(self.gateways.Get, api.GetGatewayRequest(gateway_id=g.gateway_id)):
            self.call(self.gateways.Update, api.UpdateGatewayRequest(gateway=g))
            _p(f"passerelle {g.gateway_id} déjà présente (mise à jour)")
        else:
            self.call(self.gateways.Create, api.CreateGatewayRequest(gateway=g))
            _p(f"passerelle {g.gateway_id} créée", "ok")

    def ensure_profile(self, tenant_id: str, otaa: bool, class_c: bool, region_config_id: str) -> str:
        name = profile_name(otaa, class_c)
        dp = api.DeviceProfile(
            tenant_id=tenant_id, name=name,
            description="Profil généré par lorawan-sim",
            region=common.Region.EU868, region_config_id=region_config_id,
            mac_version=common.MacVersion.LORAWAN_1_0_3,
            reg_params_revision=common.RegParamsRevision.A,
            adr_algorithm_id="default",
            payload_codec_runtime=api.CodecRuntime.JS,
            payload_codec_script=payloads.SENSOR_CODEC_JS,
            uplink_interval=3600, device_status_req_interval=1,
            supports_otaa=otaa,
            supports_class_c=class_c, class_c_timeout=5,
        )
        if not otaa:
            dp.abp_rx1_delay = 1
            dp.abp_rx1_dr_offset = 0
            dp.abp_rx2_dr = 0
            dp.abp_rx2_freq = 869525000
        resp = self.call(self.profiles.List, api.ListDeviceProfilesRequest(
            tenant_id=tenant_id, search=name, limit=50))
        for item in resp.result:
            if item.name == name:
                dp.id = item.id
                self.call(self.profiles.Update, api.UpdateDeviceProfileRequest(device_profile=dp))
                _p(f"device profile « {name} » mis à jour")
                return item.id
        r = self.call(self.profiles.Create, api.CreateDeviceProfileRequest(device_profile=dp))
        _p(f"device profile « {name} » créé", "ok")
        return r.id

    def ensure_application(self, tenant_id: str) -> str:
        resp = self.call(self.apps.List, api.ListApplicationsRequest(
            tenant_id=tenant_id, search=APP_NAME, limit=50))
        for item in resp.result:
            if item.name == APP_NAME:
                _p(f"application « {APP_NAME} » déjà présente")
                return item.id
        r = self.call(self.apps.Create, api.CreateApplicationRequest(application=api.Application(
            tenant_id=tenant_id, name=APP_NAME, description="Devices simulés (lorawan-sim)")))
        _p(f"application « {APP_NAME} » créée", "ok")
        return r.id

    def ensure_device(self, app_id: str, profile_id: str, dev: dict, fcnt_up: int):
        eui = dev["dev_eui"].lower()
        d = api.Device(
            dev_eui=eui, name=dev["name"], application_id=app_id,
            device_profile_id=profile_id, join_eui=dev.get("join_eui", "0000000000000000"),
            description="Device simulé (lorawan-sim)",
        )
        if self.exists(self.devices.Get, api.GetDeviceRequest(dev_eui=eui)):
            self.call(self.devices.Update, api.UpdateDeviceRequest(device=d))
            _p(f"device {dev['name']} ({eui}) déjà présent (mis à jour)")
        else:
            self.call(self.devices.Create, api.CreateDeviceRequest(device=d))
            _p(f"device {dev['name']} ({eui}) créé", "ok")

        if is_otaa(dev):
            # LoRaWAN 1.0.x : ChirpStack attend l'AppKey dans le champ nwk_key
            keys = api.DeviceKeys(dev_eui=eui, nwk_key=dev["app_key"], app_key=dev["app_key"])
            if self.exists(self.devices.GetKeys, api.GetDeviceKeysRequest(dev_eui=eui)):
                self.call(self.devices.UpdateKeys, api.UpdateDeviceKeysRequest(device_keys=keys))
            else:
                self.call(self.devices.CreateKeys, api.CreateDeviceKeysRequest(device_keys=keys))
        else:
            act = api.DeviceActivation(
                dev_eui=eui, dev_addr=dev["dev_addr"],
                app_s_key=dev["app_s_key"], nwk_s_enc_key=dev["nwk_s_key"],
                s_nwk_s_int_key=dev["nwk_s_key"], f_nwk_s_int_key=dev["nwk_s_key"],
                f_cnt_up=fcnt_up, n_f_cnt_down=0, a_f_cnt_down=0,
            )
            self.call(self.devices.Activate, api.ActivateDeviceRequest(device_activation=act))
            _p(f"device {dev['name']} activé en ABP (DevAddr {dev['dev_addr']})")

    def flush_dev_nonces(self, dev_eui: str):
        self.call(self.devices.FlushDevNonces, api.FlushDevNoncesRequest(dev_eui=dev_eui.lower()))


def provision(cfg: dict, state):
    server = cfg["server"]
    cs = ChirpStack(server)
    _p(f"connexion à l'API gRPC {cs.target}")
    try:
        tenant = cs.tenant_id(server.get("tenant_id"))
        for gw in cfg["gateways"]:
            cs.ensure_gateway(tenant, gw)
        devices = cfg.get("devices", [])
        if not devices:
            return
        app_id = cs.ensure_application(tenant)
        region_id = server.get("region_config_id", "")
        profiles = {}
        for dev in devices:
            key = (is_otaa(dev), is_class_c(dev))
            if key not in profiles:
                profiles[key] = cs.ensure_profile(tenant, *key, region_id)
            fcnt = state.device(dev["dev_eui"].lower())["fcnt_up"]
            cs.ensure_device(app_id, profiles[key], dev, fcnt)
        state.save()
        _p("provisionnement terminé", "ok")
    except grpc.RpcError as e:
        raise SystemExit(f"erreur API ChirpStack : {e.code().name} - {e.details()}")


def enqueue(cs: ChirpStack, dev_eui: str, fport: int, data: bytes, confirmed: bool) -> str:
    """Met un downlink en file d'attente. Lève grpc.RpcError en cas d'échec."""
    if not 1 <= fport <= 223:
        raise ValueError("FPort doit être compris entre 1 et 223")
    item = api.DeviceQueueItem(dev_eui=dev_eui.lower(), f_port=fport, data=data, confirmed=confirmed)
    return cs.call(cs.devices.Enqueue, api.EnqueueDeviceQueueItemRequest(queue_item=item)).id


def reset(cfg: dict, state):
    """Oublie les DevNonce côté serveur et les compteurs locaux."""
    cs = ChirpStack(cfg["server"])
    for dev in cfg.get("devices", []):
        eui = dev["dev_eui"].lower()
        if is_otaa(dev):
            try:
                cs.flush_dev_nonces(eui)
                _p(f"DevNonces de {dev['name']} effacés sur le serveur")
            except grpc.RpcError as e:
                _p(f"{dev['name']} : {e.code().name} - {e.details()}", "warn")
        state.reset(eui)
    state.save()
    _p("état local réinitialisé", "ok")
