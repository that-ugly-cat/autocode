"""
Authentication for Autocode web app.

Strategy: JWT stored in an httpOnly cookie named 'session' (pattern: AutoMap v2),
password hashing with bcrypt directly (pattern: vedetta — no passlib).
- Token lifetime: EXPIRE_DAYS days (renewed on each login, not on activity).
- Secret key must be set via SECRET_KEY env var; startup will crash if missing.
- `get_current_user` is the standard FastAPI dependency for protected API routes.
- `get_user_or_none` is used by HTML routes that redirect manually instead of raising 401.
"""
import ipaddress
import logging
import os
import secrets
from datetime import datetime, timedelta

import bcrypt
from fastapi import Cookie, Depends, HTTPException, Request, status
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from models import User, get_db

log = logging.getLogger("autocode.auth")

SECRET_KEY  = os.environ["SECRET_KEY"]
ALGORITHM   = "HS256"
EXPIRE_DAYS = 7

# ── Two modes ─────────────────────────────────────────────────────────────────
#
#   local     (default)   email + password + TOTP, as it has always worked
#   gateway               an upstream SSO gate vouches via X-Borant-*
#
# `local` is the default and that is not negotiable: an app that believes an
# identity header with no gate in front of it lets anyone be anyone, so the
# gateway path is dead code until someone turns it on deliberately.
#
# **What moves, and it is the point of this app's migration:** in `gateway` the
# local login is off, and with it goes this app's own second factor. That would
# be a weakening — the TOTP here is real, and `POST /api/auth/login` grants only
# a ten-minute pending token until it is passed. The second factor therefore has
# to be re-asserted at the gate with a `two_factor` policy, not dropped. A
# level goes on a class of secrets, not on a URL: the per-user Anthropic keys
# are set from /profile, so the policy belongs on `/` and not on `/admin`.
AUTH_MODE = os.environ.get("AUTH_MODE", "local").strip().lower()

# Believed only from the reverse proxy, never from the internet. Under Docker
# this is a bridge gateway and NOT 127.0.0.1: measure it from the app's log
# after a real request instead of deducing it from the network layout.
TRUSTED_PROXY = os.environ.get("BORANT_TRUSTED_PROXY", "127.0.0.1")
BORANT_LOGOUT_URL = os.environ.get("BORANT_LOGOUT_URL", "https://id.borant.eu/logout")


def _parse_trusted(raw: str) -> list:
    nets = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            nets.append(ipaddress.ip_network(chunk, strict=False))
        except ValueError:
            log.warning("BORANT_TRUSTED_PROXY: ignoring %r, not an address or CIDR", chunk)
    return nets


TRUSTED_PROXIES = _parse_trusted(TRUSTED_PROXY)


def gateway_mode() -> bool:
    return AUTH_MODE == "gateway"


def _from_trusted_proxy(request: Request) -> bool:
    peer = request.client.host if request.client else None
    if not peer:
        return False
    try:
        addr = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return any(addr in net for net in TRUSTED_PROXIES)


def user_from_gateway(request: Request, db: Session) -> User | None:
    """The user the gate vouched for, or None.

    Lookup is by `borant_sub` and never by email: a typo in the gate's admin
    panel must not hand one person another person's workspaces. An unknown
    subject gets a fresh profile that owns nothing and is a member of nothing,
    so the failure mode is an empty screen and not a leak — and it has no
    Anthropic key of its own, so it cannot spend either.
    """
    if not gateway_mode():
        return None
    sub = request.headers.get("x-borant-sub")
    if not sub:
        return None
    if not _from_trusted_proxy(request):
        log.warning("X-Borant-Sub from %s, outside BORANT_TRUSTED_PROXY (%s): ignored",
                    request.client.host if request.client else "?", TRUSTED_PROXY)
        return None

    user = db.query(User).filter(User.borant_sub == sub).first()
    if user is not None:
        return user if user.is_active else None

    email = (request.headers.get("x-borant-email", "") or f"{sub}@borant.invalid").strip().lower()
    taken = db.query(User).filter(User.email == email).first()
    if taken is not None:
        # Qualcuno con questo indirizzo c'e' gia' e non e' legato. NON si adotta
        # quella riga: agganciare per email a runtime e' cio' che map_borant.py
        # esiste per tenere manuale, e quella riga puo' essere quella
        # dell'admin. Si fallisce chiusi, dicendo cosa lanciare.
        log.error("gateway: %s arrives as %s, but a local row already holds that "
                  "address and has no borant_sub. Run "
                  "`python map_borant.py --map %s=%s` instead of letting the gate guess.",
                  email, sub, email, sub)
        return None

    # Una password locale che non conosce nessuno, invece di nessuna password:
    # AUTH_MODE=local deve restare una strada di ritorno.
    user = User(email=email,
                name=request.headers.get("x-borant-name", "").strip() or email,
                hashed_password=hash_password(secrets.token_urlsafe(32)),
                borant_sub=sub, is_active=True, is_admin=False)
    db.add(user)
    db.commit()
    db.refresh(user)
    log.info("gateway: new profile for %s (%s)", email, sub)
    return user


# ── Password ──────────────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


# ── JWT ───────────────────────────────────────────────────────────────────────

PENDING_MINUTES = 10  # short-lived token between password and the 2FA second step


def create_token(user_id: int) -> str:
    """Full session: granted only after the 2FA second step."""
    expire = datetime.utcnow() + timedelta(days=EXPIRE_DAYS)
    return jwt.encode({"sub": str(user_id), "scope": "full", "exp": expire},
                      SECRET_KEY, algorithm=ALGORITHM)


def create_pending_token(user_id: int) -> str:
    """Password verified, 2FA still pending — cannot access protected routes."""
    expire = datetime.utcnow() + timedelta(minutes=PENDING_MINUTES)
    return jwt.encode({"sub": str(user_id), "scope": "pending_2fa", "exp": expire},
                      SECRET_KEY, algorithm=ALGORITHM)


def _decode_token(token: str) -> tuple[int, str]:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return int(payload["sub"]), payload.get("scope", "full")
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session")


# ── Dependencies ──────────────────────────────────────────────────────────────

def get_current_user(
    request: Request,
    session: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
) -> User:
    if gateway_mode():
        # L'header vince sul cookie locale, sempre, e non c'e' ripiego: un
        # cookie rimasto in giro non deve sopravvivere a una sessione revocata
        # dal gate. Nota che qui non si guarda lo `scope`: il secondo fattore,
        # in questa modalita', lo impone il gate con una policy `two_factor`.
        user = user_from_gateway(request, db)
        if user is not None:
            return user
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    if not session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    user_id, scope = _decode_token(session)
    if scope != "full":  # password ok but 2FA not completed
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="2FA required")
    user = db.query(User).filter(User.id == user_id, User.is_active == True).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user


def get_user_or_none(session: str | None, db: Session,
                     request: Request | None = None) -> User | None:
    """Authenticated (full-session) user or None — for HTML routes that redirect manually.

    In `gateway` the identity is in the headers, so the request has to come in
    too; without it this returns None, which fails closed."""
    if gateway_mode():
        return user_from_gateway(request, db) if request is not None else None
    if not session:
        return None
    try:
        user_id, scope = _decode_token(session)
    except HTTPException:
        return None
    if scope != "full":
        return None
    return db.query(User).filter(User.id == user_id, User.is_active == True).first()


def get_pending_user(session: str | None, db: Session) -> User | None:
    """User from a valid token of any scope (pending or full) — for the 2FA endpoints."""
    if not session:
        return None
    try:
        user_id, _ = _decode_token(session)
    except HTTPException:
        return None
    return db.query(User).filter(User.id == user_id, User.is_active == True).first()


def require_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin required")
    return user
