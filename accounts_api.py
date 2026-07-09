"""Accounts API: OAuth login (Google/GitHub) and per-user data (favorites,
installs, private skills). Self-contained APIRouter so scraper.py only needs:
    from accounts_api import router as accounts_router
    app.include_router(accounts_router)

All account data lives in the same local SQLite store (local_store.py) that
already backs the rest of the backend -- see auth.py's module docstring for
why there is no external database or auth framework here.

Endpoints:
  GET  /auth/{provider}/start?flow=cli|web|mcp&...  -> redirect into provider OAuth
  GET  /auth/{provider}/callback?code&state         -> redirect back per flow
  GET  /auth/whoami                           -> current user
  POST /auth/logout                           -> revoke the bearer token
  GET  /runs                                  -> current user's recent route_events (dashboard metrics)
  GET/POST/DELETE /favorites[/{skill_id}]     -> per-user favorited skills
  GET/POST        /installs                   -> per-user install history
  GET             /runs                       -> per-user route run history
  GET/POST/DELETE /private-skills[/{id}]      -> per-user private skill submissions
"""
import os
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunparse, urlunsplit

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import PlainTextResponse, RedirectResponse
from pydantic import BaseModel

import auth
import local_store as store
import mcp_oauth

router = APIRouter()

_DEFAULT_WEB_RETURN_ORIGINS = {
    "https://auto-skill.com",
    "https://www.auto-skill.com",
    "https://auto-skill-site.pages.dev",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
}


def _append_query(url: str, **params: str | None) -> str:
    parsed = urlparse(url)
    query = parse_qsl(parsed.query)
    query.extend((k, v) for k, v in params.items() if v is not None)
    return urlunparse(parsed._replace(query=urlencode(query)))


def _require_user(authorization: str | None) -> dict:
    user = auth.user_from_authorization_header(authorization)
    if user is None:
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")
    return user


def _origin_for_url(value: str) -> str | None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        return None
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), "", "", ""))


def _allowed_web_return_origins() -> set[str]:
    configured = os.environ.get("AUTO_SKILL_DASHBOARD_ORIGINS", "")
    values = [v.strip() for v in configured.split(",") if v.strip()]
    origins = {_origin_for_url(v) for v in values} if values else set(_DEFAULT_WEB_RETURN_ORIGINS)
    return {origin for origin in origins if origin}


def _safe_return_to(value: str) -> str:
    if not value or len(value) > 600:
        raise HTTPException(status_code=400, detail="invalid return_to")
    origin = _origin_for_url(value)
    if origin is None:
        raise HTTPException(status_code=400, detail="invalid return_to")
    if origin not in _allowed_web_return_origins():
        raise HTTPException(status_code=400, detail="return_to origin is not allowed")
    return value


def _with_token_fragment(return_to: str, token: str) -> str:
    parsed = urlsplit(return_to)
    params = dict(parse_qsl(parsed.fragment, keep_blank_values=True))
    params["token"] = token
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, urlencode(params)))


@router.get("/auth/{provider}/start")
async def auth_start(
    provider: str,
    port: int | None = None,
    flow: str = "cli",
    login_state: str | None = None,
    return_to: str | None = None,
):
    """Three login flavors share this one Google/GitHub handshake:
    flow=cli (default) is the `auto-skill login` loopback flow (needs `port`);
    flow=web is the auto-skill-site dashboard login (needs `return_to`);
    flow=mcp is the hosted MCP connector's OAuth authorize step (needs
    `login_state`, see mcp_oauth.py). See auth_callback for the payoff."""
    if provider not in auth.PROVIDERS:
        raise HTTPException(status_code=404, detail="unknown provider")
    if not auth.PROVIDERS[provider]["client_id"]:
        raise HTTPException(status_code=503, detail=f"{provider} login is not configured on this server")
    flow = (flow or "cli").strip().lower()
    if flow == "cli" and port is None:
        raise HTTPException(status_code=400, detail="port is required for the cli flow")
    if flow == "web" and not return_to:
        raise HTTPException(status_code=400, detail="return_to is required for the web flow")
    if flow == "mcp" and not login_state:
        raise HTTPException(status_code=400, detail="login_state is required for the mcp flow")
    if flow not in {"cli", "web", "mcp"}:
        raise HTTPException(status_code=400, detail="unknown login flow")
    if flow == "cli" and (int(port or 0) <= 0 or int(port or 0) > 65535):
        raise HTTPException(status_code=400, detail="port is required for CLI login")
    if flow == "web":
        return_to = _safe_return_to(str(return_to or ""))
    state = auth.create_state(
        provider, {"flow": flow, "port": port, "login_state": login_state, "return_to": return_to}
    )
    return RedirectResponse(auth.build_authorize_url(provider, state))


@router.get("/auth/{provider}/callback")
async def auth_callback(provider: str, code: str, state: str):
    resolved = auth.pop_state(state)
    if resolved is None or resolved.get("provider") != provider:
        return PlainTextResponse("Login expired or invalid -- please retry `auto-skill login`.", status_code=400)
    try:
        user, cli_token = await auth.complete_login(provider, code)
    except Exception:
        import traceback

        traceback.print_exc()
        return PlainTextResponse("Login failed while talking to the provider -- please retry.", status_code=502)

    flow = resolved.get("flow", "cli")

    if flow == "web":
        return RedirectResponse(_with_token_fragment(str(resolved.get("return_to") or ""), cli_token))

    if flow == "mcp":
        pending = mcp_oauth.pop_pending(resolved.get("login_state") or "")
        if pending is None:
            return PlainTextResponse(
                "Login expired or invalid -- please retry connecting in your MCP client.", status_code=400
            )
        auth_code = mcp_oauth.mint_authorization_code(pending, user["id"])
        redirect_url = _append_query(pending["redirect_uri"], code=auth_code, state=pending["mcp_state"])
        return RedirectResponse(redirect_url)

    return RedirectResponse(f"http://127.0.0.1:{int(resolved['port'])}/callback?token={cli_token}")


@router.get("/auth/whoami")
async def whoami(authorization: str | None = Header(None)):
    user = _require_user(authorization)
    return {"id": user["id"], "email": user["email"], "name": user["name"], "avatar_url": user["avatar_url"]}


@router.get("/runs")
async def get_runs(limit: int = 50, authorization: str | None = Header(None)):
    user = _require_user(authorization)
    return {"runs": store.list_route_events_for_user(user["id"], limit)}


@router.post("/auth/logout")
async def logout(authorization: str | None = Header(None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    raw_token = authorization[len("Bearer "):].strip()
    auth.revoke_cli_token(raw_token)
    return {"ok": True}


@router.post("/auth/refresh")
async def refresh(authorization: str | None = Header(None)):
    """Rotate a still-valid bearer token: issue a new one and revoke the old.

    Backs the MCP OAuth refresh_token grant (mcp_oauth_provider.py) -- our
    tokens don't otherwise expire, but MCP clients that request the
    refresh_token grant type (required by the `mcp` SDK's own registration
    handler) still expect a working refresh path, not just a token that
    happens to never expire.
    """
    user = _require_user(authorization)
    raw_token = authorization[len("Bearer "):].strip()  # type: ignore[union-attr]
    new_token = auth.issue_cli_token(user["id"])
    auth.revoke_cli_token(raw_token)
    return {"access_token": new_token}


class FavoriteRequest(BaseModel):
    skill_id: str


@router.get("/favorites")
async def get_favorites(authorization: str | None = Header(None)):
    user = _require_user(authorization)
    return {"favorites": store.list_favorites(user["id"])}


@router.post("/favorites")
async def post_favorite(body: FavoriteRequest, authorization: str | None = Header(None)):
    user = _require_user(authorization)
    store.add_favorite(user["id"], body.skill_id)
    return {"ok": True}


@router.delete("/favorites/{skill_id}")
async def delete_favorite(skill_id: str, authorization: str | None = Header(None)):
    user = _require_user(authorization)
    removed = store.remove_favorite(user["id"], skill_id)
    if not removed:
        raise HTTPException(status_code=404, detail="not favorited")
    return {"ok": True}


class InstallRequest(BaseModel):
    skill_id: str | None = None
    skill_url: str | None = None
    target: str


@router.get("/installs")
async def get_installs(authorization: str | None = Header(None)):
    user = _require_user(authorization)
    return {"installs": store.list_installs(user["id"])}


@router.post("/installs")
async def post_install(body: InstallRequest, authorization: str | None = Header(None)):
    user = _require_user(authorization)
    store.record_install(user["id"], body.skill_id, body.skill_url, body.target)
    return {"ok": True}


class PrivateSkillRequest(BaseModel):
    name: str
    description: str | None = None
    content: str


@router.get("/private-skills")
async def get_private_skills(authorization: str | None = Header(None)):
    user = _require_user(authorization)
    return {"private_skills": store.list_private_skills(user["id"])}


@router.post("/private-skills")
async def post_private_skill(body: PrivateSkillRequest, authorization: str | None = Header(None)):
    user = _require_user(authorization)
    skill = store.add_private_skill(user["id"], body.name, body.description, body.content)
    return {"private_skill": skill}


@router.delete("/private-skills/{skill_id}")
async def delete_private_skill(skill_id: str, authorization: str | None = Header(None)):
    user = _require_user(authorization)
    removed = store.remove_private_skill(user["id"], skill_id)
    if not removed:
        raise HTTPException(status_code=404, detail="not found")
    return {"ok": True}
