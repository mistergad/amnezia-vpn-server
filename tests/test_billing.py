from datetime import timedelta

from app.models import Subscription, SubscriptionStatus, User, utcnow
from app.services.lifecycle import (
    BILLING_MONTH_SECONDS,
    PRICE_PER_DEVICE_MONTH_KOPECKS,
    balance_kopecks,
    settle_subscription,
)


def test_balance_burn_scales_with_device_count() -> None:
    now = utcnow()
    user = User(email="billing@example.com", password_hash="unused")
    user.balance_units = BILLING_MONTH_SECONDS
    subscription = Subscription(
        user=user,
        plan_id="balance",
        status=SubscriptionStatus.ACTIVE,
        starts_at=now - timedelta(days=1),
        last_billed_at=now - timedelta(days=1),
        expires_at=now,
        device_limit=2,
    )

    settle_subscription(None, subscription, now=now)  # type: ignore[arg-type]

    assert user.balance_units == BILLING_MONTH_SECONDS - 2 * 24 * 60 * 60
    assert balance_kopecks(user) < PRICE_PER_DEVICE_MONTH_KOPECKS
    assert subscription.expires_at > now + timedelta(days=13)
    assert subscription.expires_at < now + timedelta(days=15)
