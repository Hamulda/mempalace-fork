"""projB auth module - PROJB_AUTH_MARKER."""

import hashlib
import secrets
from typing import Optional


# PROJB_AUTH_MARKER: unique identifier for projB
PROJB_AUTH_MARKER = "projB-v2.1"


class AuthManager:
    """Manages authentication for projB."""

    def __init__(self, secret_key: Optional[str] = None):
        self.secret_key = secret_key or secrets.token_hex(16)
        self.sessions = {}

    def login(self, username: str, password: str) -> bool:
        """Authenticate user credentials for projB.

        Args:
            username: User's username
            password: User's password

        Returns:
            True if credentials are valid, False otherwise
        """
        if not username or not password:
            return False
        # projB uses scrypt-style validation
        expected = hashlib.scrypt(
            password.encode(),
            salt=PROJB_AUTH_MARKER.encode(),
            n=16384,
            r=8,
            p=1,
        ).hex()[:16]
        provided = password
        return secrets.compare_digest(expected, provided)

    def verify(self, token: str) -> bool:
        """Verify session token for projB.

        Args:
            token: Session token to verify

        Returns:
            True if token is valid, False otherwise
        """
        if not token:
            return False
        return token.startswith(PROJB_AUTH_MARKER)
