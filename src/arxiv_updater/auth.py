import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, Request, status
from pwdlib import PasswordHash
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Invite, User, UserRole

password_hash = PasswordHash.recommended()


def normalize_email(email: str) -> str:
    return email.strip().lower()


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, encoded: str) -> bool:
    return password_hash.verify(password, encoded)


def authenticate(db: Session, email: str, password: str) -> User | None:
    user = db.scalar(
        select(User).where(User.email == normalize_email(email), User.is_active.is_(True))
    )
    if user and verify_password(password, user.password_hash):
        return user
    return None


def create_user(
    db: Session,
    email: str,
    password: str,
    display_name: str,
    role: UserRole = UserRole.MEMBER,
) -> User:
    normalized = normalize_email(email)
    if db.scalar(select(User).where(User.email == normalized)):
        raise ValueError("该邮箱已经注册")
    if len(password) < 10:
        raise ValueError("密码至少需要 10 个字符")
    user = User(
        email=normalized,
        password_hash=hash_password(password),
        display_name=display_name.strip() or normalized.split("@")[0],
        role=role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def create_invite(db: Session, created_by_id: str | None = None, days: int = 7) -> str:
    raw = secrets.token_urlsafe(24)
    db.add(
        Invite(
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            created_by_id=created_by_id,
            expires_at=datetime.now(UTC) + timedelta(days=days),
        )
    )
    db.commit()
    return raw


def consume_invite(db: Session, token: str) -> Invite:
    digest = hashlib.sha256(token.encode()).hexdigest()
    invite = db.scalar(select(Invite).where(Invite.token_hash == digest))
    now = datetime.now(UTC)
    if invite and not invite.expires_at.tzinfo:
        expires_at = invite.expires_at.replace(tzinfo=UTC)
    else:
        expires_at = invite.expires_at if invite else now
    if not invite or invite.used_at or expires_at <= now:
        raise ValueError("邀请码无效或已过期")
    invite.used_at = now
    db.commit()
    return invite


def get_current_user(request: Request, db: Session) -> User:
    user_id = request.session.get("user_id")
    user = db.get(User, user_id) if user_id else None
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    return user


def require_admin(user: User) -> None:
    if user.role != UserRole.ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
