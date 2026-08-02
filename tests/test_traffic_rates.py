from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.models import CredentialStatus
from app.services import lifecycle
from app.services.provisioning import PeerStats


class FakeSession:
    def __init__(self, credential: SimpleNamespace):
        self.credential = credential
        self.committed = False

    def scalars(self, _statement):  # type: ignore[no-untyped-def]
        return [self.credential]

    def commit(self) -> None:
        self.committed = True


class MutableStatsProvisioner:
    def __init__(self, peer: PeerStats):
        self.peer = peer

    def stats(self) -> dict[str, PeerStats]:
        return {self.peer.public_key: self.peer}


def test_refresh_peer_stats_calculates_bitrate_and_handles_counter_reset(
    monkeypatch,
) -> None:
    first_sample = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
    credential = SimpleNamespace(
        status=CredentialStatus.ACTIVE,
        public_key="peer-key",
        last_handshake_at=None,
        rx_bytes=1_000,
        tx_bytes=2_000,
        rx_offset_bytes=0,
        tx_offset_bytes=0,
        rx_rate_bps=0,
        tx_rate_bps=0,
        traffic_sampled_at=first_sample,
    )
    session = FakeSession(credential)
    provisioner = MutableStatsProvisioner(
        PeerStats("peer-key", first_sample, 6_000, 12_000)
    )

    monkeypatch.setattr(lifecycle, "utcnow", lambda: first_sample + timedelta(seconds=10))
    assert lifecycle.refresh_peer_stats(session, provisioner) == 1  # type: ignore[arg-type]
    assert credential.rx_rate_bps == 4_000
    assert credential.tx_rate_bps == 8_000
    assert credential.rx_bytes == 6_000
    assert credential.tx_bytes == 12_000

    # Simulate an AWG interface restart: its raw counters start again at zero.
    provisioner.peer = PeerStats(
        "peer-key", first_sample + timedelta(seconds=20), 100, 200
    )
    monkeypatch.setattr(lifecycle, "utcnow", lambda: first_sample + timedelta(seconds=20))
    assert lifecycle.refresh_peer_stats(session, provisioner) == 1  # type: ignore[arg-type]
    assert credential.rx_offset_bytes == 6_000
    assert credential.tx_offset_bytes == 12_000
    assert credential.rx_bytes == 6_100
    assert credential.tx_bytes == 12_200
    assert credential.rx_rate_bps == 80
    assert credential.tx_rate_bps == 160
    assert session.committed
