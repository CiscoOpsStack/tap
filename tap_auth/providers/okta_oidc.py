"""Okta OIDC provider (req-tap-auth-okta-oidc).

Rides allauth's ``openid_connect`` engine, like ``google_oidc``: each TAP provider
is one Okta OIDC web app addressed by its stable ``provider_id``. The difference
is the access policy. Okta returns no hosted-domain claim, so the domain is read
from the IdP-asserted **verified** email, and a deployment can additionally pin
login to Okta group membership.

Config fields (under the provider entry's type-specific ``config``):
    issuer            str        REQUIRED — the Okta authorization server, e.g.
                                 ``https://example.okta.com`` (org server) or
                                 ``https://example.okta.com/oauth2/default``.
                                 Must be https.
    allowed_domains   list[str]  REQUIRED — verified-email domains permitted to
                                 log in. No "any Okta user" escape hatch.
    allowed_emails    list[str]  OPTIONAL — narrow to specific verified emails
                                 within allowed_domains.
    allowed_groups    list[str]  OPTIONAL — require membership in at least one of
                                 these Okta groups, read from the ``groups`` claim
                                 of the signed ID token. The Okta app must emit
                                 that claim (Sign On tab, groups claim filter).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlparse

import requests
from django.conf import settings

from tap_auth.errors import AccountNotAllowlisted, DomainNotAllowed, EmailNotVerified
from tap_auth.providers.base import (
    AccessDecision,
    ProfileSnapshot,
    ProviderConfig,
    ProviderError,
    SelfTestPhase,
    SelfTestResult,
    SelfTestStatus,
)
from tap_auth.providers.secrets import resolve_oauth_client_secret, secret_exists

PROVIDER_TYPE = "okta_oidc"
DISCOVERY_PATH = "/.well-known/openid-configuration"
DOCS_URL = "spec-tap-auth-v0 § req-tap-auth-okta-oidc"
OKTA_HOST_SUFFIXES = (".okta.com", ".oktapreview.com", ".okta-gov.com", ".okta.mil", ".okta-emea.com")

_REQUIRED_DISCOVERY_KEYS = ("issuer", "authorization_endpoint", "token_endpoint", "jwks_uri")


def _result(check: str, status: SelfTestStatus, phase: SelfTestPhase, message: str) -> SelfTestResult:
    return SelfTestResult(check=check, status=status, phase=phase, message=message, docs_url=DOCS_URL)


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() == "true"


def _names(raw: Any) -> list[str]:
    return [str(v).strip() for v in (raw or []) if str(v).strip()]


class OktaOidcProvider:
    """okta_oidc provider implementation (see module docstring)."""

    type = PROVIDER_TYPE
    allauth_provider = "openid_connect"

    def _issuer(self, config: ProviderConfig) -> str:
        return str(config.config.get("issuer") or "").rstrip("/")

    def _allowed_domains(self, config: ProviderConfig) -> Sequence[str]:
        return [d.lower() for d in _names(config.config.get("allowed_domains"))]

    def _allowed_groups(self, config: ProviderConfig) -> Sequence[str]:
        return _names(config.config.get("allowed_groups"))

    def callback_url(self, config: ProviderConfig) -> str | None:
        base = getattr(settings, "TAP_BASE_URL", "") or ""
        if not base:
            return None
        return f"{base.rstrip('/')}/auth/oidc/{config.id}/login/callback/"

    def validate_config(self, config: ProviderConfig) -> list[SelfTestResult]:
        results: list[SelfTestResult] = []
        off = SelfTestPhase.OFFLINE

        if config.type != self.type:
            results.append(_result("type", SelfTestStatus.FAIL, off, f"expected type '{self.type}', got '{config.type}'"))
            return results

        issuer = self._issuer(config)
        parsed = urlparse(issuer)
        if not issuer:
            results.append(_result("issuer", SelfTestStatus.FAIL, off, "okta_oidc requires an issuer URL"))
        elif parsed.scheme != "https" or not parsed.hostname:
            results.append(_result("issuer", SelfTestStatus.FAIL, off, f"issuer must be an https URL, got {issuer!r}"))
        elif not parsed.hostname.endswith(OKTA_HOST_SUFFIXES):
            results.append(
                _result("issuer", SelfTestStatus.WARN, off, f"{parsed.hostname} is not an Okta-hosted domain; fine for a custom domain")
            )
        else:
            results.append(_result("issuer", SelfTestStatus.PASS, off, issuer))

        domains = self._allowed_domains(config)
        if not domains:
            results.append(
                _result(
                    "allowed_domains",
                    SelfTestStatus.FAIL,
                    off,
                    "okta_oidc requires a non-empty allowed_domains — there is no 'any Okta user' login.",
                )
            )
        else:
            results.append(_result("allowed_domains", SelfTestStatus.PASS, off, f"{len(domains)} domain(s): {', '.join(domains)}"))

        bad = [e for e in _names(config.config.get("allowed_emails")) if "@" not in e]
        if bad:
            results.append(_result("allowed_emails", SelfTestStatus.WARN, off, f"entries without '@': {bad}"))

        groups = self._allowed_groups(config)
        if groups:
            results.append(_result("allowed_groups", SelfTestStatus.PASS, off, f"login requires one of {len(groups)} Okta group(s)"))

        callback = self.callback_url(config)
        if callback is None:
            results.append(
                _result("tap_base_url", SelfTestStatus.FAIL, off, "TAP_BASE_URL is required to derive the OIDC callback URL for an external provider.")
            )
        else:
            results.append(_result("tap_base_url", SelfTestStatus.PASS, off, f"callback: {callback}"))
        return results

    def resolve_secrets(self, config: ProviderConfig) -> dict[str, str]:
        return resolve_oauth_client_secret(config.secret_key)

    def evaluate_access(self, config: ProviderConfig, claims: Mapping[str, Any]) -> AccessDecision:
        """Pure, claim-only. Each check fails closed with a distinct reason:
        verified email, then verified-email domain, then the email allowlist,
        then Okta group membership."""
        email = str(claims.get("email") or "").strip().lower()
        if not email or not _truthy(claims.get("email_verified")):
            return AccessDecision(
                allowed=False,
                reason=EmailNotVerified.reason,
                user_message="Your identity provider did not confirm a verified email address.",
                log_detail=f"email_verified={claims.get('email_verified')!r} email_present={bool(email)}",
                matched_rule="email_verified",
            )

        domain = email.rsplit("@", 1)[-1]
        allowed_domains = set(self._allowed_domains(config))
        if domain not in allowed_domains:
            return AccessDecision(
                allowed=False,
                reason=DomainNotAllowed.reason,
                user_message="Your account's domain is not permitted on this deployment.",
                log_detail=f"domain {domain!r} not in allowed_domains {sorted(allowed_domains)}",
                verified_email=email,
                matched_rule="allowed_domains",
            )

        allowed_emails = {e.lower() for e in _names(config.config.get("allowed_emails"))}
        if allowed_emails and email not in allowed_emails:
            return AccessDecision(
                allowed=False,
                reason=AccountNotAllowlisted.reason,
                user_message=(
                    "You authenticated correctly and your domain is allowed, but your "
                    "account is not on this deployment's allowlist. An administrator must add you."
                ),
                log_detail=f"email not in allowed_emails ({len(allowed_emails)} entr(ies))",
                verified_email=email,
                matched_rule="allowed_emails",
            )

        allowed_groups = set(self._allowed_groups(config))
        if allowed_groups:
            raw = claims.get("groups")
            member_of = set(_names(raw if isinstance(raw, list) else []))
            if not member_of & allowed_groups:
                return AccessDecision(
                    allowed=False,
                    reason=AccountNotAllowlisted.reason,
                    user_message=(
                        "You authenticated correctly, but you are not in an Okta group that may use "
                        "this deployment. Request access through your identity governance process."
                    ),
                    log_detail=f"groups claim present={isinstance(raw, list)}; no match in {len(allowed_groups)} allowed group(s)",
                    verified_email=email,
                    matched_rule="allowed_groups",
                )
            return AccessDecision(allowed=True, verified_email=email, matched_rule="allowed_groups")

        return AccessDecision(
            allowed=True,
            verified_email=email,
            matched_rule="allowed_emails" if allowed_emails else "allowed_domains",
        )

    def profile_snapshot(self, config: ProviderConfig, claims: Mapping[str, Any]) -> ProfileSnapshot:
        return ProfileSnapshot(
            display_name=str(claims.get("name") or ""),
            first_name=str(claims.get("given_name") or ""),
            last_name=str(claims.get("family_name") or ""),
        )

    def self_test(self, config: ProviderConfig, secrets: Mapping[str, str], *, live: bool) -> list[SelfTestResult]:
        results = self.validate_config(config)
        off = SelfTestPhase.OFFLINE
        if not secret_exists(config.secret_key):
            results.append(_result("secret", SelfTestStatus.FAIL, off, f"no auth:{config.secret_key} secret file found under TAP_SECRETS_ROOT"))
        elif not secrets.get("client_id") or not secrets.get("client_secret"):
            results.append(_result("secret", SelfTestStatus.FAIL, off, "resolved secret is missing client_id/client_secret"))
        else:
            results.append(_result("secret", SelfTestStatus.PASS, off, f"client_id …{secrets['client_id'][-6:]}"))
        results.append(self._discovery_check(config, live=live))
        return results

    def _discovery_check(self, config: ProviderConfig, *, live: bool) -> SelfTestResult:
        phase = SelfTestPhase.LIVE
        issuer = self._issuer(config)
        url = issuer + DISCOVERY_PATH
        if not live:
            return _result("discovery", SelfTestStatus.SKIP, phase, f"live check skipped ({url})")
        try:
            resp = requests.get(url, timeout=10)
        except requests.RequestException as exc:
            return _result("discovery", SelfTestStatus.FAIL, phase, f"could not reach {url}: {exc}")
        if resp.status_code != 200:
            return _result("discovery", SelfTestStatus.FAIL, phase, f"{url} returned HTTP {resp.status_code}")
        try:
            doc = resp.json()
        except ValueError:
            return _result("discovery", SelfTestStatus.FAIL, phase, f"{url} did not return JSON")
        missing = [k for k in _REQUIRED_DISCOVERY_KEYS if k not in doc]
        if missing:
            return _result("discovery", SelfTestStatus.FAIL, phase, f"discovery doc missing keys: {missing}")
        if str(doc.get("issuer", "")).rstrip("/") != issuer:
            return _result("discovery", SelfTestStatus.FAIL, phase, f"discovery issuer {doc.get('issuer')!r} does not match configured {issuer!r}")
        return _result("discovery", SelfTestStatus.PASS, phase, f"issuer {issuer} reachable")

    def build_allauth_settings(self, config: ProviderConfig, secrets: Mapping[str, str]) -> dict[str, Any]:
        if not secrets.get("client_id") or not secrets.get("client_secret"):
            raise ProviderError(f"cannot build allauth settings for {config.id}: secret not resolved")
        app_settings: dict[str, Any] = {"server_url": self._issuer(config), "oauth_pkce_enabled": True}
        if self._allowed_groups(config):
            # The groups claim must come from the signed ID token: userinfo only
            # carries it with a `groups` scope, and SCOPE is engine-wide, which
            # would break every other openid_connect provider.
            app_settings["fetch_userinfo"] = False
        return {
            "provider_id": config.id,
            "name": config.display_name,
            "client_id": secrets["client_id"],
            "secret": secrets["client_secret"],
            "settings": app_settings,
        }
