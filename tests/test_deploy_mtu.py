from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_server_mtu_is_applied_now_and_after_container_restart() -> None:
    bootstrap = (ROOT / "deploy" / "vps-bootstrap.sh").read_text(encoding="utf-8")
    start = (ROOT / "deploy" / "awg2-start.sh").read_text(encoding="utf-8")

    assert 'readonly AWG_SERVER_MTU="1280"' in bootstrap
    assert 'ip link set dev awg0 mtu "$AWG_SERVER_MTU"' in bootstrap
    assert "ip link set dev awg0 mtu ${AWG_SERVER_MTU}" in start
    assert "${AWG_SERVER_MTU}" in bootstrap
