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

import re
from dataclasses import dataclass

import requests

GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
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


@dataclass
class ProviderIdentity:
    provider: str
    subject: str
    email: str


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
