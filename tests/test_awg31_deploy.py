from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_bootstrap_pins_official_awg31_stack() -> None:
    bootstrap = (ROOT / "deploy" / "vps-bootstrap.sh").read_text(encoding="utf-8")

    assert (
        'readonly AWG_IMAGE="amneziavpn/amneziawg-go:3.1.20260814@'
        'sha256:4450928744b051589bb3ba5cf6dd0cd8d7dc470b9432dc32d03d5ff5ede11b7a"'
        in bootstrap
    )
    assert 'readonly AWG_TOOLS_RELEASE="3.1.20260812"' in bootstrap
    assert 'grep -Fq "$AWG_TOOLS_RELEASE"' in bootstrap
    assert 'RANDOM_TRAILERS="on"' in bootstrap
    assert 'DISABLE_COOKIES="on"' in bootstrap
    assert "--entrypoint /usr/bin/awg" in bootstrap
    assert "grep -a -q random_trailers /usr/bin/amneziawg-go" in bootstrap
    assert "verify_awg31" in bootstrap
    assert "--reset-for-awg31" in bootstrap


def test_vendored_server_config_enables_awg31_features() -> None:
    configure = (
        ROOT / "deploy" / "vendor" / "amnezia-client" / "awg2" / "configure_container.sh"
    ).read_text(encoding="utf-8")

    assert "RandomTrailers = $RANDOM_TRAILERS" in configure
    assert "DisableCookies = $DISABLE_COOKIES" in configure


def test_bootstrap_ipv4_detection_avoids_awk_builtin_names() -> None:
    bootstrap = (ROOT / "deploy" / "vps-bootstrap.sh").read_text(encoding="utf-8")

    assert "for (field = 1; field <= NF; field++)" in bootstrap
    assert "for (index = 1; index <= NF; index++)" not in bootstrap


def test_bootstrap_without_host_ignores_inherited_domain() -> None:
    bootstrap = (ROOT / "deploy" / "vps-bootstrap.sh").read_text(encoding="utf-8")

    assert 'DOMAIN=""' in bootstrap
    assert 'DOMAIN="${DOMAIN:-}"' not in bootstrap
    assert 'DOMAIN="$(detect_interface_ipv4 "$PUBLIC_INTERFACE")"' in bootstrap
