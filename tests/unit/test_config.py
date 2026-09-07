"""Unit tests for ``Settings.model_post_init``'s insecure-default-secret guard.

Settings is instantiated directly with explicit kwargs in every test here
(pydantic-settings gives explicit init kwargs top priority over `.env`/env
vars — see pydantic-settings' source-order docs), so these tests don't
depend on, and don't mutate, the process's real environment or the
``get_settings()`` lru_cache singleton.
"""

import pytest

from app.core.config import (
    _INSECURE_ENCRYPTION_KEY,
    _INSECURE_SECRET_KEY,
    InsecureDefaultSecretError,
    Settings,
)


def test_raises_outside_development_when_secret_key_is_still_the_default() -> None:
    with pytest.raises(InsecureDefaultSecretError):
        Settings(
            app_env="production",
            secret_key=_INSECURE_SECRET_KEY,
            encryption_key="a-real-generated-encryption-key",
        )


def test_raises_outside_development_when_encryption_key_is_still_the_default() -> None:
    with pytest.raises(InsecureDefaultSecretError):
        Settings(
            app_env="production",
            secret_key="a-real-generated-secret-key",
            encryption_key=_INSECURE_ENCRYPTION_KEY,
        )


def test_raises_in_staging_too_not_just_production() -> None:
    with pytest.raises(InsecureDefaultSecretError):
        Settings(app_env="staging", secret_key=_INSECURE_SECRET_KEY, encryption_key=_INSECURE_ENCRYPTION_KEY)


def test_does_not_raise_in_development_even_with_both_keys_at_their_defaults() -> None:
    settings = Settings(
        app_env="development", secret_key=_INSECURE_SECRET_KEY, encryption_key=_INSECURE_ENCRYPTION_KEY
    )
    assert settings.secret_key == _INSECURE_SECRET_KEY
    assert settings.encryption_key == _INSECURE_ENCRYPTION_KEY


def test_does_not_raise_outside_development_when_both_keys_are_overridden() -> None:
    settings = Settings(
        app_env="production",
        secret_key="a-real-generated-secret-key",
        encryption_key="a-real-generated-encryption-key",
    )
    assert settings.app_env == "production"
