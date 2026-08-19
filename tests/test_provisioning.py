import base64
from datetime import timezone

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from app.config import Settings
from app.services.provisioning import MockProvisioner, NativeAmneziaWGProvisioner


def test_mock_provisioner_issues_importable_config() -> None:
    provisioner = MockProvisioner(
        Settings(awg_endpoint="203.0.113.4:51820", awg_dns="1.1.1.1")
    )
    issued = provisioner.provision("10.8.1.2")
    assert "[Interface]" in issued.config
    assert "Address = 10.8.1.2/32" in issued.config
    assert "Jc = 4" in issued.config
    assert "[Peer]" in issued.config
    assert "Endpoint = 203.0.113.4:51820" in issued.config
    assert issued.public_key in provisioner.stats()
    assert provisioner.assigned_ips() == {"10.8.1.2"}
    provisioner.revoke(issued.public_key)
    assert issued.public_key not in provisioner.stats()
    assert provisioner.assigned_ips() == set()
    provisioner.restore(issued.public_key, "10.8.1.2", issued.config)
    assert issued.public_key in provisioner.stats()
    assert provisioner.assigned_ips() == {"10.8.1.2"}


def test_native_provisioner_reads_live_amnezia_parameters(tmp_path) -> None:
    class FakeNativeProvisioner(NativeAmneziaWGProvisioner):
        def __init__(self, settings: Settings):
            super().__init__(settings)
            self.calls: list[tuple[list[str], str | None, str | None]] = []

        @staticmethod
        def _generate_key_material() -> tuple[str, str, str]:
            return "client-private", "client-public", "client-psk"

        def _run(self, args, *, input_text=None, binary=None):  # type: ignore[no-untyped-def]
            self.calls.append((args, input_text, binary))
            if args[-1:] == ["public-key"]:
                return "server-public"
            if args[:1] == ["showconf"]:
                return """[Interface]
Jc = 4
Jmin = 40
Jmax = 70
S1 = 10
S2 = 20
H1 = 1
H2 = 2
H3 = 3
H4 = 4
"""
            return ""

    settings = Settings(
        vpn_backend="native",
        awg_config_path=tmp_path / "not-mounted.conf",
        awg_endpoint="vpn.example.test:443",
        awg_save_config=False,
        awg_i1="<r 2><b 0x0102>",
        awg_rate_limit_enabled=True,
        awg_download_limit_mbps=10,
        awg_upload_limit_mbps=8,
    )
    provisioner = FakeNativeProvisioner(settings)
    assert provisioner.assigned_ips() == set()
    issued = provisioner.provision("10.8.1.9")
    assert issued.public_key == "client-public"
    assert "PrivateKey = client-private" in issued.config
    assert "Jmin = 40" in issued.config
    assert "I1 = <r 2><b 0x0102>" in issued.config
    assert "PublicKey = server-public" in issued.config
    apply_call = next(
        call
        for call in provisioner.calls
        if call[0][:1] == ["apply"]
        and call[2] == "/opt/amnezia/peer-manager.sh"
    )
    assert apply_call[0] == [
        "apply",
        "awg0",
        "client-public",
        "10.8.1.9",
        "9",
        "true",
        "10",
        "8",
        "false",
        settings.awg_config_path.as_posix(),
    ]
    assert apply_call[1] == "client-psk\n"
    assert not any(
        call[0] in (["genkey"], ["pubkey"], ["genpsk"])
        for call in provisioner.calls
    )
    assert not any(call[0][:1] == ["set"] for call in provisioner.calls)
    assert not any(
        call[2] in (settings.awg_quick_binary, settings.awg_rate_limit_binary)
        for call in provisioner.calls
    )
    assert provisioner.assigned_ips() == {"10.8.1.9"}

    # AWG2 interface metadata is static and should be fetched only once for
    # multiple key releases.
    second = provisioner.provision("10.8.1.10")
    assert "Jmin = 40" in second.config
    assert "HeaderProtectionKey" not in second.config
    assert sum(call[0][-1:] == ["public-key"] for call in provisioner.calls) == 1
    assert sum(call[0][:1] == ["showconf"] for call in provisioner.calls) == 1
    assert provisioner.assigned_ips() == {"10.8.1.9", "10.8.1.10"}

    provisioner.restore(issued.public_key, "10.8.1.9", issued.config)
    restore_call = [
        call for call in provisioner.calls
        if call[0][:3] == ["apply", "awg0", "client-public"]
    ][-1]
    assert restore_call[0][3:5] == ["10.8.1.9", "9"]
    assert restore_call[1] == "client-psk\n"

    provisioner.restore_many(
        [
            ("peer-public-a", "10.8.1.11", issued.config),
            ("peer-public-b", "10.8.1.12", issued.config),
        ]
    )
    batch_call = provisioner.calls[-1]
    assert batch_call[0] == [
        "apply-many",
        "awg0",
        "true",
        "10",
        "8",
        "false",
        settings.awg_config_path.as_posix(),
    ]
    assert batch_call[1] == (
        "peer-public-a\t10.8.1.11\t11\tclient-psk\n"
        "peer-public-b\t10.8.1.12\t12\tclient-psk\n"
    )
    assert batch_call[2] == "/opt/amnezia/peer-manager.sh"

    provisioner.revoke(issued.public_key, "10.8.1.9")
    assert provisioner.calls[-1] == (
        [
            "remove",
            "awg0",
            "client-public",
            "9",
            "true",
            "false",
            settings.awg_config_path.as_posix(),
        ],
        None,
        "/opt/amnezia/peer-manager.sh",
    )
    assert provisioner.assigned_ips() == {
        "10.8.1.10",
        "10.8.1.11",
        "10.8.1.12",
    }


def test_native_local_key_generation_is_wireguard_compatible() -> None:
    private_key, public_key, preshared_key = (
        NativeAmneziaWGProvisioner._generate_key_material()
    )
    private_bytes = base64.b64decode(private_key)
    public_bytes = base64.b64decode(public_key)

    assert len(private_bytes) == 32
    assert len(public_bytes) == 32
    assert len(base64.b64decode(preshared_key)) == 32
    assert private_bytes[0] & 0b111 == 0
    assert private_bytes[31] & 0b10000000 == 0
    assert private_bytes[31] & 0b01000000 != 0
    derived_public = (
        X25519PrivateKey.from_private_bytes(private_bytes)
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    )
    assert public_bytes == derived_public


def test_native_stats_parser() -> None:
    class StatsProvisioner(NativeAmneziaWGProvisioner):
        def _run(self, args, *, input_text=None, binary=None):  # type: ignore[no-untyped-def]
            return (
                "server-private\tserver-public\t51820\toff\n"
                "peer-public\tpsk\t198.51.100.3:1234\t10.8.1.2/32\t"
                "1700000000\t1024\t2048\t25\n"
            )

    stats = StatsProvisioner(Settings()).stats()["peer-public"]
    assert stats.last_handshake_at is not None
    assert stats.last_handshake_at.tzinfo == timezone.utc
    assert stats.rx_bytes == 1024
    assert stats.tx_bytes == 2048


def test_native_assigned_ips_parser() -> None:
    class AssignedProvisioner(NativeAmneziaWGProvisioner):
        def _run(self, args, *, input_text=None, binary=None):  # type: ignore[no-untyped-def]
            return (
                "peer-one\t10.8.1.2/32\n"
                "peer-two\t10.8.1.8/32, 0.0.0.0/0\n"
                "peer-three\t(none)\n"
            )

    class CachedAssignedProvisioner(AssignedProvisioner):
        def __init__(self, settings: Settings):
            super().__init__(settings)
            self.reads = 0

        def _run(self, args, *, input_text=None, binary=None):  # type: ignore[no-untyped-def]
            self.reads += 1
            return super()._run(args, input_text=input_text, binary=binary)

    provisioner = CachedAssignedProvisioner(Settings())
    assert provisioner.assigned_ips() == {"10.8.1.2", "10.8.1.8"}
    assert provisioner.assigned_ips() == {"10.8.1.2", "10.8.1.8"}
    assert provisioner.reads == 1
