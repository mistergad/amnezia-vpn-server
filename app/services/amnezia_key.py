from __future__ import annotations

import base64
import configparser
import ipaddress
import json
import zlib

from app.config import Settings


AWG_PARAMETER_NAMES = (
    "Jc",
    "Jmin",
    "Jmax",
    "S1",
    "S2",
    "S3",
    "S4",
    "H1",
    "H2",
    "H3",
    "H4",
    "I1",
    "I2",
    "I3",
    "I4",
    "I5",
    "HeaderProtectionKey",
    "ContentPaddingAddition",
    "RekeyAfterTime",
    "RekeyTimeout",
    "RejectAfterTime",
    "KeepaliveTimeout",
    "MaxHandshakeAttempts",
)


def _split_endpoint(value: str) -> tuple[str, int]:
    value = value.strip()
    if value.startswith("["):
        host, separator, port = value[1:].partition("]:")
        if not separator:
            raise ValueError("Invalid IPv6 endpoint")
    else:
        host, separator, port = value.rpartition(":")
        if not separator:
            raise ValueError("Endpoint port is missing")
    parsed_port = int(port)
    if not host or not 1 <= parsed_port <= 65535:
        raise ValueError("Invalid endpoint")
    return host, parsed_port


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def build_amnezia_vpn_key(
    *,
    config: str,
    client_public_key: str,
    label: str,
    settings: Settings,
) -> str:
    """Build a guest-only vpn:// key accepted by the AmneziaVPN client.

    The envelope intentionally contains no SSH user, password, or management
    port. Its only credential is the already-issued AWG3 client configuration.
    """

    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    parser.read_string(config)
    interface = parser["Interface"]
    peer = parser["Peer"]

    host, port = _split_endpoint(peer["Endpoint"])
    client_address = ipaddress.ip_interface(_csv(interface["Address"])[0])
    dns_servers = _csv(interface.get("DNS", settings.awg_dns))
    allowed_ips = _csv(peer.get("AllowedIPs", "0.0.0.0/0, ::/0"))
    network = ipaddress.ip_network(settings.awg_subnet, strict=False)

    parameters = {
        name: interface.get(name, "").strip()
        for name in AWG_PARAMETER_NAMES
        if interface.get(name, "").strip()
    }
    client_config: dict[str, object] = {
        "config": config,
        "hostName": host,
        "port": port,
        "client_ip": str(client_address.ip),
        "client_priv_key": interface["PrivateKey"].strip(),
        "client_pub_key": client_public_key,
        "server_pub_key": peer["PublicKey"].strip(),
        "psk_key": peer.get("PresharedKey", "").strip(),
        "clientId": client_public_key,
        "allowed_ips": allowed_ips,
        "persistent_keep_alive": peer.get("PersistentKeepalive", "25-35").strip(),
        "isObfuscationEnabled": bool(parameters),
        **parameters,
    }
    mtu = interface.get("MTU", "").strip()
    if mtu:
        client_config["mtu"] = mtu

    server_config: dict[str, object] = {
        "port": str(port),
        "transport_proto": "udp",
        "protocol_version": "3",
        "subnet_address": str(network.network_address),
        "subnet_cidr": str(network.prefixlen),
        **parameters,
    }
    for name in ("I1", "I2", "I3", "I4", "I5"):
        server_config.setdefault(name, "")
    server_config["last_config"] = json.dumps(
        client_config, ensure_ascii=False, separators=(",", ":")
    )

    envelope = {
        "description": label,
        "hostName": host,
        "containers": [
            {
                # AmneziaVPN keeps this historical container identifier for
                # the userspace AWG implementation, including protocol v3.
                "container": "amnezia-awg2",
                "awg": server_config,
            }
        ],
        "defaultContainer": "amnezia-awg2",
        "dns1": dns_servers[0] if dns_servers else "1.1.1.1",
        "dns2": dns_servers[1] if len(dns_servers) > 1 else "",
    }
    raw = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode()

    # Qt qCompress: four-byte big-endian uncompressed size + zlib stream.
    compressed = len(raw).to_bytes(4, "big") + zlib.compress(raw, level=8)
    encoded = base64.urlsafe_b64encode(compressed).rstrip(b"=").decode()
    return f"vpn://{encoded}"
