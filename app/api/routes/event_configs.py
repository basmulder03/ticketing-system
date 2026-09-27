"""EventConfig routes: get/upsert, SMTP/Mollie connection tests, and copying
config from another event. Admin-only — agents must never touch credentials
or financial settings.
"""

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, require_admin
from app.api.routes._utils import apply_partial_update, parse_uuid_or_404
from app.db.session import get_session
from app.models.enums import MollieMode, SmtpEncryptionMode
from app.models.event import Event
from app.models.event_config import EventConfig
from app.schemas.event_config import (
    ConnectionTestResult,
    EventConfigOut,
    EventConfigUpdateRequest,
    TestEmailRequest,
    TestMollieKeyRequest,
)
from app.services.audit import record_audit_entry
from app.services.event_config_actions import send_test_email, verify_mollie_api_key

router = APIRouter(prefix="/api/v1/events/{event_id}/config", tags=["admin", "event-config"])

_COPYABLE_FIELDS = (
    "smtp_host",
    "smtp_port",
    "smtp_encryption",
    "smtp_username",
    "smtp_password",
    "sender_name",
    "sender_email",
    "mollie_test_api_key",
    "mollie_live_api_key",
    "mollie_mode",
    "invoice_company_name",
    "invoice_company_address",
    "invoice_company_vat_number",
    "invoice_number_prefix",
    "service_fee_amount",
    "enabled_payment_methods",
)
"""Fields copied between events. Not ``sales_live_at``: it's specific to one event's timeline."""


def _to_out(config: EventConfig) -> EventConfigOut:
    return EventConfigOut(
        id=str(config.id),
        event_id=str(config.event_id),
        smtp_host=config.smtp_host,
        smtp_port=config.smtp_port,
        smtp_encryption=config.smtp_encryption,
        smtp_username=config.smtp_username,
        smtp_password_is_set=bool(config.smtp_password),
        sender_name=config.sender_name,
        sender_email=config.sender_email,
        mollie_test_api_key_is_set=bool(config.mollie_test_api_key),
        mollie_live_api_key_is_set=bool(config.mollie_live_api_key),
        mollie_mode=config.mollie_mode,
        invoice_company_name=config.invoice_company_name,
        invoice_company_address=config.invoice_company_address,
        invoice_company_vat_number=config.invoice_company_vat_number,
        invoice_number_prefix=config.invoice_number_prefix,
        service_fee_amount=config.service_fee_amount,
        sales_live_at=config.sales_live_at,
        enabled_payment_methods=list(config.enabled_payment_methods),
        created_at=config.created_at,
        updated_at=config.updated_at,
    )


async def _get_event_or_404(session: AsyncSession, event_id: str) -> Event:
    parsed_id = parse_uuid_or_404(event_id, detail="Event not found.")
    event = await session.get(Event, parsed_id)
    if event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found.")
    return event


async def _get_config_or_404(session: AsyncSession, event: Event) -> EventConfig:
    result = await session.execute(select(EventConfig).where(EventConfig.event_id == event.id))
    config = result.scalar_one_or_none()
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="This event has no configuration yet. PUT to this endpoint to create one.",
        )
    return config


@router.get("")
async def get_event_config(
    event_id: str,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> EventConfigOut:
    """The event's config, without decrypted secrets."""
    event = await _get_event_or_404(session, event_id)
    config = await _get_config_or_404(session, event)
    return _to_out(config)


@router.put("")
async def upsert_event_config(
    event_id: str,
    body: EventConfigUpdateRequest,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> EventConfigOut:
    """Create or update the config; omitted fields stay unchanged. An explicit
    ``null`` for a NOT NULL field (``smtp_encryption``, ``enabled_payment_methods``,
    ``mollie_mode``) resets it to its default instead of failing.
    """
    event = await _get_event_or_404(session, event_id)
    result = await session.execute(select(EventConfig).where(EventConfig.event_id == event.id))
    config = result.scalar_one_or_none()
    created = config is None
    if config is None:
        config = EventConfig(event_id=event.id)
        session.add(config)

    changes = apply_partial_update(config, body)
    if changes.get("smtp_encryption") is None and "smtp_encryption" in changes:
        config.smtp_encryption = SmtpEncryptionMode.NONE
        changes["smtp_encryption"] = SmtpEncryptionMode.NONE.value
    if changes.get("enabled_payment_methods") is None and "enabled_payment_methods" in changes:
        config.enabled_payment_methods = []
        changes["enabled_payment_methods"] = []
    if changes.get("mollie_mode") is None and "mollie_mode" in changes:
        # NOT NULL: an explicit null resets to the safe default (TEST).
        config.mollie_mode = MollieMode.TEST
        changes["mollie_mode"] = MollieMode.TEST.value
    if changes.get("service_fee_amount") is None and "service_fee_amount" in changes:
        # NOT NULL: an explicit null resets it to no fee, rather than 500ing.
        config.service_fee_amount = Decimal("0.00")
        changes["service_fee_amount"] = "0.00"
    # Never audit secret plaintext. Other values are stringified because the
    # JSON column can't encode sales_live_at's datetime.
    redacted_changes = {
        k: ("<redacted>" if k in ("smtp_password", "mollie_test_api_key", "mollie_live_api_key") else str(v))
        for k, v in changes.items()
    }
    await session.flush()
    await record_audit_entry(
        session,
        principal,
        action="event_config.create" if created else "event_config.update",
        target_type="EventConfig",
        target_id=str(config.id),
        detail={"event_id": str(event.id), **redacted_changes},
    )
    await session.commit()
    await session.refresh(config)
    return _to_out(config)


@router.post("/test-email")
async def test_email(
    event_id: str,
    body: TestEmailRequest,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ConnectionTestResult:
    """Send a test email via the event's SMTP settings. Diagnostic only, not audited."""
    event = await _get_event_or_404(session, event_id)
    config = await _get_config_or_404(session, event)
    return await send_test_email(config, body.recipient)


@router.post("/test-mollie")
async def test_mollie(
    event_id: str,
    body: TestMollieKeyRequest,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ConnectionTestResult:
    """Verify the event's test or live Mollie key. Diagnostic only, not audited."""
    event = await _get_event_or_404(session, event_id)
    config = await _get_config_or_404(session, event)
    api_key = config.mollie_live_api_key if body.environment == "live" else config.mollie_test_api_key
    return await verify_mollie_api_key(api_key or "")


@router.post("/copy-from/{source_event_id}")
async def copy_event_config(
    event_id: str,
    source_event_id: str,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> EventConfigOut:
    """Copy another event's config onto this one (creating it if needed), except ``sales_live_at``."""
    target_event = await _get_event_or_404(session, event_id)
    source_event = await _get_event_or_404(session, source_event_id)
    if target_event.id == source_event.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot copy an event's config onto itself."
        )

    source_config = await _get_config_or_404(session, source_event)

    result = await session.execute(select(EventConfig).where(EventConfig.event_id == target_event.id))
    target_config = result.scalar_one_or_none()
    created = target_config is None
    if target_config is None:
        target_config = EventConfig(event_id=target_event.id)
        session.add(target_config)

    for field in _COPYABLE_FIELDS:
        value = getattr(source_config, field)
        setattr(target_config, field, list(value) if isinstance(value, list) else value)

    await session.flush()
    await record_audit_entry(
        session,
        principal,
        action="event_config.copy_from",
        target_type="EventConfig",
        target_id=str(target_config.id),
        detail={
            "target_event_id": str(target_event.id),
            "source_event_id": str(source_event.id),
            "created": created,
        },
    )
    await session.commit()
    await session.refresh(target_config)
    return _to_out(target_config)
