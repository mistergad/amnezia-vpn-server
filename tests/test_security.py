from app.config import Settings
from app.security import ConfigCipher, hash_password, verify_password


def test_password_hash_roundtrip() -> None:
    encoded = hash_password("a sufficiently long password")
    assert encoded != "a sufficiently long password"
    assert verify_password("a sufficiently long password", encoded)
    assert not verify_password("wrong password", encoded)


def test_vpn_config_is_encrypted() -> None:
    settings = Settings(secret_key="x" * 40)
    cipher = ConfigCipher(settings)
    encrypted = cipher.encrypt("PrivateKey = secret")
    assert "secret" not in encrypted
    assert cipher.decrypt(encrypted) == "PrivateKey = secret"

