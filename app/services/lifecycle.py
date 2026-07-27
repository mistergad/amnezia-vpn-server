from __future__ import annotations

import ipaddress
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    CredentialStatus,
    Payment,
    PaymentStatus,
    Plan,
    Subscription,
    SubscriptionStatus,
    User,
    UserRole,
    VpnCredential,
    utcnow,
)
from app.security import ConfigCipher, hash_password, normalize_email
from app.services.payments import PaymentProvider, VerifiedPayment
from app.services.provisioning import Provisioner


class BusinessRuleError(RuntimeError):
    pass


PRICE_PER_DEVICE_MONTH_KOPECKS = 10_000
BILLING_MONTH_SECONDS = 30 * 24 * 60 * 60


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def seed_data(db: Session, settings: Settings) -> None:
    plans = [
        ("month", "Архивный: 1 месяц", 29900, 30, 3, False),
        ("quarter", "Архивный: 3 месяца", 74900, 90, 5, False),
        ("year", "Архивный: 1 год", 249900, 365, 7, False),
        ("balance", "Пополнение баланса", 10000, 30, 1, True),
    ]
    for slug, name, price, days, devices, active in plans:
        plan = db.scalar(select(Plan).where(Plan.slug == slug))
        if not plan:
            db.add(
                Plan(
                    slug=slug,
                    name=name,
                    price_kopecks=price,
                    duration_days=days,
                    device_limit=devices,
                    is_active=active,
                )
            )
        else:
            plan.is_active = active
    admin_email = normalize_email(settings.admin_email)
    admin = db.scalar(select(User).where(User.email == admin_email))
    if not admin:
        from app.models import UserRole

        db.add(
            User(
                email=admin_email,
                password_hash=hash_password(settings.admin_password),
                role=UserRole.ADMIN,
            )
        )
    db.commit()
    _migrate_legacy_subscriptions(db)
    _migrate_legacy_expired_credentials(db)
    _sync_active_device_counts(db)


def _migrate_legacy_subscriptions(db: Session) -> None:
    """Convert remaining prepaid time into balance units exactly once."""
    now = utcnow()
    legacy = list(
        db.scalars(
            select(Subscription).where(Subscription.last_billed_at.is_(None))
        )
    )
    for subscription in legacy:
        old_limit = max(1, subscription.plan.device_limit)
        subscription.device_limit = old_limit
        expiry = as_utc(subscription.expires_at)
        if subscription.status == SubscriptionStatus.ACTIVE and expiry and expiry > now:
            remaining_seconds = max(0, int((expiry - now).total_seconds()))
            subscription.user.balance_units += remaining_seconds * old_limit
            subscription.last_billed_at = now
            subscription.expires_at = now + timedelta(
                seconds=subscription.user.balance_units // old_limit
            )
        else:
            subscription.last_billed_at = expiry or now
    db.commit()


def _sync_active_device_counts(db: Session) -> None:
    """Make billing count match active peer keys after an upgrade or restart."""
    now = utcnow()
    subscriptions = list(
        db.scalars(
            select(Subscription).where(Subscription.status == SubscriptionStatus.ACTIVE)
        )
    )
    for subscription in subscriptions:
        settle_subscription(db, subscription, now=now)
        active_devices = db.scalar(
            select(func.count(VpnCredential.id)).where(
                VpnCredential.subscription_id == subscription.id,
                VpnCredential.status.in_(
                    [CredentialStatus.ACTIVE, CredentialStatus.SUSPENDED]
                ),
            )
        ) or 0
        subscription.device_limit = active_devices
        subscription.last_billed_at = now
        subscription.expires_at = (
            now + timedelta(seconds=subscription.user.balance_units // active_devices)
            if active_devices
            else None
        )
    db.commit()


def _migrate_legacy_expired_credentials(db: Session) -> None:
    """Preserve keys disabled by the old irreversible expiry behavior."""
    expired_subscriptions = list(
        db.scalars(
            select(Subscription).where(
                Subscription.status == SubscriptionStatus.EXPIRED,
                Subscription.expires_at.is_not(None),
            )
        )
    )
    for subscription in expired_subscriptions:
        expiry = as_utc(subscription.expires_at)
        if not expiry:
            continue
        credentials = list(
            db.scalars(
                select(VpnCredential).where(
                    VpnCredential.subscription_id == subscription.id,
                    VpnCredential.status == CredentialStatus.REVOKED,
                    VpnCredential.revoked_at.is_not(None),
                )
            )
        )
        for credential in credentials:
            revoked_at = as_utc(credential.revoked_at)
            if revoked_at and revoked_at >= expiry:
                credential.status = CredentialStatus.SUSPENDED
                credential.suspended_at = credential.revoked_at
                credential.revoked_at = None
    db.commit()


def balance_kopecks(user: User) -> int:
    return max(
        0,
        user.balance_units * PRICE_PER_DEVICE_MONTH_KOPECKS // BILLING_MONTH_SECONDS,
    )


def _units_for_payment(amount_kopecks: int) -> int:
    if amount_kopecks < PRICE_PER_DEVICE_MONTH_KOPECKS:
        raise BusinessRuleError("Минимальное пополнение — 100 ₽")
    if amount_kopecks % PRICE_PER_DEVICE_MONTH_KOPECKS:
        raise BusinessRuleError("Сумма пополнения должна быть кратна 100 ₽")
    return amount_kopecks * BILLING_MONTH_SECONDS // PRICE_PER_DEVICE_MONTH_KOPECKS


def settle_subscription(
    db: Session, subscription: Subscription, *, now: datetime | None = None
) -> Subscription:
    """Burn balance according to elapsed seconds and selected device count."""
    now = now or utcnow()
    if subscription.status != SubscriptionStatus.ACTIVE:
        return subscription
    if subscription.last_billed_at is None:
        subscription.last_billed_at = now
    last_billed = as_utc(subscription.last_billed_at) or now
    elapsed_seconds = max(0, int((now - last_billed).total_seconds()))
    limit = max(0, subscription.device_limit)
    if limit == 0:
        subscription.last_billed_at = now
        subscription.expires_at = None
        if subscription.user.balance_units <= 0:
            subscription.status = SubscriptionStatus.EXPIRED
        return subscription
    if elapsed_seconds:
        required_units = elapsed_seconds * limit
        if subscription.user.balance_units >= required_units:
            subscription.user.balance_units -= required_units
            subscription.last_billed_at = last_billed + timedelta(seconds=elapsed_seconds)
        else:
            affordable_seconds = subscription.user.balance_units // limit
            depleted_at = last_billed + timedelta(seconds=affordable_seconds)
            subscription.user.balance_units = 0
            subscription.last_billed_at = depleted_at
            subscription.expires_at = depleted_at
            subscription.status = SubscriptionStatus.EXPIRED
            return subscription
    seconds_left = subscription.user.balance_units // limit
    base = as_utc(subscription.last_billed_at) or now
    subscription.expires_at = base + timedelta(seconds=seconds_left)
    if seconds_left <= 0:
        subscription.status = SubscriptionStatus.EXPIRED
    return subscription


def create_balance_payment(
    db: Session,
    *,
    user: User,
    amount_rubles: int,
    provider: PaymentProvider,
    return_url: str,
) -> Payment:
    amount_kopecks = amount_rubles * 100
    _units_for_payment(amount_kopecks)
    plan = db.scalar(select(Plan).where(Plan.slug == "balance"))
    if not plan:
        raise BusinessRuleError("Балансовый тариф не настроен")
    payment = Payment(
        id=str(uuid.uuid4()),
        user_id=user.id,
        plan_id=plan.id,
        provider=provider.name,
        idempotency_key=secrets.token_hex(16),
        amount_kopecks=amount_kopecks,
        requested_device_limit=1,
        currency="RUB",
        status=PaymentStatus.PENDING,
    )
    payment.plan = plan
    db.add(payment)
    db.flush()
    try:
        confirmation = provider.create(payment, return_url)
    except Exception:
        db.rollback()
        raise
    payment.provider_payment_id = confirmation.provider_payment_id
    payment.confirmation_url = confirmation.confirmation_url
    db.commit()
    return payment


def activate_payment(db: Session, payment: Payment) -> Subscription:
    payment = db.scalar(select(Payment).where(Payment.id == payment.id).with_for_update())
    if payment is None:
        raise BusinessRuleError("Платеж не найден")
    if payment.status == PaymentStatus.SUCCEEDED:
        subscription = db.get(Subscription, payment.subscription_id)
        if not subscription:
            raise BusinessRuleError("Платеж активирован некорректно")
        return subscription
    now = utcnow()
    user = db.scalar(select(User).where(User.id == payment.user_id).with_for_update())
    if not user:
        raise BusinessRuleError("Пользователь платежа не найден")
    payment.status = PaymentStatus.SUCCEEDED
    payment.paid_at = now
    subscription = db.scalar(
        select(Subscription)
        .where(
            Subscription.user_id == payment.user_id,
            Subscription.status == SubscriptionStatus.ACTIVE,
        )
        .order_by(Subscription.created_at.desc())
        .limit(1)
        .with_for_update()
    )
    if subscription:
        settle_subscription(db, subscription, now=now)
    if not subscription:
        subscription = db.scalar(
            select(Subscription)
            .where(
                Subscription.user_id == payment.user_id,
                Subscription.status == SubscriptionStatus.EXPIRED,
            )
            .order_by(Subscription.created_at.desc())
            .limit(1)
            .with_for_update()
        )
    if subscription and subscription.status == SubscriptionStatus.EXPIRED:
        previous_expiry = as_utc(subscription.expires_at)
        if previous_expiry:
            legacy_expired_credentials = list(
                db.scalars(
                    select(VpnCredential).where(
                        VpnCredential.subscription_id == subscription.id,
                        VpnCredential.status == CredentialStatus.REVOKED,
                    )
                )
            )
            for credential in legacy_expired_credentials:
                revoked_at = as_utc(credential.revoked_at)
                if revoked_at and revoked_at >= previous_expiry:
                    credential.status = CredentialStatus.SUSPENDED
                    credential.suspended_at = credential.revoked_at
                    credential.revoked_at = None
    if not subscription:
        subscription = Subscription(
            user_id=payment.user_id,
            plan_id=payment.plan_id,
            status=SubscriptionStatus.ACTIVE,
            starts_at=now,
            expires_at=None,
            device_limit=0,
            last_billed_at=now,
        )
        db.add(subscription)
        db.flush()
    user.balance_units += _units_for_payment(payment.amount_kopecks)
    billable_devices = db.scalar(
        select(func.count(VpnCredential.id)).where(
            VpnCredential.subscription_id == subscription.id,
            VpnCredential.status.in_(
                [CredentialStatus.ACTIVE, CredentialStatus.SUSPENDED]
            ),
        )
    ) or 0
    subscription.plan_id = payment.plan_id
    subscription.status = SubscriptionStatus.ACTIVE
    subscription.device_limit = billable_devices
    subscription.last_billed_at = now
    subscription.expires_at = (
        now + timedelta(seconds=user.balance_units // subscription.device_limit)
        if subscription.device_limit
        else None
    )
    payment.subscription_id = subscription.id
    db.commit()
    return subscription


def apply_verified_payment(db: Session, verified: VerifiedPayment) -> Subscription | None:
    payment = db.scalar(
        select(Payment).where(Payment.provider_payment_id == verified.provider_payment_id)
    )
    if not payment or verified.internal_payment_id != payment.id:
        raise BusinessRuleError("Платеж не соответствует внутреннему заказу")
    if verified.amount_kopecks != payment.amount_kopecks or verified.currency != payment.currency:
        raise BusinessRuleError("Сумма или валюта платежа не совпадает")
    if verified.status == "canceled":
        payment.status = PaymentStatus.CANCELED
        db.commit()
        return None
    if verified.status != "succeeded":
        return None
    return activate_payment(db, payment)


def _lock_address_pool(db: Session) -> None:
    if db.bind and db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(7300191)"))


def _allocate_ip(db: Session, settings: Settings, provisioner: Provisioner) -> str:
    network = ipaddress.ip_network(settings.awg_subnet)
    if network.version != 4:
        raise BusinessRuleError("В этой версии поддерживается только IPv4-пул")
    used = set(
        db.scalars(
            select(VpnCredential.assigned_ip).where(
                VpnCredential.status.in_(
                    [CredentialStatus.ACTIVE, CredentialStatus.SUSPENDED]
                )
            )
        ).all()
    )
    used.update(provisioner.assigned_ips())
    hosts = network.hosts()
    next(hosts, None)  # The first host is reserved for the VPN interface.
    for address in hosts:
        if str(address) not in used:
            return str(address)
    raise BusinessRuleError("На VPN-узле закончились свободные адреса")


def create_credential(
    db: Session,
    *,
    subscription: Subscription,
    label: str,
    settings: Settings,
    provisioner: Provisioner,
) -> VpnCredential:
    subscription = db.scalar(
        select(Subscription).where(Subscription.id == subscription.id).with_for_update()
    )
    now = utcnow()
    if subscription:
        settle_subscription(db, subscription, now=now)
    if not subscription or subscription.status != SubscriptionStatus.ACTIVE:
        raise BusinessRuleError("Нужна активная подписка")
    if subscription.user.balance_units <= 0:
        raise BusinessRuleError("Пополните баланс перед добавлением устройства")
    count = db.scalar(
        select(func.count(VpnCredential.id)).where(
            VpnCredential.subscription_id == subscription.id,
            VpnCredential.status.in_(
                [CredentialStatus.ACTIVE, CredentialStatus.SUSPENDED]
            ),
        )
    ) or 0
    if count >= settings.max_devices_per_subscription:
        raise BusinessRuleError("Достигнут технический максимум устройств")
    _lock_address_pool(db)
    assigned_ip = _allocate_ip(db, settings, provisioner)
    provisioned = None
    clean_label = label.strip()[:120] or "Устройство"
    try:
        provisioned = provisioner.provision(assigned_ip)
        credential = VpnCredential(
            user_id=subscription.user_id,
            subscription_id=subscription.id,
            label=clean_label,
            public_key=provisioned.public_key,
            assigned_ip=assigned_ip,
            config_encrypted=ConfigCipher(settings).encrypt(provisioned.config),
            status=CredentialStatus.ACTIVE,
        )
        db.add(credential)
        db.flush()
        subscription.device_limit = count + 1
        subscription.last_billed_at = now
        subscription.expires_at = now + timedelta(
            seconds=subscription.user.balance_units // subscription.device_limit
        )
        db.commit()
        return credential
    except Exception:
        db.rollback()
        if provisioned is not None:
            try:
                provisioner.revoke(provisioned.public_key)
            except Exception:
                pass
        raise


def ensure_first_credential(
    db: Session,
    *,
    subscription: Subscription,
    settings: Settings,
    provisioner: Provisioner,
) -> VpnCredential | None:
    existing = db.scalar(
        select(VpnCredential).where(VpnCredential.subscription_id == subscription.id).limit(1)
    )
    if existing:
        return existing
    return create_credential(
        db,
        subscription=subscription,
        label="Первое устройство",
        settings=settings,
        provisioner=provisioner,
    )


def restore_suspended_credentials(
    db: Session,
    *,
    subscription: Subscription,
    settings: Settings,
    provisioner: Provisioner,
) -> int:
    """Restore suspended peers with their original keys after a balance top-up."""
    if (
        subscription.status != SubscriptionStatus.ACTIVE
        or subscription.user.balance_units <= 0
    ):
        return 0
    credentials = list(
        db.scalars(
            select(VpnCredential).where(
                VpnCredential.subscription_id == subscription.id,
                VpnCredential.status == CredentialStatus.SUSPENDED,
            )
        )
    )
    cipher = ConfigCipher(settings)
    restored = 0
    for credential in credentials:
        try:
            config = cipher.decrypt(credential.config_encrypted)
            provisioner.restore(
                credential.public_key,
                credential.assigned_ip,
                config,
            )
            credential.rx_offset_bytes = credential.rx_bytes
            credential.tx_offset_bytes = credential.tx_bytes
            credential.status = CredentialStatus.ACTIVE
            credential.suspended_at = None
            credential.revoked_at = None
            credential.last_handshake_at = None
            db.commit()
            restored += 1
        except Exception:
            db.rollback()
    return restored


def reconcile_suspended(
    db: Session, settings: Settings, provisioner: Provisioner
) -> int:
    """Retry peer restoration after transient provisioning failures."""
    restored = 0
    subscriptions = list(
        db.scalars(
            select(Subscription).where(
                Subscription.status == SubscriptionStatus.ACTIVE,
                Subscription.user.has(User.balance_units > 0),
            )
        )
    )
    for subscription in subscriptions:
        restored += restore_suspended_credentials(
            db,
            subscription=subscription,
            settings=settings,
            provisioner=provisioner,
        )
    return restored


def revoke_credential(
    db: Session, credential: VpnCredential, provisioner: Provisioner
) -> None:
    credential = db.scalar(
        select(VpnCredential).where(VpnCredential.id == credential.id).with_for_update()
    )
    if not credential or credential.status == CredentialStatus.REVOKED:
        return
    subscription = db.scalar(
        select(Subscription)
        .where(Subscription.id == credential.subscription_id)
        .with_for_update()
    )
    now = utcnow()
    if subscription:
        settle_subscription(db, subscription, now=now)
    provisioner.revoke(credential.public_key)
    credential.status = CredentialStatus.REVOKED
    credential.suspended_at = None
    credential.revoked_at = now
    db.flush()
    if subscription and subscription.status == SubscriptionStatus.ACTIVE:
        active_devices = db.scalar(
            select(func.count(VpnCredential.id)).where(
                VpnCredential.subscription_id == subscription.id,
                VpnCredential.status.in_(
                    [CredentialStatus.ACTIVE, CredentialStatus.SUSPENDED]
                ),
            )
        ) or 0
        subscription.device_limit = active_devices
        subscription.last_billed_at = now
        subscription.expires_at = (
            now + timedelta(seconds=subscription.user.balance_units // active_devices)
            if active_devices
            else None
        )
    db.commit()


def delete_credential(
    db: Session, credential: VpnCredential, provisioner: Provisioner
) -> None:
    """Disable a live peer before permanently removing its database record."""
    credential_id = credential.id
    if credential.status != CredentialStatus.REVOKED:
        revoke_credential(db, credential, provisioner)
    stored = db.get(VpnCredential, credential_id)
    if stored:
        db.delete(stored)
        db.commit()


def delete_customer_account(
    db: Session, user: User, provisioner: Provisioner
) -> None:
    """Revoke every peer and permanently remove all customer-owned records."""
    if user.role != UserRole.CUSTOMER:
        raise BusinessRuleError("Можно удалять только аккаунты клиентов")

    active_credentials = list(
        db.scalars(
            select(VpnCredential).where(
                VpnCredential.user_id == user.id,
                VpnCredential.status == CredentialStatus.ACTIVE,
            )
        )
    )
    # Each successful revoke is committed so a provisioning failure cannot leave
    # a missing peer represented as active in the control plane.
    for credential in active_credentials:
        revoke_credential(db, credential, provisioner)

    user_id = user.id
    try:
        db.execute(delete(VpnCredential).where(VpnCredential.user_id == user_id))
        db.execute(delete(Payment).where(Payment.user_id == user_id))
        db.execute(delete(Subscription).where(Subscription.user_id == user_id))
        db.execute(delete(User).where(User.id == user_id))
        db.commit()
    except Exception:
        db.rollback()
        raise


def refresh_peer_stats(db: Session, provisioner: Provisioner) -> int:
    stats = provisioner.stats()
    updated = 0
    for credential in db.scalars(
        select(VpnCredential).where(VpnCredential.status == CredentialStatus.ACTIVE)
    ):
        peer = stats.get(credential.public_key)
        if peer:
            credential.last_handshake_at = peer.last_handshake_at
            credential.rx_bytes = credential.rx_offset_bytes + peer.rx_bytes
            credential.tx_bytes = credential.tx_offset_bytes + peer.tx_bytes
            updated += 1
    db.commit()
    return updated


def reconcile_expired(db: Session, provisioner: Provisioner) -> int:
    now = utcnow()
    subscriptions = list(
        db.scalars(
            select(Subscription).where(
                Subscription.status == SubscriptionStatus.ACTIVE,
            )
        )
    )
    reconciled = 0
    for subscription in subscriptions:
        try:
            settle_subscription(db, subscription, now=now)
            if subscription.status == SubscriptionStatus.ACTIVE:
                db.commit()
                continue
            credentials = list(
                db.scalars(
                    select(VpnCredential).where(
                        VpnCredential.subscription_id == subscription.id,
                        VpnCredential.status == CredentialStatus.ACTIVE,
                    )
                )
            )
            for credential in credentials:
                provisioner.revoke(credential.public_key)
                credential.status = CredentialStatus.SUSPENDED
                credential.suspended_at = now
                credential.revoked_at = None
                credential.last_handshake_at = None
            db.commit()
            reconciled += 1
        except Exception:
            db.rollback()
    return reconciled
