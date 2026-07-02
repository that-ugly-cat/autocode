"""
TOTP (RFC 6238) and backup codes — pure standard library, no pyotp dependency.

Standard time-based one-time passwords: 6 digits, 30-second step, HMAC-SHA1.
Compatible with any authenticator app (Ente Auth, Google Authenticator, Aegis…).
Keeping it stdlib means the smoke test can generate valid codes without installing
anything, and the app carries no extra runtime dependency for the algorithm itself
(only the optional QR image needs qrcode/Pillow).
"""
import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

DIGITS = 6
STEP = 30


def generate_secret() -> str:
    """A fresh base32 secret (160 bits) for a new enrollment."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _hotp(secret_b32: str, counter: int) -> str:
    key = base64.b32decode(secret_b32 + "=" * (-len(secret_b32) % 8))
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** DIGITS)
    return str(code).zfill(DIGITS)


def now(secret_b32: str, at: float | None = None) -> str:
    return _hotp(secret_b32, int((at if at is not None else time.time()) // STEP))


def verify(secret_b32: str, code: str, window: int = 1, at: float | None = None) -> bool:
    """Accept the current code ±window steps, to tolerate clock skew."""
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit():
        return False
    counter = int((at if at is not None else time.time()) // STEP)
    return any(hmac.compare_digest(_hotp(secret_b32, counter + e), code)
               for e in range(-window, window + 1))


def provisioning_uri(secret_b32: str, account: str, issuer: str = "Autocode") -> str:
    label = quote(f"{issuer}:{account}")
    return (f"otpauth://totp/{label}?secret={secret_b32}"
            f"&issuer={quote(issuer)}&digits={DIGITS}&period={STEP}")


def qr_data_uri(uri: str) -> str | None:
    """PNG QR of the otpauth URI as a data: URI. None if qrcode/Pillow is unavailable
    (the caller can still show the secret/URI for manual entry)."""
    try:
        import io
        import qrcode
        img = qrcode.make(uri)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return None


# ── Backup codes ──────────────────────────────────────────────────────────────

N_BACKUP_CODES = 10


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.strip().lower().replace("-", "").encode()).hexdigest()


def generate_backup_codes() -> tuple[list[str], list[str]]:
    """Returns (plaintext codes shown once, sha256 hashes to store). High entropy,
    so a fast hash is fine — no need for bcrypt's slowness here."""
    plain = []
    for _ in range(N_BACKUP_CODES):
        raw = secrets.token_hex(4)  # 8 hex chars
        plain.append(f"{raw[:4]}-{raw[4:]}")
    return plain, [_hash_code(c) for c in plain]


def consume_backup_code(code: str, hashes: list[str]) -> list[str] | None:
    """If `code` matches a stored hash, return the remaining hashes (code consumed);
    otherwise None."""
    h = _hash_code(code)
    if h in hashes:
        return [x for x in hashes if x != h]
    return None
