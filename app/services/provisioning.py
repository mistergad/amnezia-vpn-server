from __future__ import annotations

import base64
import configparser
import hashlib
import ipaddress
import secrets
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.config import Settings


@dataclass(frozen=True)
class ProvisionedCredential:
    public_key: str
    config: str


@dataclass(frozen=True)
class PeerStats:
    public_key: str
    last_handshake_at: datetime | None
    rx_bytes: int
    tx_bytes: int


class ProvisioningError(RuntimeError):
    pass


class Provisioner:
    def provision(self, assigned_ip: str) -> ProvisionedCredential:
        raise NotImplementedError

    def restore(self, public_key: str, assigned_ip: str, config: str) -> None:
        """Restore an existing peer without changing the client-side key."""
        raise NotImplementedError

    def revoke(self, public_key: str, assigned_ip: str | None = None) -> None:
        raise NotImplementedError

    def stats(self) -> dict[str, PeerStats]:
        raise NotImplementedError

    def assigned_ips(self) -> set[str]:
        """Return addresses already present on the live VPN node."""
        return set()


class MockProvisioner(Provisioner):
    """Development backend. It never changes networking on the host."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._peers: dict[str, str] = {}

    @staticmethod
    def _key() -> str:
        return base64.b64encode(secrets.token_bytes(32)).decode()

    def provision(self, assigned_ip: str) -> ProvisionedCredential:
        private_key = self._key()
        public_key = base64.b64encode(hashlib.sha256(private_key.encode()).digest()).decode()
        server_key = self._key()
        psk = self._key()
        self._peers[public_key] = assigned_ip
        config = _render_client_config(
            private_key=private_key,
            assigned_ip=assigned_ip,
            dns=self.settings.awg_dns,
            obfuscation={
                "Jc": "4",
                "Jmin": "10",
                "Jmax": "50",
                "S1": "25",
                "S2": "35",
                "S3": "45",
                "S4": "12",
                "H1": "1",
                "H2": "2",
                "H3": "3",
                "H4": "4",
                "HeaderProtectionKey": self._key(),
                "ContentPaddingAddition": "10-100",
                "RekeyAfterTime": "100-120",
                "RekeyTimeout": "3-7",
                "RejectAfterTime": "150-180",
                "KeepaliveTimeout": "5-15",
                "MaxHandshakeAttempts": "15-20",
            },
            server_public_key=server_key,
            preshared_key=psk,
            endpoint=self.settings.awg_endpoint,
        )
        return ProvisionedCredential(public_key=public_key, config=config)

    def revoke(self, public_key: str, assigned_ip: str | None = None) -> None:
        self._peers.pop(public_key, None)

    def restore(self, public_key: str, assigned_ip: str, config: str) -> None:
        self._peers[public_key] = assigned_ip

    def assigned_ips(self) -> set[str]:
        return set(self._peers.values())

    def stats(self) -> dict[str, PeerStats]:
        return {
            key: PeerStats(key, None, 0, 0)
            for key in self._peers
        }


class NativeAmneziaWGProvisioner(Provisioner):
    """Controls one AmneziaWG 3 interface using the official awg tools."""

    PARAMETER_NAMES = (
        "Jc", "Jmin", "Jmax", "S1", "S2", "S3", "S4",
        "H1", "H2", "H3", "H4", "I1", "I2", "I3", "I4", "I5",
        "HeaderProtectionKey", "ContentPaddingAddition", "RekeyAfterTime",
        "RekeyTimeout", "RejectAfterTime", "KeepaliveTimeout",
        "MaxHandshakeAttempts",
    )

    REQUIRED_PARAMETER_NAMES = {
        "Jc", "Jmin", "Jmax", "S1", "S2", "S3", "S4",
        "H1", "H2", "H3", "H4", "HeaderProtectionKey",
        "ContentPaddingAddition", "RekeyAfterTime", "RekeyTimeout",
        "RejectAfterTime", "KeepaliveTimeout", "MaxHandshakeAttempts",
    }

    def __init__(self, settings: Settings):
        self.settings = settings

    def _run(
        self, args: list[str], *, input_text: str | None = None, binary: str | None = None
    ) -> str:
        command = [*self.settings.awg_command_prefix, binary or self.settings.awg_binary, *args]
        try:
            completed = subprocess.run(
                command,
                input=input_text,
                text=True,
                capture_output=True,
                check=True,
                timeout=20,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            stderr = getattr(exc, "stderr", "") or ""
            raise ProvisioningError(
                f"AmneziaWG command failed: {' '.join(command[:3])}: {stderr.strip()}"
            ) from exc
        return completed.stdout.strip()

    def _interface_settings(self) -> dict[str, str]:
        path = Path(self.settings.awg_config_path)
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        parser.optionxform = str
        try:
            if path.is_file():
                parser.read(path, encoding="utf-8")
            else:
                # Useful when the interface is managed inside the official
                # amnezia-awg container and the config is not mounted on the host.
                parser.read_string(
                    self._run(["showconf", self.settings.awg_interface])
                )
            interface = parser["Interface"]
        except (configparser.Error, KeyError) as exc:
            raise ProvisioningError("Cannot parse the AmneziaWG [Interface] config") from exc
        values = {
            name: interface.get(name, "").strip()
            for name in self.PARAMETER_NAMES
            if interface.get(name, "").strip()
        }
        for number in range(1, 6):
            configured = getattr(self.settings, f"awg_i{number}")
            if configured:
                values[f"I{number}"] = configured.strip()
        if not self.REQUIRED_PARAMETER_NAMES.issubset(values):
            missing = ", ".join(
                sorted(self.REQUIRED_PARAMETER_NAMES - values.keys())
            )
            raise ProvisioningError(
                f"Missing AmneziaWG 3 parameters in server config: {missing}"
            )
        return values

    def _save(self) -> None:
        if self.settings.awg_save_config:
            self._run(
                ["save", self.settings.awg_config_path.as_posix()],
                binary=self.settings.awg_quick_binary,
            )

    def _rate_limit_class_minor(self, assigned_ip: str) -> int:
        network = ipaddress.ip_network(self.settings.awg_subnet, strict=False)
        address = ipaddress.ip_address(assigned_ip)
        if network.version != 4 or address.version != 4 or address not in network:
            raise ProvisioningError(
                f"Cannot rate-limit {assigned_ip}: address is outside {network}"
            )
        minor = int(address) - int(network.network_address)
        if minor < 2 or minor > 65534:
            raise ProvisioningError(
                "Per-device rate limits require an IPv4 subnet with no more "
                "than 65534 client addresses"
            )
        return minor

    def _apply_rate_limit(self, assigned_ip: str) -> None:
        if not self.settings.awg_rate_limit_enabled:
            return
        self._run(
            [
                "apply",
                self.settings.awg_interface,
                assigned_ip,
                str(self._rate_limit_class_minor(assigned_ip)),
                str(self.settings.awg_download_limit_mbps),
                str(self.settings.awg_upload_limit_mbps),
            ],
            binary=self.settings.awg_rate_limit_binary,
        )

    def _remove_rate_limit(self, assigned_ip: str) -> None:
        if not self.settings.awg_rate_limit_enabled:
            return
        self._run(
            [
                "remove",
                self.settings.awg_interface,
                str(self._rate_limit_class_minor(assigned_ip)),
            ],
            binary=self.settings.awg_rate_limit_binary,
        )

    def assigned_ips(self) -> set[str]:
        output = self._run(["show", self.settings.awg_interface, "allowed-ips"])
        assigned: set[str] = set()
        for line in output.splitlines():
            fields = line.split("\t", 1)
            if len(fields) != 2 or fields[1].strip() == "(none)":
                continue
            for value in fields[1].split(","):
                try:
                    network = ipaddress.ip_network(value.strip(), strict=False)
                except ValueError:
                    continue
                if network.version == 4 and network.prefixlen == 32:
                    assigned.add(str(network.network_address))
        return assigned

    def provision(self, assigned_ip: str) -> ProvisionedCredential:
        private_key = self._run(["genkey"])
        public_key = self._run(["pubkey"], input_text=private_key + "\n")
        preshared_key = self._run(["genpsk"])
        server_public_key = self._run(["show", self.settings.awg_interface, "public-key"])
        self._run(
            [
                "set", self.settings.awg_interface, "peer", public_key,
                "preshared-key", "/dev/stdin", "allowed-ips", f"{assigned_ip}/32",
            ],
            input_text=preshared_key + "\n",
        )
        try:
            self._save()
            self._apply_rate_limit(assigned_ip)
            config = _render_client_config(
                private_key=private_key,
                assigned_ip=assigned_ip,
                dns=self.settings.awg_dns,
                obfuscation=self._interface_settings(),
                server_public_key=server_public_key,
                preshared_key=preshared_key,
                endpoint=self.settings.awg_endpoint,
            )
            return ProvisionedCredential(public_key=public_key, config=config)
        except Exception:
            try:
                self._run(
                    ["set", self.settings.awg_interface, "peer", public_key, "remove"]
                )
                self._save()
            except Exception:
                pass
            raise

    def revoke(self, public_key: str, assigned_ip: str | None = None) -> None:
        self._run(["set", self.settings.awg_interface, "peer", public_key, "remove"])
        self._save()
        if assigned_ip:
            try:
                self._remove_rate_limit(assigned_ip)
            except ProvisioningError:
                # A stale tc class is harmless and is removed by the next
                # container/service synchronization. Peer revocation must win.
                pass

    def restore(self, public_key: str, assigned_ip: str, config: str) -> None:
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            parser.read_string(config)
            preshared_key = parser.get("Peer", "PresharedKey").strip()
        except (configparser.Error, KeyError) as exc:
            raise ProvisioningError(
                "Cannot restore peer: PresharedKey is missing from the client config"
            ) from exc
        if not preshared_key:
            raise ProvisioningError(
                "Cannot restore peer: PresharedKey is empty in the client config"
            )
        self._run(
            [
                "set", self.settings.awg_interface, "peer", public_key,
                "preshared-key", "/dev/stdin", "allowed-ips", f"{assigned_ip}/32",
            ],
            input_text=preshared_key + "\n",
        )
        try:
            self._save()
            self._apply_rate_limit(assigned_ip)
        except Exception:
            try:
                self._run(
                    ["set", self.settings.awg_interface, "peer", public_key, "remove"]
                )
                self._save()
            except Exception:
                pass
            raise

    def stats(self) -> dict[str, PeerStats]:
        dump = self._run(["show", self.settings.awg_interface, "dump"])
        peers: dict[str, PeerStats] = {}
        for line_number, line in enumerate(dump.splitlines()):
            if line_number == 0 or not line.strip():
                continue
            fields = line.split("\t")
            if len(fields) < 8:
                continue
            try:
                handshake_epoch = int(fields[4])
                handshake = (
                    datetime.fromtimestamp(handshake_epoch, tz=timezone.utc)
                    if handshake_epoch > 0 else None
                )
                peers[fields[0]] = PeerStats(
                    public_key=fields[0],
                    last_handshake_at=handshake,
                    rx_bytes=int(fields[5]),
                    tx_bytes=int(fields[6]),
                )
            except ValueError:
                continue
        return peers


def _render_client_config(
    *,
    private_key: str,
    assigned_ip: str,
    dns: str,
    obfuscation: dict[str, str],
    server_public_key: str,
    preshared_key: str,
    endpoint: str,
) -> str:
    ipaddress.ip_address(assigned_ip)
    interface_lines = [
        "[Interface]",
        f"Address = {assigned_ip}/32",
        f"DNS = {dns}",
        f"PrivateKey = {private_key}",
    ]
    for name in NativeAmneziaWGProvisioner.PARAMETER_NAMES:
        value = obfuscation.get(name)
        if value:
            interface_lines.append(f"{name} = {value}")
    peer_lines = [
        "",
        "[Peer]",
        f"PublicKey = {server_public_key}",
        f"PresharedKey = {preshared_key}",
        "AllowedIPs = 0.0.0.0/0, ::/0",
        f"Endpoint = {endpoint}",
        "PersistentKeepalive = 25-35",
        "",
    ]
    return "\n".join([*interface_lines, *peer_lines])


def build_provisioner(settings: Settings) -> Provisioner:
    if settings.vpn_backend == "mock":
        return MockProvisioner(settings)
    if settings.vpn_backend == "native":
        return NativeAmneziaWGProvisioner(settings)
    raise ValueError(f"Unsupported VPN_BACKEND: {settings.vpn_backend}")
