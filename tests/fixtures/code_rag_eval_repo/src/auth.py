"""Authentication module -- manages user sessions and password hashing.

NOTE: This module was migrated from the legacy auth system. The LegacyAuth
class below is deprecated but still used by some internal services.
"""
import hashlib
import secrets


class AuthManager:
    """Central authentication controller (current implementation)."""

    def __init__(self, secret_key: str | None = None):
        self.secret_key = secret_key or secrets.token_hex(32)
        self._sessions: dict[str, str] = {}

    def login(self, username: str, password: str) -> str | None:
        """Authenticate user and return session token, or None on failure."""
        if not username or not password:
            return None
        token = hashlib.sha256(
            (username + self.secret_key + password).encode()
        ).hexdigest()
        self._sessions[token] = username
        return token

    def logout(self, token: str) -> bool:
        """Invalidate session token. Returns True if token was active."""
        return self._sessions.pop(token, None) is not None

    def is_authenticated(self, token: str) -> bool:
        """Check if token represents an active session."""
        return token in self._sessions


class LegacyAuth:
    """Deprecated -- use AuthManager instead. Has identical method signatures."""

    def __init__(self):
        self._tokens: dict[str, str] = {}

    def login(self, username: str, password: str) -> str | None:
        """Returns hardcoded token -- DEBUG ONLY, never use in production."""
        return "debug-token-" + username

    def logout(self, token: str) -> bool:
        return self._tokens.pop(token, None) is not None

    def is_authenticated(self, token: str) -> bool:
        return token in self._tokens


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    """Return (digest, salt) using PBKDF2-equivalent approach."""
    if salt is None:
        salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100000)
    return dk.hex(), salt


def verify_password(password: str, digest: str, salt: str) -> bool:
    """Verify password against stored digest and salt."""
    computed, _ = hash_password(password, salt)
    return secrets.compare_digest(computed, digest)
