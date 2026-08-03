"""Minimal but real auth: pbkdf2 password hashing (stdlib) + JWT bearer (HS256).

Trust boundary — not simplified away. Roles: ceo | dept_head | member | client.
"""
import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import Organization, User

_bearer = HTTPBearer()
_PBKDF2_ROUNDS = 200_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ROUNDS)
    return f"{salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    salt_hex, dk_hex = stored.split("$")
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), _PBKDF2_ROUNDS)
    return hmac.compare_digest(dk.hex(), dk_hex)


def issue_token(user: User) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user.id,
        "org": user.org_id,
        "role": user.role,
        "exp": now + timedelta(seconds=settings.jwt_ttl_seconds),
        "iat": now,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


class Principal:
    def __init__(self, user_id: str, org_id: str, role: str):
        self.user_id = user_id
        self.org_id = org_id
        self.role = role


def _principal_from_token(token: str, db: Session) -> Principal:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="invalid token")
    user = db.get(User, payload.get("sub"))
    if user is None or user.org_id != payload.get("org"):
        raise HTTPException(status_code=401, detail="unknown principal")
    return Principal(user.id, user.org_id, user.role)


def current_principal(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
    db: Session = Depends(get_db),
) -> Principal:
    return _principal_from_token(creds.credentials, db)


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def leadforge_principal(request: Request, db: Session = Depends(get_db)) -> Principal:
    """Server-to-server auth for the LeadForge webhook: a long-lived per-org secret in the
    X-LeadForge-Secret header, or a Bearer JWT for a ceo/dept_head as fallback."""
    secret = request.headers.get("x-leadforge-secret")
    if secret:
        org = db.scalars(
            select(Organization).where(Organization.webhook_secret_hash == hash_secret(secret))
        ).first()
        if org is None:
            raise HTTPException(status_code=401, detail="invalid webhook secret")
        return Principal("leadforge-webhook", org.id, "dept_head")
    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing credentials")
    p = _principal_from_token(auth_header.split(" ", 1)[1], db)
    if p.role not in ("ceo", "dept_head"):
        raise HTTPException(status_code=403, detail="requires ceo or dept_head")
    return p


def require_role(*roles: str):
    def dep(p: Principal = Depends(current_principal)) -> Principal:
        if p.role not in roles:
            raise HTTPException(status_code=403, detail=f"requires role in {roles}")
        return p

    return dep
