from __future__ import annotations

import base64
import os
import threading
from pathlib import Path

from config import DATA_DIR


VAPID_PRIVATE_KEY_PATH = DATA_DIR / "vapid_private.pem"
VAPID_SUBJECT = "mailto:push@obsidianvow.app"

_key_lock = threading.Lock()


def ensure_vapid_private_key(path: Path = VAPID_PRIVATE_KEY_PATH) -> Path:
    """Create the durable private key once; an existing key is never replaced."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    path = Path(path)
    if path.exists():
        return path

    with _key_lock:
        if path.exists():
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        private_key = ec.generate_private_key(ec.SECP256R1())
        pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return path
        with os.fdopen(fd, "wb") as handle:
            handle.write(pem)
            handle.flush()
            os.fsync(handle.fileno())
    return path


def application_server_key(path: Path = VAPID_PRIVATE_KEY_PATH) -> str:
    from cryptography.hazmat.primitives import serialization

    private_path = ensure_vapid_private_key(path)
    private_key = serialization.load_pem_private_key(
        private_path.read_bytes(), password=None
    )
    raw_public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    return base64.urlsafe_b64encode(raw_public_key).rstrip(b"=").decode("ascii")
