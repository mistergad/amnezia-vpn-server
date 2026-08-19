from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_bootstrap_pins_official_awg31_stack() -> None:
    bootstrap = (ROOT / "deploy" / "vps-bootstrap.sh").read_text(encoding="utf-8")

    assert (
        'readonly AWG_IMAGE="amneziavpn/amneziawg-go:3.1.20260812@'
        'sha256:c60cc651df4a2315d67dcd5411203fa1eb1beb4cb493aa6326cfaf8359d00434"'
        in bootstrap
    )
    assert 'readonly AWG_RELEASE="3.1.20260812"' in bootstrap
    assert 'grep -Fq "$AWG_RELEASE"' in bootstrap
    assert 'RANDOM_TRAILERS="on"' in bootstrap
    assert 'DISABLE_COOKIES="on"' in bootstrap
    assert "grep -a -q RandomTrailers /usr/bin/awg" in bootstrap
    assert "grep -a -q random_trailers /usr/bin/amneziawg-go" in bootstrap
    assert "verify_awg31" in bootstrap
    assert "--reset-for-awg31" in bootstrap


def test_vendored_server_config_enables_awg31_features() -> None:
    configure = (
        ROOT / "deploy" / "vendor" / "amnezia-client" / "awg2" / "configure_container.sh"
    ).read_text(encoding="utf-8")

    assert "RandomTrailers = $RANDOM_TRAILERS" in configure
    assert "DisableCookies = $DISABLE_COOKIES" in configure
