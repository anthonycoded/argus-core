import pytest

from fastapi import HTTPException

from app.core.auth import AuthenticatedApplication
from app.core.permissions import require_scopes


class FakeApplication:
    id = "test-app"
    slug = "argus"


class FakeAPIKey:
    id = "test-key"
    scopes = ["sources:read"]


@pytest.fixture
def authenticated_app():
    return AuthenticatedApplication(
        application=FakeApplication(),
        api_key=FakeAPIKey(),
    )


def test_allow_required_scope(authenticated_app):
    dependency = require_scopes("sources:read")

    result = dependency(auth=authenticated_app)

    assert result == authenticated_app


def test_reject_missing_scope(authenticated_app):
    dependency = require_scopes("sources:write")

    with pytest.raises(HTTPException) as exc:
        dependency(auth=authenticated_app)

    assert exc.value.status_code == 403


def test_allow_multiple_required_scopes():
    auth = AuthenticatedApplication(
        application=FakeApplication(),
        api_key=type(
            "APIKey",
            (),
            {"scopes": ["sources:read", "sources:write"]},
        )(),
    )

    dependency = require_scopes(
        "sources:read",
        "sources:write",
    )

    assert dependency(auth=auth) == auth


def test_reject_when_one_required_scope_is_missing(authenticated_app):
    dependency = require_scopes(
        "sources:read",
        "sources:write",
    )

    with pytest.raises(HTTPException) as exc:
        dependency(auth=authenticated_app)

    assert exc.value.status_code == 403


def test_empty_required_scopes_allow_access(authenticated_app):
    dependency = require_scopes()

    assert dependency(auth=authenticated_app) == authenticated_app