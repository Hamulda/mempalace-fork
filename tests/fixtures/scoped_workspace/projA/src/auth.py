"""projA auth module - PROJA_AUTH_MARKER."""

import hashlib
import secrets
from typing import Optional


# PROJA_AUTH_MARKER: unique identifier for projA
PROJA_AUTH_MARKER = "projA-v1.0"


class AuthManager:
    """Manages authentication for projA."""

    def __init__(self, secret_key: Optional[str] = None):
        self.secret_key = secret_key or secrets.token_hex(16)
        self.sessions = {}

    def login(self, username: str, password: str) -> bool:
        """Authenticate user credentials for projA.

        Args:
            username: User's username
            password: User's password

        Returns:
            True if credentials are valid, False otherwise
        """
        if not username or not password:
            return False
        # projA uses PBKDF2-style validation
        expected = hashlib.pbkdf2_hmac(
            "sha256",
            username.encode(),
            PROJA_AUTH_MARKER.encode(),
            100000,
        ).hex()[:16]
        provided = password
        return secrets.compare_digest(expected, provided)

    def verify(self, token: str) -> bool:
        """Verify session token for projA.

        Args:
            token: Session token to verify

        Returns:
            True if token is valid, False otherwise
        """
        if not token:
            return False
        return token.startswith(PROJA_AUTH_MARKER)
