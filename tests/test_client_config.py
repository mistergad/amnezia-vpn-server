from app.services.client_config import ensure_client_mtu


def test_adds_mtu_to_legacy_config() -> None:
    config = "[Interface]\nAddress = 10.8.1.2/32\n\n[Peer]\nPublicKey = server\n"

    rendered = ensure_client_mtu(config, 1200)

    assert rendered.startswith("[Interface]\nMTU = 1200\nAddress = 10.8.1.2/32")
    assert rendered.count("MTU =") == 1


def test_replaces_existing_mtu_without_duplication() -> None:
    config = "[Interface]\r\nAddress = 10.8.1.2/32\r\nMTU = 1280\r\n\r\n[Peer]\r\n"

    rendered = ensure_client_mtu(config, 1200)

    assert "MTU = 1280" not in rendered
    assert rendered.count("MTU = 1200") == 1
    assert "\r\n" in rendered
