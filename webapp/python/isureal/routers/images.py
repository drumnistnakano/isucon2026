import base64
import hmac
import re
import time
from http import HTTPStatus
from urllib.parse import parse_qs

from fastapi import APIRouter, Request, Response

from ..db import DB, fetch_one
from ..errors import APIError
from ..image_cache import ImageCache
from ..image_signature import encode_signature, image_signature, image_signing_key

router = APIRouter(prefix="/api/images")

NO_STORE = {"Cache-Control": "no-store"}
_SIGNATURE = re.compile(r"[A-Za-z0-9_-]{43}", re.ASCII)
_INVALID_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")


def image_forbidden() -> APIError:
    return APIError(HTTPStatus.FORBIDDEN, "image_forbidden", headers=NO_STORE)


def parse_signed_query(raw_query: str) -> tuple[int, bytes]:
    """exp と sig をちょうど 1 つずつ含むクエリを解釈する"""
    if ";" in raw_query or _INVALID_ESCAPE.search(raw_query):
        raise image_forbidden()
    query = parse_qs(raw_query, keep_blank_values=True)
    if len(query.get("exp", [])) != 1 or len(query.get("sig", [])) != 1:
        raise image_forbidden()
    [exp_text], [sig_text] = query["exp"], query["sig"]
    try:
        exp = int(exp_text)
    except ValueError:
        raise image_forbidden() from None
    if str(exp) != exp_text or exp <= time.time():
        raise image_forbidden()
    if not _SIGNATURE.fullmatch(sig_text):
        raise image_forbidden()
    sig = base64.urlsafe_b64decode(sig_text + "=")
    if encode_signature(sig) != sig_text:
        raise image_forbidden()
    return exp, sig


@router.get("/{image_id}")
async def get_image(db: DB, image_id: str, request: Request) -> Response:
    exp, sig = parse_signed_query(request.url.query)

    cache: ImageCache = request.app.state.image_cache
    cached = cache.get(image_id)
    # キャッシュに無ければ data も同時に取る（1往復で済ませる）
    columns = "u.secret_key_hash, i.media_type"
    if cached is None:
        columns += ", i.data"
    image = await fetch_one(
        db,
        f"""
        SELECT {columns}
        FROM image_objects i JOIN users u ON u.id = i.owner_user_id AND u.status = 'active'
        WHERE i.id = %(id)s AND (
          u.profile_image_id = i.id
          OR EXISTS (
            SELECT 1 FROM posts p
            WHERE p.image_id = i.id AND p.author_id = u.id AND p.deleted_at IS NULL
          )
          OR EXISTS (
            SELECT 1 FROM realmojis r
            JOIN posts p ON p.id = r.post_id AND p.deleted_at IS NULL
            JOIN users author ON author.id = p.author_id AND author.status = 'active'
            WHERE r.image_id = i.id AND r.reactor_id = u.id AND r.replaced_at IS NULL AND r.deleted_at IS NULL
          )
        )
        """,
        {"id": image_id},
    )
    if image is None:
        raise image_forbidden()
    key = image_signing_key(image["secret_key_hash"])
    if not hmac.compare_digest(sig, image_signature(key, image_id, exp)):
        request.state.abuse = "invalid_signature"
        raise image_forbidden()
    if exp <= time.time():
        raise image_forbidden()

    if cached is None:
        # 画像の中身は不変なので、取得できたものをそのまま保存してよい
        cached = image["data"]
        cache.put(image_id, cached)
    return Response(cached, media_type=image["media_type"], headers=NO_STORE)
