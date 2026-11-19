"""Service identity and authorisation: who is calling, and may they?

    caller                                   receiver
    ──────                                   ────────
    private key ─► JWT {iss=sub=me,          public keys of every service
                        aud=receiver,   ──►  verify signature, audience, expiry
                        exp=now+60s}         look up (receiver, caller) in access.toml
                                             ─► scopes, or refuse

Two separate questions, answered in two places:
  * Authentication (who): the caller signs a short-lived token with its own Ed25519
    key. Only the holder of that key can produce it, and it names one audience, so
    a token stolen from one service cannot be replayed against another.
  * Authorisation (what): the *receiver* decides, from config/access.toml. A token
    carries identity only; a caller cannot grant itself a scope.

This is the shape of a service mesh (SPIFFE identities + per-service policy), done
by hand. The production alternative is an OAuth authorisation server issuing
scoped tokens (client-credentials grant); see ADR 0011 for the trade-off.

Asymmetric keys on purpose: a receiver holds only public keys, so compromising one
service does not let it mint tokens as any other.

Everything here is off when no keys are configured (unit tests), and fail-closed
when AUTH_REQUIRED=1 (every container): a missing key then stops the service at
startup instead of silently running open.
"""

from __future__ import annotations

import logging
import os
import time
import tomllib
import uuid
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

log = logging.getLogger(__name__)

TOKEN_TTL_S = 60
MAX_TTL_S = 300  # refuse long-lived tokens even if correctly signed
LEEWAY_S = 5  # clock skew between containers

# Host-side tools reach services through published loopback ports; the audience is
# still the service's name, never "127.0.0.1".
LOCAL_PORTS = {
    8100: "llm-gateway", 8200: "orchestrator", 8300: "market-data",
    8401: "technical-agent", 8402: "fundamentals-agent", 8403: "news-agent",
    8404: "debate-agent", 8405: "portfolio-agent", 8500: "memory", 8600: "broker",
    8406: "advisor-agent", 8800: "official-web",
}


class AuthError(Exception):
    """401: not authenticated (no, bad or expired token). 403: authenticated, not allowed."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status, self.reason = status, reason


class AuthConfigError(Exception):
    """AUTH_REQUIRED is set but keys or policy are missing."""


def auth_required() -> bool:
    return os.getenv("AUTH_REQUIRED", "").lower() in {"1", "true", "yes"}


def _keys_dir() -> Path:
    return Path(os.getenv("KEYS_DIR", ".keys"))


# ---------------------------------------------------------------- caller side


@dataclass(frozen=True)
class Identity:
    name: str
    key: Ed25519PrivateKey = field(repr=False)

    def mint(self, audience: str, ttl_s: int = TOKEN_TTL_S) -> str:
        now = int(time.time())
        return jwt.encode(
            {"iss": self.name, "sub": self.name, "aud": audience, "iat": now,
             "exp": now + ttl_s, "jti": uuid.uuid4().hex},
            self.key, algorithm="EdDSA", headers={"kid": self.name})


def audience_for(url: str) -> str:
    """The receiver's service name: its hostname on the Compose network, or the
    service behind a published loopback port when called from the host."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if host in {"127.0.0.1", "localhost", "::1"}:
        return LOCAL_PORTS.get(parts.port or 80, host)
    return host


class BearerAuth:
    """Signs every outgoing request. A fresh token per request: 60 s life, no refresh
    logic, no shared state. Works as an httpx or httpx2 `auth=` callable."""

    def __init__(self, identity: Identity, audience: str | None = None) -> None:
        self.identity, self.audience = identity, audience

    def __call__(self, request: Any) -> Any:
        aud = self.audience or audience_for(str(request.url))
        request.headers["Authorization"] = f"Bearer {self.identity.mint(aud)}"
        return request


def load_private_key(path: Path) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise AuthConfigError(f"{path}: not an Ed25519 private key")
    return key


@cache
def default_identity() -> Identity | None:
    """This process's identity: SERVICE_NAME (containers) or `operator` (you, on the
    host). Key from SERVICE_KEY_FILE, else .keys/private/<name>.key."""
    name = os.getenv("SERVICE_NAME", "operator")
    path = Path(os.getenv("SERVICE_KEY_FILE", _keys_dir() / "private" / f"{name}.key"))
    if not path.exists():
        if auth_required():
            raise AuthConfigError(f"AUTH_REQUIRED but no signing key at {path}")
        return None
    return Identity(name, load_private_key(path))


def bearer_for(audience: str | None = None) -> BearerAuth | None:
    ident = default_identity()
    return BearerAuth(ident, audience) if ident else None


# ---------------------------------------------------------------- receiver side


@dataclass(frozen=True)
class Principal:
    name: str
    scopes: frozenset[str]


@dataclass
class Verifier:
    service: str
    public_keys: dict[str, Ed25519PublicKey]
    grants: dict[str, frozenset[str]]

    def verify(self, token: str) -> Principal:
        try:
            kid = jwt.get_unverified_header(token).get("kid")
        except jwt.PyJWTError as e:
            raise AuthError(401, f"malformed token: {e}") from e
        key = self.public_keys.get(kid or "")
        if key is None:
            raise AuthError(401, f"unknown signer {kid!r}")
        try:
            claims = jwt.decode(token, key, algorithms=["EdDSA"], audience=self.service,
                                issuer=kid, leeway=LEEWAY_S,
                                options={"require": ["iss", "sub", "aud", "iat", "exp"]})
        except jwt.PyJWTError as e:  # bad signature, wrong audience, expired, ...
            raise AuthError(401, f"{type(e).__name__}: {e}") from e
        if claims["sub"] != kid:
            raise AuthError(401, "subject does not match signer")
        if claims["exp"] - claims["iat"] > MAX_TTL_S:
            raise AuthError(401, f"token lifetime over {MAX_TTL_S} s")
        scopes = self.grants.get(kid)
        if not scopes:
            raise AuthError(403, f"{kid} has no access to {self.service}")
        return Principal(kid, scopes)

    def check(self, authorization: str | None, scope: str) -> Principal:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise AuthError(401, "missing bearer token")
        p = self.verify(authorization[7:].strip())
        if scope not in p.scopes:
            raise AuthError(403, f"{p.name} lacks scope {scope!r} on {self.service}")
        return p


def load_policy(path: Path | None = None) -> dict[str, dict[str, frozenset[str]]]:
    path = path or Path(os.getenv("ACCESS_POLICY_FILE", "config/access.toml"))
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    return {recv: {caller: frozenset(scopes) for caller, scopes in callers.items()}
            for recv, callers in raw.items()}


def load_public_keys(directory: Path | None = None) -> dict[str, Ed25519PublicKey]:
    directory = directory or _keys_dir() / "public"
    keys = {}
    for p in sorted(directory.glob("*.pub")):
        k = serialization.load_pem_public_key(p.read_bytes())
        if isinstance(k, Ed25519PublicKey):
            keys[p.stem] = k
    return keys


def load_verifier(service: str | None = None) -> Verifier | None:
    """Enforcement is opt-in per process (AUTH_REQUIRED=1, set on every container) and
    then fail-closed: missing keys or policy stop the service. Without it (unit tests,
    a server started by hand) the receiver runs open and says so."""
    service = service or os.getenv("SERVICE_NAME", "")
    if not auth_required():
        log.warning("auth OFF for %s (AUTH_REQUIRED not set)", service or "?")
        return None
    try:
        keys, policy = load_public_keys(), load_policy()
    except FileNotFoundError as e:
        raise AuthConfigError(f"AUTH_REQUIRED but {e}") from e
    if not keys:
        raise AuthConfigError("AUTH_REQUIRED but no public keys found")
    if service not in policy:
        raise AuthConfigError(f"no [{service}] section in the access policy")
    return Verifier(service, keys, policy[service])


# ---------------------------------------------------------------- HTTP (FastAPI) guard


def guard_http(app: Any, verifier: Verifier | None, rules: dict[str, str]) -> None:
    """Require a scope for requests whose path starts with a rule's prefix. Paths not
    covered (health, the public agent card) stay open. The caller, once proven, is on
    `request.state.principal`."""
    if verifier is None:
        return
    from fastapi import Request
    from fastapi.responses import JSONResponse

    ordered = sorted(rules.items(), key=lambda kv: -len(kv[0]))

    @app.middleware("http")
    async def _auth(request: Request, call_next):
        scope = next((s for prefix, s in ordered if _matches(request, prefix)), None)
        if scope is None:
            return await call_next(request)
        try:
            request.state.principal = verifier.check(request.headers.get("authorization"),
                                                     scope)
        except AuthError as e:
            log.warning("refused %s %s: %s", request.method, request.url.path, e.reason)
            return JSONResponse(status_code=e.status, content={"detail": e.reason},
                                headers={"WWW-Authenticate": "Bearer"} if e.status == 401
                                else {})
        return await call_next(request)


def _matches(request: Any, rule: str) -> bool:
    method, _, prefix = rule.rpartition(" ")
    if method and request.method != method:
        return False
    path = request.url.path
    if prefix == "/":
        return path == "/"
    return path == prefix or path.startswith(prefix.rstrip("/") + "/")


# ---------------------------------------------------------------- MCP guard


class McpGuard:
    """Plugs a Verifier into the MCP SDK's own resource-server auth.

    The SDK checks the bearer token on every HTTP request (401 if bad) and exposes
    it to tools through `get_access_token()`. Each tool then asks for the scope it
    needs with `require()`. In-process clients (tests) bypass HTTP, so a guard with
    no verifier allows everything.
    """

    def __init__(self, service: str, verifier: Verifier | None) -> None:
        self.service, self.verifier = service, verifier

    @classmethod
    def from_env(cls, service: str) -> McpGuard:
        return cls(service, load_verifier(service))

    @property
    def enabled(self) -> bool:
        return self.verifier is not None

    def server_kwargs(self) -> dict[str, Any]:
        if self.verifier is None:
            return {}
        from mcp.server.auth.settings import AuthSettings

        return {
            "token_verifier": _SdkTokenVerifier(self.verifier),
            "auth": AuthSettings(
                # We have no OAuth authorisation server; identities are self-signed.
                # The SDK still wants an issuer URL for its protected-resource metadata.
                issuer_url=f"http://{self.service}.desk.invalid",
                resource_server_url=f"http://{self.service}:8000/mcp",
                validate_token_resource=False,  # our verifier checks the audience
            ),
        }

    def require(self, scope: str) -> str | None:
        """Raise a ToolError unless the current caller holds `scope`. Returns the caller."""
        if self.verifier is None:
            return None
        from mcp.server.auth.middleware.auth_context import get_access_token
        from mcp.server.mcpserver.exceptions import ToolError

        token = get_access_token()
        if token is None:
            raise ToolError("forbidden: unauthenticated")
        if scope not in token.scopes:
            log.warning("refused %s: %s lacks %s", self.service, token.client_id, scope)
            raise ToolError(f"forbidden: {token.client_id} lacks scope {scope!r}")
        return token.client_id

    def caller(self) -> str | None:
        if self.verifier is None:
            return None
        from mcp.server.auth.middleware.auth_context import get_access_token

        token = get_access_token()
        return token.client_id if token else None


class _SdkTokenVerifier:
    def __init__(self, verifier: Verifier) -> None:
        self.verifier = verifier

    async def verify_token(self, token: str):
        from mcp.server.auth.provider import AccessToken

        try:
            p = self.verifier.verify(token)
        except AuthError as e:
            log.warning("refused token for %s: %s", self.verifier.service, e.reason)
            return None  # the SDK answers 401
        return AccessToken(token=token, client_id=p.name, scopes=sorted(p.scopes))


def mcp_http_client(url: str, timeout_s: float):
    """An httpx2 client for MCP's streamable HTTP transport that signs every request."""
    import httpx2
    from mcp.shared._httpx_utils import create_mcp_http_client

    return create_mcp_http_client(
        auth=bearer_for(audience_for(url)),
        timeout=httpx2.Timeout(30.0, read=timeout_s))


# ---------------------------------------------------------------- key generation


def generate(names: list[str], directory: Path, rotate: bool = False) -> list[str]:
    (directory / "private").mkdir(parents=True, exist_ok=True)
    (directory / "public").mkdir(parents=True, exist_ok=True)
    made = []
    for name in names:
        priv = directory / "private" / f"{name}.key"
        if priv.exists() and not rotate:
            continue
        key = Ed25519PrivateKey.generate()
        priv.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
        (directory / "public" / f"{name}.pub").write_bytes(key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
        made.append(name)
    return made


def keygen() -> None:
    """`uv run desk-keygen`: one Ed25519 key pair per identity named in the policy."""
    import argparse

    ap = argparse.ArgumentParser(description=keygen.__doc__)
    ap.add_argument("--rotate", nargs="*", metavar="NAME",
                    help="replace these identities' keys (all, if no names given)")
    args = ap.parse_args()
    policy = load_policy()
    names = sorted(set(policy) | {c for callers in policy.values() for c in callers})
    directory = _keys_dir()
    made = generate(names, directory)
    if args.rotate is not None:
        made += generate(args.rotate or names, directory, rotate=True)
    print(f"{len(names)} identities in {directory}; new or rotated: {sorted(set(made)) or 'none'}")
    print("Restart services to pick up rotated keys.")
