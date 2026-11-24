"""账号与会话：一次性安装码注册、登录、会话 token（只存哈希）、改密。

设计取舍：
- 只有一个人用，所以开放注册会让「先下载 APK 的人」抢注账号 → 注册必须带 INSTALL_CODE。
- 会话 token 90 天，数据库只存 sha256，可吊销；App 端无需保存密码。
- 兼容旧的 AUTH_TOKEN（脚本/旧设备继续可用）。
"""
import base64
import hashlib
import hmac
import logging
import os
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from . import db

logger = logging.getLogger("core.auth")

PBKDF2_ROUNDS = 210_000
SESSION_DAYS = 90
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return "pbkdf2_sha256$%d$%s$%s" % (PBKDF2_ROUNDS, _b64(salt), _b64(dk))


def verify_password(pw: str, stored: str) -> bool:
    try:
        algo, rounds, salt_b64, hash_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_b64 + "==")
        want = base64.urlsafe_b64decode(hash_b64 + "==")
        got = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, int(rounds))
        return hmac.compare_digest(got, want)
    except Exception:
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def valid_username(name: str) -> bool:
    return bool(USERNAME_RE.match(name or ""))


async def count_users(sf) -> int:
    async with sf() as s:
        rows = (await s.execute(select(db.User.id))).scalars().all()
        return len(rows)


async def get_user(sf, username: str):
    async with sf() as s:
        return (
            await s.execute(select(db.User).where(db.User.username == username))
        ).scalars().first()


async def create_user(sf, username: str, password: str):
    async with sf() as s:
        user = db.User(username=username, password_hash=hash_password(password))
        s.add(user)
        await s.commit()
        await s.refresh(user)
        return user


async def _issue_session(sf, user_id: str, days: int = SESSION_DAYS):
    token = os.urandom(32).hex()
    expires = datetime.now(timezone.utc) + timedelta(days=days)
    async with sf() as s:
        s.add(db.AuthSession(token_hash=token_hash(token), user_id=user_id, expires_at=expires))
        await s.commit()
    return token, expires


async def login(sf, username: str, password: str):
    """成功返回 (token, expires_at, username)，失败返回 None。"""
    user = await get_user(sf, username)
    if user is None or not verify_password(password, user.password_hash):
        return None
    token, expires = await _issue_session(sf, user.id)
    async with sf() as s:
        u = await s.get(db.User, user.id)
        if u is not None:
            u.last_login_at = datetime.now(timezone.utc)
            await s.commit()
    return token, expires, user.username


async def resolve_session(sf, token: str):
    """返回 user_id；过期/吊销返回 None。"""
    if not token:
        return None
    th = token_hash(token)
    async with sf() as s:
        row = await s.get(db.AuthSession, th)
        if row is None:
            return None
        now = datetime.now(timezone.utc)
        exp = row.expires_at
        if exp is not None and exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp is not None and exp < now:
            await s.delete(row)
            await s.commit()
            return None
        row.last_seen_at = now
        await s.commit()
        return row.user_id


async def revoke_session(sf, token: str) -> None:
    async with sf() as s:
        await s.execute(delete(db.AuthSession).where(db.AuthSession.token_hash == token_hash(token)))
        await s.commit()


async def change_password(sf, user_id: str, old_password: str, new_password: str) -> bool:
    async with sf() as s:
        user = await s.get(db.User, user_id)
        if user is None or not verify_password(old_password, user.password_hash):
            return False
        user.password_hash = hash_password(new_password)
        await s.commit()
        # 改密后踢掉所有会话（含当前），强制重新登录
        await s.execute(delete(db.AuthSession).where(db.AuthSession.user_id == user_id))
        await s.commit()
        return True
