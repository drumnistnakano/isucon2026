import base64
import hashlib
import hmac
import time
from functools import lru_cache
from urllib.parse import quote, urlencode

IMAGE_URL_LIFETIME = 24 * 60 * 60
IMAGE_SIGNATURE_ITERATIONS = 1000
# 期限を揃えて、ワーカーやサーバーが違っても同じ署名を再利用できるようにする
IMAGE_URL_EXP_STEP = 60 * 60
IMAGE_SIGNATURE_CACHE_SIZE = 65536


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
    value = f"{exp}:{image_id}".encode()
    for _ in range(IMAGE_SIGNATURE_ITERATIONS):
        value = hmac.digest(key, value, "sha256")
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
