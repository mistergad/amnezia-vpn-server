from __future__ import annotations

import base64
import json
import zlib

from app.config import Settings
from app.services.amnezia_key import build_amnezia_vpn_key


CONFIG = """[Interface]
Address = 10.8.1.9/32
DNS = 1.1.1.1, 1.0.0.1
PrivateKey = client-private
Jc = 4
Jmin = 10
Jmax = 50
S1 = 25
S2 = 35
S3 = 12
S4 = 8
H1 = 100-200
H2 = 300-400
H3 = 500-600
H4 = 700-800
I1 = <r 2><b 0x0102>
HeaderProtectionKey = header-protection-key
ContentPaddingAddition = 10-100
RekeyAfterTime = 100-120
RekeyTimeout = 3-7
RejectAfterTime = 150-180
KeepaliveTimeout = 5-15
MaxHandshakeAttempts = 15-20

[Peer]
PublicKey = server-public
PresharedKey = client-psk
AllowedIPs = 0.0.0.0/0, ::/0
Endpoint = 203.0.113.8:55424
PersistentKeepalive = 25-35
"""


def decode_key(key: str) -> dict[str, object]:
    assert key.startswith("vpn://")
    encoded = key.removeprefix("vpn://")
    packed = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    expected_size = int.from_bytes(packed[:4], "big")
    raw = zlib.decompress(packed[4:])
    assert len(raw) == expected_size
    return json.loads(raw)


def test_builds_guest_only_amnezia_vpn_key() -> None:
    key = build_amnezia_vpn_key(
        config=CONFIG,
        client_public_key="client-public",
        label="Телефон",
        settings=Settings(awg_subnet="10.8.1.0/24"),
    )
    envelope = decode_key(key)

    assert envelope["hostName"] == "203.0.113.8"
    assert envelope["defaultContainer"] == "amnezia-awg2"
    assert "userName" not in envelope
    assert "password" not in envelope
    assert "port" not in envelope

    container = envelope["containers"][0]  # type: ignore[index]
    assert container["container"] == "amnezia-awg2"
    awg = container["awg"]
    assert awg["protocol_version"] == "3"
    assert awg["HeaderProtectionKey"] == "header-protection-key"
    assert awg["ContentPaddingAddition"] == "10-100"
    client = json.loads(awg["last_config"])
    assert client["config"] == CONFIG
    assert client["client_pub_key"] == "client-public"
    assert client["client_priv_key"] == "client-private"
    assert client["I1"] == "<r 2><b 0x0102>"
    assert client["RekeyAfterTime"] == "100-120"
