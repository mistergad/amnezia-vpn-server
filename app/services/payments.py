from __future__ import annotations

import secrets
from dataclasses import dataclass
from decimal import Decimal

import httpx

from app.config import Settings
from app.models import Payment


class PaymentProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class PaymentConfirmation:
    provider_payment_id: str
    confirmation_url: str
    status: str


@dataclass(frozen=True)
class VerifiedPayment:
    provider_payment_id: str
    status: str
    amount_kopecks: int
    currency: str
    internal_payment_id: str


class PaymentProvider:
    name: str

    def create(self, payment: Payment, return_url: str) -> PaymentConfirmation:
        raise NotImplementedError

    def verify(self, provider_payment_id: str) -> VerifiedPayment:
        raise NotImplementedError


class MockPaymentProvider(PaymentProvider):
    name = "mock"

    def __init__(self, settings: Settings):
        self.settings = settings

    def create(self, payment: Payment, return_url: str) -> PaymentConfirmation:
        provider_id = "mock_" + secrets.token_urlsafe(12)
        return PaymentConfirmation(
            provider_payment_id=provider_id,
            confirmation_url=f"/payments/mock/{payment.id}",
            status="pending",
        )

    def verify(self, provider_payment_id: str) -> VerifiedPayment:
        raise PaymentProviderError("Mock payments are confirmed by the local development route")


class YooKassaPaymentProvider(PaymentProvider):
    name = "yookassa"
    api_url = "https://api.yookassa.ru/v3/payments"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.auth = (settings.yookassa_shop_id or "", settings.yookassa_secret_key or "")

    def create(self, payment: Payment, return_url: str) -> PaymentConfirmation:
        payload = {
            "amount": {
                "value": f"{Decimal(payment.amount_kopecks) / Decimal(100):.2f}",
                "currency": payment.currency,
            },
            "capture": True,
            "confirmation": {"type": "redirect", "return_url": return_url},
            "description": f"VPN subscription: {payment.plan.name}"[:128],
            "metadata": {"internal_payment_id": payment.id},
        }
        try:
            response = httpx.post(
                self.api_url,
                json=payload,
                auth=self.auth,
                headers={"Idempotence-Key": payment.idempotency_key},
                timeout=15,
            )
            response.raise_for_status()
            data = response.json()
            return PaymentConfirmation(
                provider_payment_id=data["id"],
                confirmation_url=data["confirmation"]["confirmation_url"],
                status=data["status"],
            )
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise PaymentProviderError("YooKassa did not create the payment") from exc

    def verify(self, provider_payment_id: str) -> VerifiedPayment:
        try:
            response = httpx.get(
                f"{self.api_url}/{provider_payment_id}", auth=self.auth, timeout=15
            )
            response.raise_for_status()
            data = response.json()
            value = Decimal(data["amount"]["value"])
            return VerifiedPayment(
                provider_payment_id=data["id"],
                status=data["status"],
                amount_kopecks=int(value * 100),
                currency=data["amount"]["currency"],
                internal_payment_id=data.get("metadata", {}).get("internal_payment_id", ""),
            )
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise PaymentProviderError("YooKassa payment could not be verified") from exc


def build_payment_provider(settings: Settings) -> PaymentProvider:
    if settings.payment_provider == "mock":
        return MockPaymentProvider(settings)
    if settings.payment_provider == "yookassa":
        return YooKassaPaymentProvider(settings)
    raise ValueError(f"Unsupported PAYMENT_PROVIDER: {settings.payment_provider}")
