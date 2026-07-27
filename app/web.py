from __future__ import annotations

import hmac
import io
from datetime import datetime, timedelta, timezone
from typing import Annotated

import qrcode
from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from sqlalchemy import desc, select
from sqlalchemy.orm import Session, selectinload

from app.database import get_db
from app.models import (
    CredentialStatus,
    Payment,
    Subscription,
    SubscriptionStatus,
    User,
    UserRole,
    VpnCredential,
    utcnow,
)
from app.security import ConfigCipher, hash_password, new_csrf_token, normalize_email, verify_password
from app.services.amnezia_key import build_amnezia_vpn_key
from app.services.lifecycle import (
    BusinessRuleError,
    activate_payment,
    apply_verified_payment,
    as_utc,
    balance_kopecks,
    create_credential,
    create_balance_payment,
    delete_credential,
    delete_customer_account,
    ensure_first_credential,
    refresh_peer_stats,
    restore_suspended_credentials,
    revoke_credential,
    settle_subscription,
)


router = APIRouter()
Db = Annotated[Session, Depends(get_db)]


def _user(request: Request, db: Session) -> User | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    user = db.get(User, user_id)
    return user if user and user.is_active else None


def _require_user(request: Request, db: Session) -> User:
    user = _user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Требуется вход")
    return user


def _require_admin(request: Request, db: Session) -> User:
    user = _require_user(request, db)
    if user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Недостаточно прав")
    return user


def _csrf(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = new_csrf_token()
        request.session["csrf"] = token
    return token


def _check_csrf(request: Request, token: str) -> None:
    expected = request.session.get("csrf", "")
    if not expected or not hmac.compare_digest(expected, token):
        raise HTTPException(status_code=403, detail="Форма устарела. Обновите страницу")


def _render(request: Request, name: str, db: Session, **context: object) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request=request,
        name=name,
        context={
            "request": request,
            "current_user": _user(request, db),
            "csrf_token": _csrf(request),
            "app_name": request.app.state.settings.app_name,
            **context,
        },
    )


def _redirect_login() -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


def _format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if size < 1024 or unit == "ТБ":
            return f"{size:.0f} {unit}" if unit == "Б" else f"{size:.1f} {unit}"
        size /= 1024
    return str(value)


def _is_connected(value: datetime | None) -> bool:
    normalized = as_utc(value)
    return bool(normalized and normalized >= utcnow() - timedelta(minutes=3))


def _client_summary(user: User) -> dict[str, object]:
    subscriptions = sorted(user.subscriptions, key=lambda item: item.created_at, reverse=True)
    subscription = next(
        (item for item in subscriptions if item.status == SubscriptionStatus.ACTIVE),
        subscriptions[0] if subscriptions else None,
    )
    active_devices = sum(
        1 for item in user.credentials if item.status == CredentialStatus.ACTIVE
    )
    if not user.is_active:
        status, status_label, status_class = "disabled", "Отключён", "revoked"
    elif not subscription:
        status, status_label, status_class = "new", "Без подписки", ""
    elif subscription.status == SubscriptionStatus.ACTIVE and active_devices == 0:
        status, status_label, status_class = "paused", "Приостановлен", ""
    else:
        labels = {
            SubscriptionStatus.ACTIVE: ("active", "Активен", "online"),
            SubscriptionStatus.EXPIRED: ("expired", "Истёк", "revoked"),
            SubscriptionStatus.CANCELED: ("canceled", "Отменён", "revoked"),
            SubscriptionStatus.PENDING: ("pending", "Ожидает оплаты", ""),
        }
        status, status_label, status_class = labels[subscription.status]
    return {
        "user": user,
        "subscription": subscription,
        "balance_kopecks": balance_kopecks(user),
        "active_devices": active_devices,
        "status": status,
        "status_label": status_label,
        "status_class": status_class,
        "expires_at": subscription.expires_at if subscription else None,
    }


@router.get("/", response_class=HTMLResponse)
def home(request: Request, db: Db) -> Response:
    if _user(request, db):
        return RedirectResponse("/admin" if _user(request, db).role == UserRole.ADMIN else "/app", 303)
    return _render(request, "home.html", db, price_per_device=100)


@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request, db: Db) -> HTMLResponse:
    return _render(request, "register.html", db, error=None)


@router.post("/register", response_class=HTMLResponse)
def register(
    request: Request,
    db: Db,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()],
) -> Response:
    _check_csrf(request, csrf_token)
    normalized = normalize_email(email)
    if "@" not in normalized or len(normalized) > 320:
        return _render(request, "register.html", db, error="Укажите корректный email")
    if db.scalar(select(User).where(User.email == normalized)):
        return _render(request, "register.html", db, error="Такой пользователь уже существует")
    try:
        password_hash = hash_password(password)
    except ValueError as exc:
        return _render(request, "register.html", db, error=str(exc))
    user = User(email=normalized, password_hash=password_hash)
    db.add(user)
    db.commit()
    request.session.clear()
    request.session["user_id"] = user.id
    request.session["csrf"] = new_csrf_token()
    return RedirectResponse("/app", 303)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Db) -> HTMLResponse:
    return _render(request, "login.html", db, error=None)


@router.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    db: Db,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()],
) -> Response:
    _check_csrf(request, csrf_token)
    user = db.scalar(select(User).where(User.email == normalize_email(email)))
    if not user or not user.is_active or not verify_password(password, user.password_hash):
        return _render(request, "login.html", db, error="Неверный email или пароль")
    request.session.clear()
    request.session["user_id"] = user.id
    request.session["csrf"] = new_csrf_token()
    return RedirectResponse("/admin" if user.role == UserRole.ADMIN else "/app", 303)


@router.post("/logout")
def logout(request: Request, csrf_token: Annotated[str, Form()]) -> RedirectResponse:
    _check_csrf(request, csrf_token)
    request.session.clear()
    return RedirectResponse("/", 303)


@router.get("/app", response_class=HTMLResponse)
def customer_dashboard(request: Request, db: Db) -> Response:
    user = _user(request, db)
    if not user:
        return _redirect_login()
    if user.role == UserRole.ADMIN:
        return RedirectResponse("/admin", 303)
    subscriptions = list(
        db.scalars(
            select(Subscription)
            .where(Subscription.user_id == user.id)
            .options(selectinload(Subscription.credentials), selectinload(Subscription.user))
            .order_by(desc(Subscription.created_at))
        )
    )
    for subscription in subscriptions:
        if subscription.status == SubscriptionStatus.ACTIVE:
            settle_subscription(db, subscription)
    db.commit()
    payments = list(
        db.scalars(
            select(Payment).where(Payment.user_id == user.id).order_by(desc(Payment.created_at)).limit(10)
        )
    )
    resumable_subscription = next(
        (item for item in subscriptions if item.status == SubscriptionStatus.ACTIVE),
        next(
            (item for item in subscriptions if item.status == SubscriptionStatus.EXPIRED),
            None,
        ),
    )
    suspended_devices = (
        sum(
            1
            for credential in resumable_subscription.credentials
            if credential.status == CredentialStatus.SUSPENDED
        )
        if resumable_subscription
        else 0
    )
    return _render(
        request,
        "dashboard.html",
        db,
        subscriptions=subscriptions,
        payments=payments,
        balance_rubles=balance_kopecks(user) / 100,
        price_per_device=100,
        max_devices=request.app.state.settings.max_devices_per_subscription,
        now=utcnow(),
        is_connected=_is_connected,
        format_bytes=_format_bytes,
        suspended_devices=suspended_devices,
    )


@router.post("/balance/topup")
def topup_balance(
    request: Request,
    db: Db,
    amount_rubles: Annotated[int, Form()],
    csrf_token: Annotated[str, Form()],
) -> Response:
    _check_csrf(request, csrf_token)
    user = _user(request, db)
    if not user:
        return _redirect_login()
    payment = create_balance_payment(
        db,
        user=user,
        amount_rubles=amount_rubles,
        provider=request.app.state.payment_provider,
        return_url=f"{request.app.state.settings.base_url}/app",
    )
    return RedirectResponse(payment.confirmation_url or "/app", 303)


@router.get("/payments/mock/{payment_id}", response_class=HTMLResponse)
def mock_payment_page(request: Request, db: Db, payment_id: str) -> Response:
    if request.app.state.settings.payment_provider != "mock":
        raise HTTPException(404)
    user = _user(request, db)
    if not user:
        return _redirect_login()
    payment = db.get(Payment, payment_id)
    if not payment or payment.user_id != user.id:
        raise HTTPException(404, "Платеж не найден")
    return _render(request, "mock_payment.html", db, payment=payment)


@router.post("/payments/mock/{payment_id}/confirm")
def mock_payment_confirm(
    request: Request,
    db: Db,
    payment_id: str,
    csrf_token: Annotated[str, Form()],
) -> Response:
    if request.app.state.settings.payment_provider != "mock":
        raise HTTPException(404)
    _check_csrf(request, csrf_token)
    user = _require_user(request, db)
    payment = db.get(Payment, payment_id)
    if not payment or payment.user_id != user.id:
        raise HTTPException(404, "Платеж не найден")
    subscription = activate_payment(db, payment)
    restore_suspended_credentials(
        db,
        subscription=subscription,
        settings=request.app.state.settings,
        provisioner=request.app.state.provisioner,
    )
    ensure_first_credential(
        db,
        subscription=subscription,
        settings=request.app.state.settings,
        provisioner=request.app.state.provisioner,
    )
    return RedirectResponse("/app", 303)


@router.post("/webhooks/yookassa")
async def yookassa_webhook(request: Request, db: Db) -> JSONResponse:
    if request.app.state.settings.payment_provider != "yookassa":
        raise HTTPException(404)
    payload = await request.json()
    provider_id = payload.get("object", {}).get("id")
    if not isinstance(provider_id, str):
        raise HTTPException(400, "Invalid event")
    verified = request.app.state.payment_provider.verify(provider_id)
    subscription = apply_verified_payment(db, verified)
    if subscription:
        restore_suspended_credentials(
            db,
            subscription=subscription,
            settings=request.app.state.settings,
            provisioner=request.app.state.provisioner,
        )
        ensure_first_credential(
            db,
            subscription=subscription,
            settings=request.app.state.settings,
            provisioner=request.app.state.provisioner,
        )
    return JSONResponse({"ok": True})


@router.post("/app/devices")
def add_device(
    request: Request,
    db: Db,
    subscription_id: Annotated[str, Form()],
    label: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()],
) -> Response:
    _check_csrf(request, csrf_token)
    user = _require_user(request, db)
    subscription = db.get(Subscription, subscription_id)
    if not subscription or subscription.user_id != user.id:
        raise HTTPException(404, "Подписка не найдена")
    try:
        create_credential(
            db,
            subscription=subscription,
            label=label,
            settings=request.app.state.settings,
            provisioner=request.app.state.provisioner,
        )
    except BusinessRuleError as exc:
        raise HTTPException(409, str(exc)) from exc
    return RedirectResponse("/app", 303)


def _owned_credential(request: Request, db: Session, credential_id: str) -> VpnCredential:
    user = _require_user(request, db)
    credential = db.get(VpnCredential, credential_id)
    if not credential or (credential.user_id != user.id and user.role != UserRole.ADMIN):
        raise HTTPException(404, "Ключ не найден")
    return credential


def _require_active_credential(credential: VpnCredential) -> None:
    if credential.status == CredentialStatus.SUSPENDED:
        raise HTTPException(
            423,
            "Ключ временно приостановлен и снова заработает после пополнения баланса",
        )
    if credential.status != CredentialStatus.ACTIVE:
        raise HTTPException(410, "Ключ отозван")


@router.get("/app/devices/{credential_id}/config")
def download_config(request: Request, db: Db, credential_id: str) -> Response:
    credential = _owned_credential(request, db, credential_id)
    _require_active_credential(credential)
    config = ConfigCipher(request.app.state.settings).decrypt(credential.config_encrypted)
    safe_name = "amnezia-" + credential.id[:8] + ".conf"
    return Response(
        content=config,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/app/devices/{credential_id}/qr")
def credential_qr(request: Request, db: Db, credential_id: str) -> StreamingResponse:
    credential = _owned_credential(request, db, credential_id)
    _require_active_credential(credential)
    config = ConfigCipher(request.app.state.settings).decrypt(credential.config_encrypted)
    image = qrcode.make(config)
    output = io.BytesIO()
    image.save(output, format="PNG")
    output.seek(0)
    return StreamingResponse(
        output,
        media_type="image/png",
        headers={"Cache-Control": "no-store", "Content-Disposition": "inline"},
    )


@router.get("/app/devices/{credential_id}/key")
def credential_text_key(request: Request, db: Db, credential_id: str) -> Response:
    credential = _owned_credential(request, db, credential_id)
    _require_active_credential(credential)
    settings = request.app.state.settings
    config = ConfigCipher(settings).decrypt(credential.config_encrypted)
    key = build_amnezia_vpn_key(
        config=config,
        client_public_key=credential.public_key,
        label=credential.label,
        settings=settings,
    )
    return Response(
        content=key,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": "inline",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/app/devices/{credential_id}/revoke")
def customer_revoke(
    request: Request,
    db: Db,
    credential_id: str,
    csrf_token: Annotated[str, Form()],
) -> RedirectResponse:
    _check_csrf(request, csrf_token)
    credential = _owned_credential(request, db, credential_id)
    revoke_credential(db, credential, request.app.state.provisioner)
    return RedirectResponse("/admin" if _user(request, db).role == UserRole.ADMIN else "/app", 303)


@router.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request, db: Db) -> Response:
    user = _user(request, db)
    if not user:
        return _redirect_login()
    if user.role != UserRole.ADMIN:
        raise HTTPException(403)
    customers = list(
        db.scalars(
            select(User)
            .where(User.role == UserRole.CUSTOMER)
            .options(selectinload(User.subscriptions), selectinload(User.credentials))
            .order_by(desc(User.created_at))
        )
    )
    for customer in customers:
        for subscription in customer.subscriptions:
            if subscription.status == SubscriptionStatus.ACTIVE:
                settle_subscription(db, subscription)
    db.commit()
    return _render(
        request,
        "admin.html",
        db,
        clients=[_client_summary(customer) for customer in customers],
    )


@router.get("/admin/clients/{user_id}", response_class=HTMLResponse)
def admin_client(request: Request, db: Db, user_id: str) -> Response:
    _require_admin(request, db)
    customer = db.scalar(
        select(User)
        .where(User.id == user_id, User.role == UserRole.CUSTOMER)
        .options(selectinload(User.subscriptions), selectinload(User.credentials))
    )
    if not customer:
        raise HTTPException(404, detail="Клиент не найден")
    for subscription in customer.subscriptions:
        if subscription.status == SubscriptionStatus.ACTIVE:
            settle_subscription(db, subscription)
    db.commit()
    return _render(
        request,
        "admin_client.html",
        db,
        client=_client_summary(customer),
        credentials=sorted(customer.credentials, key=lambda item: item.created_at, reverse=True),
        format_bytes=_format_bytes,
    )


@router.post("/admin/devices/{credential_id}/delete")
def admin_delete_device(
    request: Request,
    db: Db,
    credential_id: str,
    csrf_token: Annotated[str, Form()],
) -> RedirectResponse:
    _check_csrf(request, csrf_token)
    _require_admin(request, db)
    credential = db.get(VpnCredential, credential_id)
    if not credential:
        raise HTTPException(404, detail="Устройство не найдено")
    user_id = credential.user_id
    delete_credential(db, credential, request.app.state.provisioner)
    return RedirectResponse(f"/admin/clients/{user_id}", 303)


@router.post("/admin/clients/{user_id}/delete")
def admin_delete_client(
    request: Request,
    db: Db,
    user_id: str,
    csrf_token: Annotated[str, Form()],
) -> RedirectResponse:
    _check_csrf(request, csrf_token)
    _require_admin(request, db)
    customer = db.scalar(
        select(User).where(User.id == user_id, User.role == UserRole.CUSTOMER)
    )
    if not customer:
        raise HTTPException(404, detail="Клиент не найден")
    delete_customer_account(db, customer, request.app.state.provisioner)
    return RedirectResponse("/admin", 303)


@router.post("/admin/refresh")
def admin_refresh(
    request: Request, db: Db, csrf_token: Annotated[str, Form()]
) -> RedirectResponse:
    _check_csrf(request, csrf_token)
    _require_admin(request, db)
    refresh_peer_stats(db, request.app.state.provisioner)
    return RedirectResponse("/admin", 303)


@router.get("/api/v1/admin/clients")
def admin_clients_api(request: Request, db: Db) -> JSONResponse:
    _require_admin(request, db)
    customers = db.scalars(
        select(User)
        .where(User.role == UserRole.CUSTOMER)
        .options(selectinload(User.subscriptions), selectinload(User.credentials))
        .order_by(desc(User.created_at))
    ).all()
    clients = [_client_summary(customer) for customer in customers]
    return JSONResponse(
        [
            {
                "id": item["user"].id,
                "email": item["user"].email,
                "balance_kopecks": item["balance_kopecks"],
                "active_devices": item["active_devices"],
                "status": item["status"],
                "expires_at": (
                    as_utc(item["expires_at"]).isoformat()
                    if item["expires_at"]
                    else None
                ),
            }
            for item in clients
        ]
    )


@router.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
