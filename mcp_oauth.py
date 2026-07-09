"""MCP OAuth authorization server endpoints, backing the hosted
streamable-http connector's login flow.

The connector (a separate process/repo, see auto-skill-connector's
mcp_oauth_provider.py) implements the `mcp` SDK's
OAuthAuthorizationServerProvider protocol by calling these routes over HTTP,
since the connector never touches this SQLite store directly -- it only ever
talks to the backend over HTTP, same as /route. The actual identity check is
the existing Google/GitHub login in accounts_api.py; this module only bridges
an in-flight MCP authorization request across that login round-trip, keyed by
`login_state`, the same way auth.py's `_states` bridges the CLI/web flows.
"""
from __future__ import annotations

import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

import auth
import local_store as store

router = APIRouter(prefix="/mcp-oauth")

_CODE_TTL_SECONDS = 600
_PENDING_TTL_SECONDS = 600
# login_state -> (pending /authorize request dict, expiry_epoch).
_pending: dict[str, tuple[dict, float]] = {}


def _purge_pending() -> None:
    now = time.time()
    for key in [k for k, (_, exp) in _pending.items() if exp < now]:
        _pending.pop(key, None)


def stash_pending(payload: dict) -> str:
    _purge_pending()
    login_state = secrets.token_urlsafe(24)
    _pending[login_state] = (payload, time.time() + _PENDING_TTL_SECONDS)
    return login_state


def peek_pending(login_state: str) -> dict | None:
    _purge_pending()
    entry = _pending.get(login_state)
    return entry[0] if entry else None


def pop_pending(login_state: str) -> dict | None:
    _purge_pending()
    entry = _pending.pop(login_state, None)
    return entry[0] if entry else None


@router.post("/clients")
async def register_client(client_info: dict[str, Any]):
    """Persist a dynamically-registered client verbatim.

    The `mcp` SDK's own RegistrationHandler (mcp/server/auth/handlers/register.py)
    already generated client_id/client_secret and validated the metadata
    before the connector's provider.register_client() calls this -- we just
    store whatever it hands us and echo it back on lookup, since the SDK's
    ClientAuthenticator middleware later compares the caller's client_secret
    against exactly this stored value."""
    client_id = client_info.get("client_id")
    if not client_id:
        raise HTTPException(status_code=400, detail="client_id is required")
    store.create_oauth_client(client_id, client_info)
    return client_info


@router.get("/clients/{client_id}")
async def get_client(client_id: str):
    client = store.get_oauth_client(client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="unknown client")
    return client


@router.get("/authorize")
async def authorize(
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    state: str,
    scope: str = "",
    resource: str | None = None,
    code_challenge_method: str = "S256",
):
    """Landed on by the MCP client's browser redirect (see mcp_server.py's
    provider.authorize()). Stashes the pending request and hands off to a
    provider chooser -- the actual login happens via /auth/{provider}/start."""
    if code_challenge_method != "S256":
        raise HTTPException(status_code=400, detail="only S256 code_challenge_method is supported")
    client = store.get_oauth_client(client_id)
    if client is None or redirect_uri not in client["redirect_uris"]:
        raise HTTPException(status_code=400, detail="unknown client or redirect_uri")
    login_state = stash_pending(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "scopes": scope.split() if scope else [],
            "resource": resource,
            "mcp_state": state,
        }
    )
    return RedirectResponse(f"/mcp-oauth/choose?login_state={login_state}")


@router.get("/choose")
async def choose(login_state: str):
    if peek_pending(login_state) is None:
        return HTMLResponse(
            "Login expired or invalid -- please retry connecting in your MCP client.", status_code=400
        )
    links = "".join(
        f'<p><a href="/auth/{provider}/start?flow=mcp&login_state={login_state}">Continue with {label}</a></p>'
        for provider, label in (("google", "Google"), ("github", "GitHub"))
    )
    html = (
        "<!doctype html><html><body style=\"font-family:sans-serif;max-width:420px;"
        "margin:80px auto;text-align:center\">"
        "<h2>Sign in to connect Auto-Skill</h2>" + links + "</body></html>"
    )
    return HTMLResponse(html)


@router.get("/codes/{code}")
async def peek_code(code: str):
    """Non-destructive lookup for the connector's load_authorization_code --
    the `mcp` SDK itself validates expiry/redirect_uri/PKCE against this
    before ever calling POST /token, so this must not consume the code."""
    entry = store.peek_mcp_auth_code(code)
    if entry is None:
        raise HTTPException(status_code=404, detail="unknown code")
    return entry


class TokenExchangeRequest(BaseModel):
    code: str
    client_id: str


@router.post("/token")
async def token(body: TokenExchangeRequest):
    """Called from the connector's exchange_authorization_code, only after
    the SDK has already validated PKCE/expiry/redirect_uri via /codes/{code}
    -- this just consumes the code (single use) and mints the access token."""
    entry = store.consume_mcp_auth_code(body.code, body.client_id)
    if entry is None:
        raise HTTPException(status_code=400, detail="invalid_grant: unknown, expired, or already-used code")
    access_token = auth.issue_cli_token(entry["user_id"])
    return {"access_token": access_token, "token_type": "bearer", "scope": " ".join(entry["scopes"])}


def mint_authorization_code(pending: dict, user_id: str) -> str:
    """Called from accounts_api.py's mcp login-callback branch once the user
    has authenticated, turning the stashed /authorize request into a code."""
    code = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=_CODE_TTL_SECONDS)).isoformat()
    store.create_mcp_auth_code(
        code=code,
        client_id=pending["client_id"],
        code_challenge=pending["code_challenge"],
        redirect_uri=pending["redirect_uri"],
        scopes=pending["scopes"],
        user_id=user_id,
        expires_at=expires_at,
    )
    return code
