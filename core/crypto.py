"""
core/crypto.py

Shared password encryption helpers for at-rest storage.

Uses XOR with a PBKDF2-derived key stream — lightweight, stdlib-only,
and sufficient for protecting passwords in a local SQLite / TOML file
on the sysop's machine.

Encrypted values are prefixed with ``ENC:`` so callers can distinguish
encrypted from plaintext strings (useful for migration).
"""

import base64
import hashlib
import secrets


ENC_PREFIX = "ENC:"


def _derive_key(machine_secret: str, salt: str, length: int = 256) -> bytes:
    """Derive a key stream from machine_secret and a per-password salt using PBKDF2."""
    return hashlib.pbkdf2_hmac(
        "sha256",
        machine_secret.encode("utf-8"),
        f"pw-enc:{salt}".encode("utf-8"),
        iterations=100_000,
        dklen=length,
    )


def encrypt_password(password: str, machine_secret: str, salt: str) -> str:
    """Encrypt a password for at-rest storage. Returns base64 string."""
    pw_bytes = password.encode("utf-8")
    key = _derive_key(machine_secret, salt, length=len(pw_bytes))
    encrypted = bytes(pb ^ kb for pb, kb in zip(pw_bytes, key))
    return base64.b64encode(encrypted).decode("ascii")


def decrypt_password(encrypted_b64: str, machine_secret: str, salt: str) -> str:
    """Decrypt a stored password. Returns the raw password string."""
    enc_bytes = base64.b64decode(encrypted_b64)
    key = _derive_key(machine_secret, salt, length=len(enc_bytes))
    decrypted = bytes(eb ^ kb for eb, kb in zip(enc_bytes, key))
    return decrypted.decode("utf-8")


def encrypt_value(plaintext: str, machine_secret: str) -> str:
    """Encrypt a string and return ``ENC:<salt>:<base64>`` for storage.

    Returns empty string if *plaintext* is empty.
    """
    if not plaintext:
        return ""
    salt = secrets.token_hex(16)
    enc = encrypt_password(plaintext, machine_secret, salt)
    return f"{ENC_PREFIX}{salt}:{enc}"


def decrypt_value(stored: str, machine_secret: str) -> str:
    """Decrypt a value produced by :func:`encrypt_value`.

    If *stored* is not encrypted (no ``ENC:`` prefix), returns it as-is
    (plaintext fallback for migration).  Returns empty string for empty input.
    """
    if not stored:
        return ""
    if not stored.startswith(ENC_PREFIX):
        return stored  # plaintext — not yet migrated
    payload = stored[len(ENC_PREFIX):]
    salt, enc_b64 = payload.split(":", 1)
    return decrypt_password(enc_b64, machine_secret, salt)
