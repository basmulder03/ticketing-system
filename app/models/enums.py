"""Shared enum types for auth/audit models."""

import enum


class AdminRole(str, enum.Enum):
    """Roles for session-authenticated human ``AdminUser`` accounts.

    Only ``admin`` and ``scanner`` are represented here, even though
    PROJECT_BRIEF.md lists three conceptual roles (Admin/Scanner/Agent).
    "Agent" is deliberately NOT an ``AdminRole`` value: agents authenticate
    via a separate API-key path (``AgentAccount``, see
    ``app/models/agent_account.py``), not password + session login, so
    modeling it as an ``AdminUser`` row with a role flag would blur two
    structurally different auth mechanisms and make it easy to accidentally
    grant an agent session-login capability it should never have.
    """

    ADMIN = "admin"
    SCANNER = "scanner"


class ActorType(str, enum.Enum):
    """Distinguishes human admins from AI agents in the audit log.

    Every audit entry MUST use one of these two values — never a generic
    "system" actor — per PROJECT_BRIEF.md's AI/Agent Access requirement.
    """

    HUMAN = "human"
    AI_AGENT = "ai_agent"
