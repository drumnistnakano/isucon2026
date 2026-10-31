import base64
import hashlib
import hmac
import logging
import os
import sqlite3
import threading
import time
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote, urlencode

IMAGE_URL_LIFETIME = 24 * 60 * 60
IMAGE_SIGNATURE_ITERATIONS = 1000
# 期限を揃えて、ワーカーやサーバーが違っても同じ署名を再利用できるようにする
IMAGE_URL_EXP_STEP = 6 * 60 * 60
IMAGE_SIGNATURE_CACHE_SIZE = 65536
IMAGE_SIGNATURE_STORE_PATH = os.getenv(
    "ISUREAL_IMAGE_SIGNATURE_STORE", "/home/isucon/cache/image-signatures.sqlite3"
)

logger = logging.getLogger(__name__)


class SignatureStore:
    """計算した署名をワーカー間で共有し、再起動後も残す

    署名は入力だけで決まるので、無効化も initialize でのクリアも要らない。
    読み書きに失敗しても計算し直すだけなので、例外は外に出さない。
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._local = threading.local()

    def _connection(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None and self._local.pid == os.getpid():
            return conn
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._path, timeout=0.05, isolation_level=None)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = OFF")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS signatures (
              key BLOB NOT NULL,
              image_id TEXT NOT NULL,
              exp INTEGER NOT NULL,
              signature BLOB NOT NULL,
              PRIMARY KEY (key, image_id, exp)
            ) WITHOUT ROWID
            """
        )
        self._local.conn = conn
        self._local.pid = os.getpid()
        return conn

    def get(self, key: bytes, image_id: str, exp: int) -> bytes | None:
        try:
            row = (
                self._connection()
                .execute(
                    "SELECT signature FROM signatures WHERE key = ? AND image_id = ? AND exp = ?",
                    (key, image_id, exp),
                )
                .fetchone()
            )
        except (sqlite3.Error, OSError):
            logger.exception("failed to read image signature")
            return None
        return row[0] if row else None

    def put(self, key: bytes, image_id: str, exp: int, signature: bytes) -> None:
        try:
            self._connection().execute(
                "INSERT OR IGNORE INTO signatures (key, image_id, exp, signature) VALUES (?, ?, ?, ?)",
                (key, image_id, exp, signature),
            )
        except (sqlite3.Error, OSError):
            logger.exception("failed to write image signature")


_signature_store = SignatureStore(IMAGE_SIGNATURE_STORE_PATH)


def image_signing_key(secret_key_hash: str) -> bytes:
    try:
        key = bytes.fromhex(secret_key_hash)
    except ValueError:
        key = b""
    if len(key) != hashlib.sha256().digest_size:
        raise ValueError("invalid image signing key")
    return key


@lru_cache(maxsize=IMAGE_SIGNATURE_CACHE_SIZE)
def image_signature(key: bytes, image_id: str, exp: int) -> bytes:
    stored = _signature_store.get(key, image_id, exp)
    if stored is not None:
        return stored
    value = f"{exp}:{image_id}".encode()
    for _ in range(IMAGE_SIGNATURE_ITERATIONS):
        value = hmac.digest(key, value, "sha256")
    # 発行した期限の形だけ保存し、攻撃で送られた任意の exp では増やさない
    if exp % IMAGE_URL_EXP_STEP == 0:
        _signature_store.put(key, image_id, exp, value)
    return value


def encode_signature(signature: bytes) -> str:
    return base64.urlsafe_b64encode(signature).rstrip(b"=").decode()


def signed_image_url(key: bytes, image_id: str) -> str:
    expires_at = int(time.time()) + IMAGE_URL_LIFETIME
    exp = expires_at // IMAGE_URL_EXP_STEP * IMAGE_URL_EXP_STEP
    query = urlencode(
        {"exp": exp, "sig": encode_signature(image_signature(key, image_id, exp))}
    )
    return f"/api/images/{quote(image_id, safe='$&+,;=:@')}?{query}"
