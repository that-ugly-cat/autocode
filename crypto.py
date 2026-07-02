"""
Symmetric encryption for per-user Anthropic API keys.

Fernet with a server-side key from the FERNET_KEY env var (generate once with
`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`).
Keys are decrypted in memory only when a run starts, never logged.
"""
import os

from cryptography.fernet import Fernet

_fernet = Fernet(os.environ["FERNET_KEY"].encode())


def encrypt_api_key(plain: str) -> str:
    return _fernet.encrypt(plain.encode()).decode()


def decrypt_api_key(encrypted: str) -> str:
    return _fernet.decrypt(encrypted.encode()).decode()


def mask_api_key(plain: str) -> str:
    """Display form: only the last 4 characters visible."""
    tail = plain[-4:] if len(plain) >= 4 else plain
    return f"sk-…••••{tail}"
