"""okta_oidc provider tests (req-tap-auth-okta-oidc).

Pure claim and config checks plus the discovery self-test against a mocked
issuer; no real Okta tenant.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import jsonschema
import pytest
import requests

from tap.secret_naming import SECRET_SUFFIX
from tap_auth.errors import AccountNotAllowlisted, DomainNotAllowed, EmailNotVerified
from tap_auth.providers import ProviderConfig, SelfTestStatus, build_socialaccount_providers, get_provider
from tap_auth.providers.okta_oidc import OktaOidcProvider

ISSUER = "https://example.okta.com/oauth2/default"
SCHEMA = Path(__file__).resolve().parents[1] / "schemas" / "auth-boot-section.schema.json"


def _cfg(**over) -> ProviderConfig:
    raw = {
        "id": "example-okta",
        "type": "okta_oidc",
        "display_name": "Example (Okta)",
        "issuer": ISSUER,
        "allowed_domains": ["example.com"],
    }
    raw.update(over)
    return ProviderConfig.from_dict(raw)


def _write_secret(root: Path, key: str) -> None:
    doc = {
        "scope": "auth",
        "key": key,
        "kind": "oidc_client",
        "description": "test okta client",
        "data": {"client_id": "0oaexampleclient", "client_secret": "okta-secret"},
    }
    (root / f"{key}{SECRET_SUFFIX}").write_text(json.dumps(doc))


def _claims(**over) -> dict:
    claims = {"sub": "00u1", "email": "Dev@Example.com", "email_verified": True, "name": "Dev One"}
    claims.update(over)
    return claims


def _checks(results) -> dict:
    return {r.check: r.status for r in results}


@pytest.fixture
def base_url(settings):
    settings.TAP_BASE_URL = "https://tap.stage.example"


class TestValidate:
    def test_registered(self):
        assert get_provider("okta_oidc").type == "okta_oidc"

    def test_valid_config_passes(self, base_url):
        checks = _checks(OktaOidcProvider().validate_config(_cfg()))
        assert checks["issuer"] is SelfTestStatus.PASS
        assert checks["allowed_domains"] is SelfTestStatus.PASS
        assert checks["tap_base_url"] is SelfTestStatus.PASS

    def test_missing_issuer_fails(self, base_url):
        assert _checks(OktaOidcProvider().validate_config(_cfg(issuer="")))["issuer"] is SelfTestStatus.FAIL

    def test_http_issuer_fails(self, base_url):
        checks = _checks(OktaOidcProvider().validate_config(_cfg(issuer="http://example.okta.com")))
        assert checks["issuer"] is SelfTestStatus.FAIL

    def test_custom_domain_warns(self, base_url):
        checks = _checks(OktaOidcProvider().validate_config(_cfg(issuer="https://login.example.com")))
        assert checks["issuer"] is SelfTestStatus.WARN

    def test_missing_allowed_domains_fails(self, base_url):
        checks = _checks(OktaOidcProvider().validate_config(_cfg(allowed_domains=[])))
        assert checks["allowed_domains"] is SelfTestStatus.FAIL

    def test_callback_url(self, base_url):
        assert OktaOidcProvider().callback_url(_cfg()) == "https://tap.stage.example/auth/oidc/example-okta/login/callback/"


class TestEvaluateAccess:
    def test_verified_email_in_domain_allowed(self):
        d = OktaOidcProvider().evaluate_access(_cfg(), _claims())
        assert d.allowed and d.verified_email == "dev@example.com" and d.matched_rule == "allowed_domains"

    def test_unverified_email_denied(self):
        d = OktaOidcProvider().evaluate_access(_cfg(), _claims(email_verified=False))
        assert not d.allowed and d.reason == EmailNotVerified.reason and d.verified_email is None

    def test_string_verified_flag(self):
        assert OktaOidcProvider().evaluate_access(_cfg(), _claims(email_verified="true")).allowed

    def test_other_domain_denied(self):
        d = OktaOidcProvider().evaluate_access(_cfg(), _claims(email="dev@evil.example"))
        assert not d.allowed and d.reason == DomainNotAllowed.reason

    def test_subdomain_is_not_the_domain(self):
        assert not OktaOidcProvider().evaluate_access(_cfg(), _claims(email="dev@sub.example.com")).allowed

    def test_email_allowlist(self):
        cfg = _cfg(allowed_emails=["ops@example.com"])
        assert OktaOidcProvider().evaluate_access(cfg, _claims(email="ops@example.com")).allowed
        d = OktaOidcProvider().evaluate_access(cfg, _claims())
        assert not d.allowed and d.reason == AccountNotAllowlisted.reason

    def test_group_pin(self):
        cfg = _cfg(allowed_groups=["tap-stage-users"])
        ok = OktaOidcProvider().evaluate_access(cfg, _claims(groups=["Everyone", "tap-stage-users"]))
        assert ok.allowed and ok.matched_rule == "allowed_groups"
        d = OktaOidcProvider().evaluate_access(cfg, _claims(groups=["Everyone"]))
        assert not d.allowed and d.matched_rule == "allowed_groups"

    def test_group_pin_without_claim_denied(self):
        d = OktaOidcProvider().evaluate_access(_cfg(allowed_groups=["tap-stage-users"]), _claims())
        assert not d.allowed and d.reason == AccountNotAllowlisted.reason

    def test_group_claim_must_be_a_list(self):
        cfg = _cfg(allowed_groups=["tap-stage-users"])
        assert not OktaOidcProvider().evaluate_access(cfg, _claims(groups="tap-stage-users")).allowed


class TestSelfTestAndWiring:
    def test_discovery_pass(self, tmp_path, settings, base_url):
        settings.TAP_SECRETS_ROOT = str(tmp_path)
        _write_secret(tmp_path, "example-okta")
        cfg = _cfg()
        provider = OktaOidcProvider()
        doc = {"issuer": ISSUER, "authorization_endpoint": "a", "token_endpoint": "t", "jwks_uri": "j"}
        resp = mock.Mock(status_code=200, json=mock.Mock(return_value=doc))
        with mock.patch("tap_auth.providers.okta_oidc.requests.get", return_value=resp):
            checks = _checks(provider.self_test(cfg, provider.resolve_secrets(cfg), live=True))
        assert checks["secret"] is SelfTestStatus.PASS
        assert checks["discovery"] is SelfTestStatus.PASS

    def test_discovery_issuer_mismatch_fails(self, base_url):
        doc = {"issuer": "https://other.okta.com", "authorization_endpoint": "a", "token_endpoint": "t", "jwks_uri": "j"}
        resp = mock.Mock(status_code=200, json=mock.Mock(return_value=doc))
        with mock.patch("tap_auth.providers.okta_oidc.requests.get", return_value=resp):
            result = OktaOidcProvider()._discovery_check(_cfg(), live=True)
        assert result.status is SelfTestStatus.FAIL

    def test_discovery_unreachable_fails(self, base_url):
        with mock.patch("tap_auth.providers.okta_oidc.requests.get", side_effect=requests.ConnectionError("down")):
            assert OktaOidcProvider()._discovery_check(_cfg(), live=True).status is SelfTestStatus.FAIL

    def test_allauth_entry_uses_pkce(self, tmp_path, settings):
        settings.TAP_SECRETS_ROOT = str(tmp_path)
        _write_secret(tmp_path, "example-okta")
        raw = {"id": "example-okta", "type": "okta_oidc", "display_name": "Okta", "issuer": ISSUER, "allowed_domains": ["example.com"]}
        apps = build_socialaccount_providers([raw])["openid_connect"]["APPS"]
        assert apps[0]["provider_id"] == "example-okta"
        assert apps[0]["settings"] == {"server_url": ISSUER, "oauth_pkce_enabled": True}

    def test_group_pin_reads_the_id_token(self, tmp_path, settings):
        settings.TAP_SECRETS_ROOT = str(tmp_path)
        _write_secret(tmp_path, "example-okta")
        cfg = _cfg(allowed_groups=["tap-stage-users"])
        provider = OktaOidcProvider()
        entry = provider.build_allauth_settings(cfg, provider.resolve_secrets(cfg))
        assert entry["settings"]["fetch_userinfo"] is False


class TestBootSchema:
    def _section(self, provider: dict) -> dict:
        return {"description": "stage", "providers": [provider]}

    def _provider(self, **over) -> dict:
        p = {"id": "example-okta", "type": "okta_oidc", "display_name": "Example Okta", "description": "stage SSO", "issuer": ISSUER, "allowed_domains": ["example.com"]}
        p.update(over)
        return p

    def test_okta_entry_valid(self):
        jsonschema.validate(self._section(self._provider(allowed_groups=["tap-stage-users"])), json.loads(SCHEMA.read_text()))

    def test_okta_requires_issuer(self):
        p = self._provider()
        del p["issuer"]
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(self._section(p), json.loads(SCHEMA.read_text()))

    def test_okta_refuses_google_fields(self):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(self._section(self._provider(email_domain_fallback=True)), json.loads(SCHEMA.read_text()))

    def test_google_refuses_okta_fields(self):
        p = {"id": "g", "type": "google_oidc", "display_name": "G", "description": "g", "allowed_domains": ["x.com"], "allowed_groups": ["a"]}
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(self._section(p), json.loads(SCHEMA.read_text()))
