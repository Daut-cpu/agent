"""Токены пользователей, роли в проекте и шифрование данных в хранилище."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets

from cryptography.fernet import Fernet, InvalidToken

ROLES = ("viewer", "editor", "owner")


def new_token() -> str:
    return "saa_" + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def token_matches(token: str, token_hash: str) -> bool:
    return hmac.compare_digest(hash_token(token), token_hash)


def role_allows(role: str | None, required: str) -> bool:
    """viewer < editor < owner."""
    if role not in ROLES:
        return False
    return ROLES.index(role) >= ROLES.index(required)


class Cipher:
    """Шифрует содержимое документов и артефактов, если задан ключ.

    Ключ — `ANALYST_ENCRYPTION_KEY` (Fernet, `python -m analyst gen-key`).
    Без ключа данные хранятся открыто — допустимо только для локальной разработки.
    """

    PREFIX = "fernet:"

    def __init__(self, key: str | None = None):
        key = key if key is not None else os.environ.get("ANALYST_ENCRYPTION_KEY", "")
        self._fernet = Fernet(key.encode()) if key else None

    @property
    def enabled(self) -> bool:
        return self._fernet is not None

    def encrypt(self, text: str) -> str:
        if not self._fernet:
            return text
        return self.PREFIX + self._fernet.encrypt(text.encode()).decode()

    def decrypt(self, value: str) -> str:
        if not value.startswith(self.PREFIX):
            return value
        if not self._fernet:
            raise RuntimeError("Данные зашифрованы, но ANALYST_ENCRYPTION_KEY не задан")
        try:
            return self._fernet.decrypt(value[len(self.PREFIX):].encode()).decode()
        except InvalidToken as e:
            raise RuntimeError("Неверный ANALYST_ENCRYPTION_KEY") from e

    @staticmethod
    def generate_key() -> str:
        return Fernet.generate_key().decode()
