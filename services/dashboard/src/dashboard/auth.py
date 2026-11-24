"""Who is signed in: users, passwords, and the two tokens a browser holds.

    sign-up / login ──► users (argon2id hash) ──► access JWT (15 min) + refresh token (14 d)
    every request   ──► access JWT, checked here without touching the database
    access expired  ──► POST /api/auth/refresh: the refresh token is swapped for a new pair

* The access token is a JWT (HS256, signed with DASHBOARD_SESSION_SECRET): user id,
  account, email, name, expiry. Verifying it is a signature check, so the many reads a
  screen makes cost no database round trip. It cannot be revoked, so it lives 15 minutes.
* The refresh token is random, stored only as a SHA-256 hash, and rotated on every use:
  each refresh marks it used and issues a new one in the same *family*. Presenting a used
  token again means it was copied, so the whole family is revoked and the thief and the
  user are both signed out. Signing out revokes the family too.
* Both are HttpOnly, SameSite=Strict cookies, so page scripts never see them; the refresh
  cookie is sent only to /api/auth. Every write also needs the X-Desk-Request header
  (CSRF), checked in main.

Passwords: argon2id; an unknown email costs the same as a wrong password, so timing does
not reveal who has an account. Email verification and password resets come with an email
provider; `email_verified_at` is where that lands.
"""

import hashlib
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import asyncpg
import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

ACCESS_S = 15 * 60
REFRESH_S = 14 * 24 * 3600
RACE_GRACE_S = 30  # two tabs refreshing at once: the loser retries, nobody is signed out
ISSUER, AUDIENCE = "psx-desk", "psx-desk-app"
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
MIN_PASSWORD = 10

_ph = PasswordHasher()  # argon2id, library defaults (RFC 9106 low-memory profile)
_DUMMY_HASH = _ph.hash(secrets.token_urlsafe(16))


@dataclass(frozen=True)
class User:
    id: str
    email: str
    name: str
    account: str


class EmailTaken(Exception):
    pass


class RefreshRaced(Exception):
    """This token was swapped moments ago by another tab; retry with the new cookie."""


# ---------------------------------------------------------------- access tokens


def issue_access(secret: str, user: User, now: float | None = None) -> str:
    t = int(now if now is not None else time.time())
    return jwt.encode({"iss": ISSUER, "aud": AUDIENCE, "sub": user.id, "acc": user.account,
                       "email": user.email, "name": user.name, "iat": t,
                       "exp": t + ACCESS_S, "jti": uuid.uuid4().hex}, secret, algorithm="HS256")


def read_access(secret: str, token: str | None) -> User | None:
    if not token:
        return None
    try:
        c = jwt.decode(token, secret, algorithms=["HS256"], audience=AUDIENCE, issuer=ISSUER,
                       options={"require": ["exp", "iat", "sub", "acc"]})
    except jwt.PyJWTError:
        return None
    return User(id=c["sub"], email=c.get("email", ""), name=c.get("name", ""),
                account=c["acc"])


# ---------------------------------------------------------------- the user store


def _hash_token(raw: str) -> bytes:
    return hashlib.sha256(raw.encode()).digest()


def _user(row) -> User:
    return User(id=str(row["id"]), email=row["email"], name=row["name"], account=row["account"])


def new_account_key() -> str:
    return f"u_{secrets.token_hex(6)}"


class UserStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    @classmethod
    async def connect(cls, dsn: str) -> "UserStore":
        return cls(await asyncpg.create_pool(dsn, min_size=1, max_size=4))

    async def close(self) -> None:
        await self.pool.close()

    async def create_user(self, email: str, name: str, password: str,
                          account: str | None = None) -> User:
        try:
            row = await self.pool.fetchrow(
                "INSERT INTO users (email, name, password_hash, account) "
                "VALUES ($1, $2, $3, $4) RETURNING id, email, name, account",
                email.lower(), name, _ph.hash(password), account or new_account_key())
        except asyncpg.UniqueViolationError as e:
            raise EmailTaken(email) from e
        return _user(row)

    async def authenticate(self, email: str, password: str) -> User | None:
        row = await self.pool.fetchrow(
            "SELECT id, email, name, account, password_hash, disabled_at FROM users "
            "WHERE email = $1", email.lower())
        try:
            _ph.verify(row["password_hash"] if row else _DUMMY_HASH, password)
        except (VerificationError, InvalidHashError):
            return None
        if row is None or row["disabled_at"] is not None:
            return None
        if _ph.check_needs_rehash(row["password_hash"]):
            await self.pool.execute("UPDATE users SET password_hash = $2 WHERE id = $1",
                                    row["id"], _ph.hash(password))
        await self.pool.execute("UPDATE users SET last_login_at = now() WHERE id = $1",
                                row["id"])
        return _user(row)

    async def ensure_operator(self, email: str, password: str, account: str = "operator",
                              name: str = "Operator") -> User:
        """The operator account, whose password is DASHBOARD_PASSWORD in .env (the source
        of truth: change it there and restart)."""
        row = await self.pool.fetchrow(
            "SELECT id, email, name, account, password_hash FROM users WHERE account = $1",
            account)
        if row is None:
            return await self.create_user(email, name, password, account=account)
        try:
            _ph.verify(row["password_hash"], password)
        except (VerificationError, InvalidHashError):
            await self.pool.execute("UPDATE users SET password_hash = $2 WHERE id = $1",
                                    row["id"], _ph.hash(password))
        return _user(row)

    async def user(self, user_id: str) -> User | None:
        row = await self.pool.fetchrow(
            "SELECT id, email, name, account FROM users WHERE id = $1 AND disabled_at IS NULL",
            uuid.UUID(user_id))
        return _user(row) if row else None

    # ------------------------------------------------------------ refresh tokens

    async def start_session(self, user: User, user_agent: str = "") -> str:
        raw = secrets.token_urlsafe(32)
        await self.pool.execute(
            "INSERT INTO sessions (user_id, family, token_hash, expires_at, user_agent) "
            "VALUES ($1, $2, $3, $4, $5)", uuid.UUID(user.id), uuid.uuid4(), _hash_token(raw),
            datetime.now(UTC) + timedelta(seconds=REFRESH_S), user_agent[:200])
        return raw

    async def rotate(self, raw: str, user_agent: str = "") -> tuple[User, str] | None:
        """Swap a refresh token for a new one. None: sign in again. Raises RefreshRaced
        when another tab swapped this token seconds ago."""
        now = datetime.now(UTC)
        async with self.pool.acquire() as conn, conn.transaction():
            s = await conn.fetchrow(
                "SELECT id, user_id, family, expires_at, used_at, revoked_at FROM sessions "
                "WHERE token_hash = $1 FOR UPDATE", _hash_token(raw))
            if s is None or s["revoked_at"] is not None or s["expires_at"] < now:
                return None
            if s["used_at"] is not None:
                if (now - s["used_at"]).total_seconds() < RACE_GRACE_S:
                    raise RefreshRaced()
                # Reuse of a spent token: it was copied. End this sign-in everywhere.
                await conn.execute("UPDATE sessions SET revoked_at = $2 "
                                   "WHERE family = $1 AND revoked_at IS NULL", s["family"], now)
                return None
            row = await conn.fetchrow(
                "SELECT id, email, name, account FROM users WHERE id = $1 "
                "AND disabled_at IS NULL", s["user_id"])
            if row is None:
                return None
            await conn.execute("UPDATE sessions SET used_at = $2 WHERE id = $1", s["id"], now)
            new = secrets.token_urlsafe(32)
            await conn.execute(
                "INSERT INTO sessions (user_id, family, token_hash, expires_at, user_agent) "
                "VALUES ($1, $2, $3, $4, $5)", s["user_id"], s["family"], _hash_token(new),
                now + timedelta(seconds=REFRESH_S), user_agent[:200])
            return _user(row), new

    async def end_session(self, raw: str) -> None:
        await self.pool.execute(
            "UPDATE sessions SET revoked_at = now() WHERE revoked_at IS NULL AND family = "
            "(SELECT family FROM sessions WHERE token_hash = $1)", _hash_token(raw))
