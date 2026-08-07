from datetime import timezone

import pytest

from app.config import Settings
from app.services.provisioning import (
    MockProvisioner,
    NativeAmneziaWGProvisioner,
    ProvisioningError,
)


def test_mock_provisioner_issues_importable_config() -> None:
    provisioner = MockProvisioner(
        Settings(awg_endpoint="203.0.113.4:51820", awg_dns="1.1.1.1")
    )
    issued = provisioner.provision("10.8.1.2")
    assert "[Interface]" in issued.config
    assert "Address = 10.8.1.2/32" in issued.config
    assert "Jc = 4" in issued.config
    assert "HeaderProtectionKey = " in issued.config
    assert "ContentPaddingAddition = 10-100" in issued.config
    assert "PersistentKeepalive = 25-35" in issued.config
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

        def _run(self, args, *, input_text=None, binary=None):  # type: ignore[no-untyped-def]
            self.calls.append((args, input_text, binary))
            if args == ["genkey"]:
                return "client-private"
            if args == ["pubkey"]:
                return "client-public"
            if args == ["genpsk"]:
                return "client-psk"
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
S3 = 30
S4 = 12
HeaderProtectionKey = header-protection-key
ContentPaddingAddition = 10-100
RekeyAfterTime = 100-120
RekeyTimeout = 3-7
RejectAfterTime = 150-180
KeepaliveTimeout = 5-15
MaxHandshakeAttempts = 15-20
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
    issued = provisioner.provision("10.8.1.9")
    assert issued.public_key == "client-public"
    assert "PrivateKey = client-private" in issued.config
    assert "Jmin = 40" in issued.config
    assert "I1 = <r 2><b 0x0102>" in issued.config
    assert "HeaderProtectionKey = header-protection-key" in issued.config
    assert "PersistentKeepalive = 25-35" in issued.config
    assert "PublicKey = server-public" in issued.config
    set_call = next(call for call in provisioner.calls if call[0][:1] == ["set"])
    assert "/dev/stdin" in set_call[0]
    assert set_call[1] == "client-psk\n"
    rate_call = next(
        call
        for call in provisioner.calls
        if call[2] == "/opt/amnezia/traffic-limit.sh"
    )
    assert rate_call[0] == ["apply", "awg0", "10.8.1.9", "9", "10", "8"]

    provisioner.restore(issued.public_key, "10.8.1.9", issued.config)
    restore_call = [
        call for call in provisioner.calls
        if call[0][:4] == ["set", "awg0", "peer", "client-public"]
    ][-1]
    assert restore_call[0][-2:] == ["allowed-ips", "10.8.1.9/32"]
    assert restore_call[1] == "client-psk\n"

    provisioner.revoke(issued.public_key, "10.8.1.9")
    assert provisioner.calls[-1] == (
        ["remove", "awg0", "9"],
        None,
        "/opt/amnezia/traffic-limit.sh",
    )


def test_native_provisioner_rejects_legacy_awg2_config(tmp_path) -> None:
    class LegacyProvisioner(NativeAmneziaWGProvisioner):
        def _run(self, args, *, input_text=None, binary=None):  # type: ignore[no-untyped-def]
            return """[Interface]
Jc = 4
Jmin = 10
Jmax = 50
S1 = 20
S2 = 30
S3 = 40
S4 = 12
H1 = 1
H2 = 2
H3 = 3
H4 = 4
"""

    provisioner = LegacyProvisioner(
        Settings(awg_config_path=tmp_path / "not-mounted.conf")
    )
    with pytest.raises(ProvisioningError, match="Missing AmneziaWG 3 parameters"):
        provisioner._interface_settings()


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

    assert AssignedProvisioner(Settings()).assigned_ips() == {"10.8.1.2", "10.8.1.8"}


def test_native_save_uses_container_config_path() -> None:
    class SaveProvisioner(NativeAmneziaWGProvisioner):
        def __init__(self, settings: Settings):
            super().__init__(settings)
            self.call = None

        def _run(self, args, *, input_text=None, binary=None):  # type: ignore[no-untyped-def]
            self.call = (args, binary)
            return ""

    provisioner = SaveProvisioner(
        Settings(awg_config_path="/opt/amnezia/awg/awg0.conf", awg_quick_binary="/usr/bin/awg-quick")
    )
    provisioner._save()
    assert provisioner.call == (
        ["save", "/opt/amnezia/awg/awg0.conf"],
        "/usr/bin/awg-quick",
    )
