from pathlib import Path

from app.models import VpnCredential


ROOT = Path(__file__).resolve().parents[1]


def test_peer_manager_combines_awg_persistence_and_traffic_control() -> None:
    script = (ROOT / "deploy" / "awg2-peer-manager.sh").read_text(
        encoding="utf-8"
    )

    assert 'awg set "$interface" peer "$public_key"' in script
    assert '"$TRAFFIC_LIMIT_BINARY" apply' in script
    assert 'awg-quick save "$config_path"' in script
    assert "apply-many)" in script
    assert "rollback_batch" in script
    assert "preshared-key \"$PSK_FILE\"" in script
    assert 'preshared-key "$preshared_key"' not in script
    assert "rollback_apply" in script
    assert "acquire_lock" in script


def test_bootstrap_installs_only_the_peer_manager_for_mutations() -> None:
    bootstrap = (ROOT / "deploy" / "vps-bootstrap.sh").read_text(encoding="utf-8")

    assert '"$SCRIPT_DIR/awg2-peer-manager.sh"' in bootstrap
    assert '"$CONTAINER_NAME:/opt/amnezia/peer-manager.sh"' in bootstrap
    assert "AWG_PEER_MANAGER_BINARY=/opt/amnezia/peer-manager.sh" in bootstrap
    assert "$AWG_BIN show *" in bootstrap
    assert "$AWG_BIN showconf *" in bootstrap
    assert "/opt/amnezia/peer-manager.sh *" in bootstrap
    assert "$AWG_QUICK_BIN save" not in bootstrap.split(
        "cat > /etc/sudoers.d/amnezia-service <<EOF", maxsplit=1
    )[1].split("EOF", maxsplit=1)[0]


def test_encrypted_client_config_is_deferred_on_dashboard_queries() -> None:
    assert VpnCredential.config_encrypted.property.deferred is True
