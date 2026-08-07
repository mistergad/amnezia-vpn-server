from __future__ import annotations

import re
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import CredentialStatus, Payment, PaymentStatus, Subscription, User, VpnCredential
from app.models import SubscriptionStatus, utcnow
from app.services.lifecycle import reconcile_expired, refresh_peer_stats


def csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def test_registration_payment_key_and_revoke_flow() -> None:
    with TestClient(app) as client:
        page = client.get("/register")
        response = client.post(
            "/register",
            data={
                "email": "client@example.com",
                "password": "very-secure-password",
                "csrf_token": csrf(page.text),
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        dashboard = client.get("/app")
        assert "Запустить VPN" in dashboard.text

        response = client.post(
            "/balance/topup",
            data={
                "amount_rubles": "300",
                "csrf_token": csrf(dashboard.text),
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "/payments/mock/" in response.headers["location"]

        payment_page = client.get(response.headers["location"])
        paid = client.post(
            response.headers["location"] + "/confirm",
            data={"csrf_token": csrf(payment_page.text)},
            follow_redirects=False,
        )
        assert paid.status_code == 303
        dashboard = client.get("/app")
        assert "Первое устройство" in dashboard.text
        assert "Скачать .conf" in dashboard.text
        assert "Скопировать ключ" in dashboard.text

        with SessionLocal() as db:
            payment = db.scalar(select(Payment).where(Payment.user.has(email="client@example.com")))
            credential = db.scalar(
                select(VpnCredential).where(VpnCredential.user.has(email="client@example.com"))
            )
            assert payment and payment.status == PaymentStatus.SUCCEEDED
            assert payment.amount_kopecks == 30_000
            assert credential and credential.status == CredentialStatus.ACTIVE
            credential_id = credential.id
            subscription_id = credential.subscription_id
            subscription = db.get(Subscription, subscription_id)
            user = db.scalar(select(User).where(User.email == "client@example.com"))
            assert subscription and subscription.device_limit == 1
            assert user and user.balance_units > 0
            expiry_with_one_device = subscription.expires_at
            assert "PrivateKey" not in credential.config_encrypted

        # A repeated provider notification is idempotent and issues no second key.
        repeated = client.post(
            response.headers["location"] + "/confirm",
            data={"csrf_token": csrf(dashboard.text)},
            follow_redirects=False,
        )
        assert repeated.status_code == 303
        with SessionLocal() as db:
            credentials_count = len(
                list(
                    db.scalars(
                        select(VpnCredential).where(
                            VpnCredential.subscription_id == subscription_id
                        )
                    )
                )
            )
            assert credentials_count == 1

        # Adding a key automatically increases burn rate and shortens the forecast.
        added = client.post(
            "/app/devices",
            data={
                "subscription_id": subscription_id,
                "label": "Телефон",
                "csrf_token": csrf(dashboard.text),
            },
            follow_redirects=False,
        )
        assert added.status_code == 303
        with SessionLocal() as db:
            subscription = db.get(Subscription, subscription_id)
            second_credential = db.scalar(
                select(VpnCredential).where(
                    VpnCredential.subscription_id == subscription_id,
                    VpnCredential.label == "Телефон",
                )
            )
            assert subscription and subscription.device_limit == 2
            assert subscription.expires_at < expiry_with_one_device
            expiry_with_two_devices = subscription.expires_at
            assert second_credential

        # Revoking that key automatically lowers burn rate again.
        dashboard = client.get("/app")
        removed = client.post(
            f"/app/devices/{second_credential.id}/revoke",
            data={"csrf_token": csrf(dashboard.text)},
            follow_redirects=False,
        )
        assert removed.status_code == 303
        with SessionLocal() as db:
            subscription = db.get(Subscription, subscription_id)
            assert subscription and subscription.device_limit == 1
            assert subscription.expires_at > expiry_with_two_devices

        config_response = client.get(f"/app/devices/{credential_id}/config")
        assert config_response.status_code == 200
        assert "[Interface]" in config_response.text
        assert "PrivateKey =" in config_response.text
        assert config_response.headers["cache-control"] == "no-store"

        key_response = client.get(f"/app/devices/{credential_id}/key")
        assert key_response.status_code == 200
        assert key_response.text.startswith("vpn://")
        assert key_response.headers["cache-control"] == "no-store"

        revoked = client.post(
            f"/app/devices/{credential_id}/revoke",
            data={"csrf_token": csrf(dashboard.text)},
            follow_redirects=False,
        )
        assert revoked.status_code == 303
        assert client.get(f"/app/devices/{credential_id}/config").status_code == 410
        assert client.get(f"/app/devices/{credential_id}/key").status_code == 410

        # A key revoked by the customer remains permanently revoked after top-up.
        dashboard = client.get("/app")
        topup = client.post(
            "/balance/topup",
            data={"amount_rubles": "100", "csrf_token": csrf(dashboard.text)},
            follow_redirects=False,
        )
        payment_page = client.get(topup.headers["location"])
        confirmed = client.post(
            topup.headers["location"] + "/confirm",
            data={"csrf_token": csrf(payment_page.text)},
            follow_redirects=False,
        )
        assert confirmed.status_code == 303
        with SessionLocal() as db:
            credential = db.get(VpnCredential, credential_id)
            assert credential and credential.status == CredentialStatus.REVOKED
            assert credential.public_key not in app.state.provisioner.stats()


def test_admin_can_open_client_api() -> None:
    with TestClient(app) as client:
        login = client.get("/login")
        response = client.post(
            "/login",
            data={
                "email": "admin@test.local",
                "password": "strong-test-admin-password",
                "csrf_token": csrf(login.text),
            },
            follow_redirects=False,
        )
        assert response.headers["location"] == "/admin"
        admin_page = client.get("/admin")
        assert admin_page.status_code == 200
        assert "Баланс" in admin_page.text
        assert "Устройства" in admin_page.text
        assert "Статус" in admin_page.text
        assert "Дата окончания" in admin_page.text
        assert "Скорость" in admin_page.text
        assert "Последняя связь" not in admin_page.text
        assert "Трафик" not in admin_page.text
        api_response = client.get("/api/v1/admin/clients")
        assert api_response.status_code == 200
        assert isinstance(api_response.json(), list)
        if api_response.json():
            client_data = api_response.json()[0]
            assert set(client_data) == {
                "id",
                "email",
                "balance_kopecks",
                "active_devices",
                "download_rate_bps",
                "upload_rate_bps",
                "status",
                "expires_at",
            }
            assert client_data["active_devices"] >= 0
            if client_data["active_devices"]:
                assert client_data["expires_at"] is not None
            assert client_data["balance_kopecks"] >= 0
            assert client_data["download_rate_bps"] >= 0
            assert client_data["upload_rate_bps"] >= 0


def test_expired_key_is_restored_after_balance_topup() -> None:
    email = "resume-key@example.com"
    with TestClient(app) as client:
        register_page = client.get("/register")
        registered = client.post(
            "/register",
            data={
                "email": email,
                "password": "very-secure-password",
                "csrf_token": csrf(register_page.text),
            },
            follow_redirects=False,
        )
        assert registered.status_code == 303

        dashboard = client.get("/app")
        first_payment = client.post(
            "/balance/topup",
            data={"amount_rubles": "100", "csrf_token": csrf(dashboard.text)},
            follow_redirects=False,
        )
        payment_page = client.get(first_payment.headers["location"])
        confirmed = client.post(
            first_payment.headers["location"] + "/confirm",
            data={"csrf_token": csrf(payment_page.text)},
            follow_redirects=False,
        )
        assert confirmed.status_code == 303

        with SessionLocal() as db:
            user = db.scalar(select(User).where(User.email == email))
            credential = db.scalar(
                select(VpnCredential).where(VpnCredential.user_id == user.id)
            )
            subscription = db.get(Subscription, credential.subscription_id)
            assert user and credential and subscription
            credential_id = credential.id
            public_key = credential.public_key
            original_config = credential.config_encrypted
            credential.rx_bytes = 1024
            credential.tx_bytes = 2048
            user.balance_units = 0
            subscription.last_billed_at = utcnow() - timedelta(seconds=1)
            subscription.expires_at = utcnow() - timedelta(seconds=1)
            db.commit()
            reconcile_expired(db, app.state.provisioner)

        with SessionLocal() as db:
            credential = db.get(VpnCredential, credential_id)
            subscription = db.get(Subscription, credential.subscription_id)
            assert credential and credential.status == CredentialStatus.SUSPENDED
            assert credential.suspended_at is not None
            assert credential.revoked_at is None
            assert subscription and subscription.status == SubscriptionStatus.EXPIRED
            assert public_key not in app.state.provisioner.stats()

        suspended_config = client.get(f"/app/devices/{credential_id}/config")
        assert suspended_config.status_code == 423
        dashboard = client.get("/app")
        assert "Возобновить VPN" in dashboard.text
        assert "Повторный импорт не потребуется" in dashboard.text

        second_payment = client.post(
            "/balance/topup",
            data={"amount_rubles": "100", "csrf_token": csrf(dashboard.text)},
            follow_redirects=False,
        )
        payment_page = client.get(second_payment.headers["location"])
        resumed = client.post(
            second_payment.headers["location"] + "/confirm",
            data={"csrf_token": csrf(payment_page.text)},
            follow_redirects=False,
        )
        assert resumed.status_code == 303

        with SessionLocal() as db:
            credentials = list(
                db.scalars(select(VpnCredential).where(VpnCredential.user_id == user.id))
            )
            assert len(credentials) == 1
            credential = credentials[0]
            subscription = db.get(Subscription, credential.subscription_id)
            assert credential.id == credential_id
            assert credential.public_key == public_key
            assert credential.config_encrypted == original_config
            assert credential.status == CredentialStatus.ACTIVE
            assert credential.suspended_at is None
            assert credential.rx_offset_bytes == 1024
            assert credential.tx_offset_bytes == 2048
            assert subscription and subscription.status == SubscriptionStatus.ACTIVE
            assert subscription.device_limit == 1
            assert public_key in app.state.provisioner.stats()
            refresh_peer_stats(db, app.state.provisioner)
            assert credential.rx_bytes == 1024
            assert credential.tx_bytes == 2048

        restored_config = client.get(f"/app/devices/{credential_id}/config")
        assert restored_config.status_code == 200
        assert "PrivateKey =" in restored_config.text

        dashboard = client.get("/app")
        revoked = client.post(
            f"/app/devices/{credential_id}/revoke",
            data={"csrf_token": csrf(dashboard.text)},
            follow_redirects=False,
        )
        assert revoked.status_code == 303
        assert public_key not in app.state.provisioner.stats()


def test_admin_can_delete_device_and_customer_account() -> None:
    email = "delete-me@example.com"
    with TestClient(app) as client:
        register_page = client.get("/register")
        registered = client.post(
            "/register",
            data={
                "email": email,
                "password": "very-secure-password",
                "csrf_token": csrf(register_page.text),
            },
            follow_redirects=False,
        )
        assert registered.status_code == 303

        dashboard = client.get("/app")
        payment = client.post(
            "/balance/topup",
            data={"amount_rubles": "100", "csrf_token": csrf(dashboard.text)},
            follow_redirects=False,
        )
        payment_page = client.get(payment.headers["location"])
        confirmed = client.post(
            payment.headers["location"] + "/confirm",
            data={"csrf_token": csrf(payment_page.text)},
            follow_redirects=False,
        )
        assert confirmed.status_code == 303

        with SessionLocal() as db:
            customer = db.scalar(select(User).where(User.email == email))
            assert customer
            subscription = db.scalar(
                select(Subscription).where(Subscription.user_id == customer.id)
            )
            credential = db.scalar(
                select(VpnCredential).where(
                    VpnCredential.user_id == customer.id,
                    VpnCredential.label == "Первое устройство",
                )
            )
            assert credential and subscription
            customer_id = customer.id
            credential_id = credential.id
            subscription_id = subscription.id
            credential.rx_bytes = 1024
            credential.tx_bytes = 2048
            credential.rx_rate_bps = 8_000_000
            credential.tx_rate_bps = 10_000_000
            db.commit()

        dashboard = client.get("/app")
        assert "device-stats" not in dashboard.text
        assert "↓ 1.0 КБ" not in dashboard.text
        assert "↑ 2.0 КБ" not in dashboard.text
        added_device = client.post(
            "/app/devices",
            data={
                "subscription_id": subscription_id,
                "label": "Телефон",
                "csrf_token": csrf(dashboard.text),
            },
            follow_redirects=False,
        )
        assert added_device.status_code == 303

        client.cookies.clear()
        login = client.get("/login")
        logged_in = client.post(
            "/login",
            data={
                "email": "admin@test.local",
                "password": "strong-test-admin-password",
                "csrf_token": csrf(login.text),
            },
            follow_redirects=False,
        )
        assert logged_in.headers["location"] == "/admin"

        admin_page = client.get("/admin")
        assert email in admin_page.text
        assert "Удалить аккаунт" in admin_page.text

        client_page = client.get(f"/admin/clients/{customer_id}")
        assert client_page.status_code == 200
        assert "Первое устройство" in client_page.text
        assert "Телефон" in client_page.text
        assert "Получено" in client_page.text
        assert "Отправлено" in client_page.text
        assert "Скорость" in client_page.text
        assert "↓ 2.0 КБ" in client_page.text
        assert "↑ 1.0 КБ" in client_page.text
        assert "3.0 КБ" in client_page.text
        assert "Скорость: 10.0 Мбит/с" in client_page.text
        assert "Скорость: 8.0 Мбит/с" in client_page.text
        deleted_device = client.post(
            f"/admin/devices/{credential_id}/delete",
            data={"csrf_token": csrf(client_page.text)},
            follow_redirects=False,
        )
        assert deleted_device.status_code == 303
        assert deleted_device.headers["location"] == f"/admin/clients/{customer_id}"

        with SessionLocal() as db:
            assert db.get(VpnCredential, credential_id) is None
            subscription = db.get(Subscription, subscription_id)
            assert subscription and subscription.device_limit == 1
            assert subscription.expires_at is not None

        client_page = client.get(f"/admin/clients/{customer_id}")
        deleted_account = client.post(
            f"/admin/clients/{customer_id}/delete",
            data={"csrf_token": csrf(client_page.text)},
            follow_redirects=False,
        )
        assert deleted_account.status_code == 303
        assert deleted_account.headers["location"] == "/admin"

        with SessionLocal() as db:
            assert db.get(User, customer_id) is None
            assert not list(
                db.scalars(select(Subscription).where(Subscription.user_id == customer_id))
            )
            assert not list(db.scalars(select(Payment).where(Payment.user_id == customer_id)))
            assert not list(
                db.scalars(select(VpnCredential).where(VpnCredential.user_id == customer_id))
            )
        assert app.state.provisioner.stats() == {}

        assert email not in client.get("/admin").text
