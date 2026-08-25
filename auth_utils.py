"""
Shared password-hashing helpers for activity_server.

Used by both main.py (the /admin/login endpoint) and manage.py (the
`set-admin-password` CLI command) so the two stay in sync without
depending on an extra third-party package.
"""

import hashlib
import hmac
import secrets

_ALGORITHM = "pbkdf2_sha256"
_ITERATIONS = 200_000


def hash_password(password: str) -> str:
    """Hash a plaintext password into a self-describing storable string."""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), _ITERATIONS
    )
    return f"{_ALGORITHM}${_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Check a plaintext password against a hash produced by hash_password()."""
    try:
        algo, iterations_str, salt, hash_hex = stored_hash.split("$")
        if algo != _ALGORITHM:
            return False
        iterations = int(iterations_str)
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt), iterations
        )
        return hmac.compare_digest(digest.hex(), hash_hex)
    except Exception:
        return False