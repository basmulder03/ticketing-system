"""ORM model registry.

Import this module (rather than individual model modules) wherever "every
model" needs to be registered against ``Base.metadata`` — e.g.
``alembic/env.py`` for autogenerate. Each model module registers itself as
a side effect of being imported here.
"""

from app.models.admin_user import AdminUser
from app.models.agent_account import AgentAccount
from app.models.audit_log import AuditLogEntry
from app.models.enums import (
    ActorType,
    AdminRole,
    OrderStatus,
    PaymentMethod,
    PublishStatus,
    SmtpEncryptionMode,
    ThemeFont,
)
from app.models.event import Event
from app.models.event_config import EventConfig
from app.models.order import Order
from app.models.show import Show
from app.models.theme import Theme
from app.models.ticket import Ticket
from app.models.ticket_type import TicketType

__all__ = [
    "ActorType",
    "AdminRole",
    "AdminUser",
    "AgentAccount",
    "AuditLogEntry",
    "Event",
    "EventConfig",
    "Order",
    "OrderStatus",
    "PaymentMethod",
    "PublishStatus",
    "Show",
    "SmtpEncryptionMode",
    "Theme",
    "ThemeFont",
    "Ticket",
    "TicketType",
]
