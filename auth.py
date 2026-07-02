"""
Authentication for Autocode web app.

Strategy: JWT stored in an httpOnly cookie named 'session' (pattern: AutoMap v2),
password hashing with bcrypt directly (pattern: vedetta — no passlib).
- Token lifetime: EXPIRE_DAYS days (renewed on each login, not on activity).
- Secret key must be set via SECRET_KEY env var; startup will crash if missing.
- `get_current_user` is the standard FastAPI dependency for protected API routes.
- `get_user_or_none` is used by HTML routes that redirect manually instead of raising 401.
"""
import os
from datetime import datetime, timedelta

import bcrypt
from fastapi import Cookie, Depends, HTTPException, status
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from models import User, get_db

SECRET_KEY  = os.environ["SECRET_KEY"]
ALGORITHM   = "HS256"
EXPIRE_DAYS = 7


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
    session: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
) -> User:
    if not session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    user_id, scope = _decode_token(session)
    if scope != "full":  # password ok but 2FA not completed
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="2FA required")
    user = db.query(User).filter(User.id == user_id, User.is_active == True).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user


def get_user_or_none(session: str | None, db: Session) -> User | None:
    """Authenticated (full-session) user or None — for HTML routes that redirect manually."""
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
