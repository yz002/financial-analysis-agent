"""
Provider-side OAuth token verification (Phase C session 2, design doc
oauth-identity-session-design.md SS1/SS7 step 2): each function wraps exactly one outbound
HTTP call to the provider itself and returns a verified ProviderIdentity, or raises a typed
refusal -- no DB/FastAPI dependency, mirroring billing.py's isolation of pure logic from
route glue. Session 3 wires these into POST /v1/auth/exchange.

Neither provider's token can be validated locally by this backend, so calling the provider's
own endpoint and trusting its response *is* the verification -- confirmed via fresh web search
this session, not assumed from training data:

- Google: GET https://openidconnect.googleapis.com/v1/userinfo (the current OIDC UserInfo
  endpoint), not oauth2.googleapis.com/tokeninfo, which Google's own docs say is unsuitable
  for production use. A 200 response's `sub` is the stable per-account identifier; `email` is
  only trustworthy when `email_verified` is true.
- Microsoft: GET https://graph.microsoft.com/v1.0/me. Microsoft's own docs state that
  Graph-audience tokens are proprietary/opaque and can't be locally validated by a third
  party, so Graph's own 200/non-200 response to this call is the verification. The default
  field set includes `id` (stable identifier) and `mail`, which is null for a real, common
  set of accounts (personal Microsoft accounts, some work/school tenants) -- `userPrincipalName`
  is the fallback, accepted only when it's syntactically an email address.

Neither call is cached -- every /v1/auth/exchange re-verifies with the provider, so a revoked
grant is caught at the next sign-in.
"""

import os
import re
from dataclasses import dataclass

import requests

GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
MICROSOFT_GRAPH_ME_URL = "https://graph.microsoft.com/v1.0/me"

# A basic email-format check for Microsoft's userPrincipalName fallback -- not full RFC 5322
# validation, just enough to distinguish an email-shaped UPN from a phone number or
# Skype-style alias (design doc SS1).
_EMAIL_FORMAT = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_REQUEST_TIMEOUT_SECONDS = 10


class OAuthProviderError(Exception):
    """Base class for this module's typed refusals -- specific to provider verification,
    not a general application error."""


class InvalidProviderTokenError(OAuthProviderError):
    """The token is invalid, expired, wrong-audience, or otherwise unverifiable. Maps to a
    401 at the route layer (session 3)."""


class ProviderEmailUnavailableError(OAuthProviderError):
    """The token verified successfully, but no valid email address is available for the
    account. Maps to a 422 at the route layer (session 3)."""


class OAuthProviderConfigError(OAuthProviderError):
    """A required server-side OAuth config value (e.g. GOOGLE_CLIENT_SECRET) is missing.
    A server misconfiguration, not a caller fault -- maps to a 500 at the route layer,
    same class of error as /v1/billing/checkout-session's missing STRIPE_PRICE_ID."""


@dataclass
class ProviderIdentity:
    provider: str
    subject: str
    email: str


def exchange_google_code_for_token(code: str, code_verifier: str, redirect_uri: str) -> str:
    """
    Google's "Web application" client type -- the only one compatible with
    launchWebAuthFlow's https redirect requirement (confirmed this session against
    Chrome's own developer docs: the dedicated "Chrome Extension" client type has no
    redirect-URI field and only works with chrome.identity.getAuthToken) -- is itself
    documented by Google as a confidential client: "a web server application does need a
    secret." PKCE does not substitute for client_secret at its token endpoint, unlike
    Microsoft's Azure "Single-page application" platform type, which is a genuine
    no-secret public client (confirmed via Google's current OAuth 2.0 docs, Phase D
    session 2 -- see EXTENSION_INTEGRATION.md SS1). So, unlike Microsoft's exchange (done
    client-side by the extension), this one happens here, where GOOGLE_CLIENT_SECRET can
    stay confidential.

    Raises OAuthProviderConfigError if GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET aren't set
    (a server misconfiguration), InvalidProviderTokenError on a non-200 response or a
    response missing access_token (an invalid/expired code, a code_verifier/redirect_uri
    mismatch, etc. -- Google doesn't distinguish these to callers, so neither do we).
    """
    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise OAuthProviderConfigError("GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET is not configured.")

    response = requests.post(
        GOOGLE_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "code_verifier": code_verifier,
            "redirect_uri": redirect_uri,
        },
        timeout=_REQUEST_TIMEOUT_SECONDS,
    )
    if response.status_code != 200:
        raise InvalidProviderTokenError(
            f"Google token exchange failed with status {response.status_code}."
        )

    access_token = response.json().get("access_token")
    if not access_token:
        raise InvalidProviderTokenError("Google token exchange response did not include an access token.")
    return access_token


def verify_google_token(oauth_token: str) -> ProviderIdentity:
    """
    Raises InvalidProviderTokenError on a non-200 response or when email_verified is not
    true -- both refuse identically (design doc SS1 groups them: an unverified email is as
    unusable as an outright invalid token), never returning a partial/fake identity.
    """
    response = requests.get(
        GOOGLE_USERINFO_URL,
        headers={"Authorization": f"Bearer {oauth_token}"},
        timeout=_REQUEST_TIMEOUT_SECONDS,
    )
    if response.status_code != 200:
        raise InvalidProviderTokenError(
            f"Google userinfo request failed with status {response.status_code}."
        )

    data = response.json()
    if not data.get("email_verified"):
        raise InvalidProviderTokenError("Google account email is not verified.")

    return ProviderIdentity(provider="google", subject=data["sub"], email=data["email"])


def verify_microsoft_token(oauth_token: str) -> ProviderIdentity:
    """
    Raises InvalidProviderTokenError on a non-200 response (Graph's own rejection of the
    token is the verification -- it can't be validated any other way). Raises
    ProviderEmailUnavailableError when `mail` is null and `userPrincipalName` isn't
    email-shaped, rather than accepting a non-email UPN as someone's email.
    """
    response = requests.get(
        MICROSOFT_GRAPH_ME_URL,
        headers={"Authorization": f"Bearer {oauth_token}"},
        timeout=_REQUEST_TIMEOUT_SECONDS,
    )
    if response.status_code != 200:
        raise InvalidProviderTokenError(
            f"Microsoft Graph /me request failed with status {response.status_code}."
        )

    data = response.json()
    email = data.get("mail")
    if not email:
        upn = data.get("userPrincipalName")
        if not upn or not _EMAIL_FORMAT.match(upn):
            raise ProviderEmailUnavailableError(
                "Microsoft account has no email and userPrincipalName is not email-shaped."
            )
        email = upn

    return ProviderIdentity(provider="microsoft", subject=data["id"], email=email)
