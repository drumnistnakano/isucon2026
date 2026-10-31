import io
import uuid
import zipfile
from http import HTTPStatus
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import TypeAdapter

from ..auth import CurrentSession, RegionalSession
from ..binding import RequestBody, json_body
from ..db import DB, execute, fetch_all, fetch_one, from_row
from ..errors import APIError, NoRowsError
from ..image_cache import ImageCache
from ..jpeg import jpeg_failure
from ..models.post import POST_COLUMNS, Post
from ..models.user import User, get_user, user_columns, valid_region, valid_user_text
from ..notification_hub import NOTIFY_QUERY, user_key
from ..schemas import ExportPostResponse, MeResponse

router = APIRouter(prefix="/api/me")


class UpdateMeRequest(RequestBody):
    display_name: str | None = None
    bio: str | None = None
    region: str | None = None


@router.get("")
async def get_me(db: DB, session: RegionalSession) -> MeResponse:
    user = await get_user(db, session.user_id)
    if user is None:
        raise APIError(HTTPStatus.NOT_FOUND, "account_not_found")
    return MeResponse.of(user)


@router.patch("")
async def update_me(
    db: DB,
    session: CurrentSession,
    body: Annotated[UpdateMeRequest, Depends(json_body(UpdateMeRequest))],
) -> MeResponse:
    if (
        (body.display_name is None and body.bio is None and body.region is None)
        or (
            body.display_name is not None
            and not valid_user_text(body.display_name, 1, 100)
        )
        or (body.bio is not None and not valid_user_text(body.bio, 0, 500))
        or (body.region is not None and not valid_region(body.region))
    ):
        raise APIError(HTTPStatus.BAD_REQUEST, "invalid_profile")
    async with db.transaction():
        for column, value in (
            ("display_name", body.display_name),
            ("bio", body.bio),
            ("region", body.region),
        ):
            if value is not None:
                await execute(
                    db,
                    f"UPDATE users SET {column} = %(value)s, updated_at = clock_timestamp() WHERE id = %(id)s",
                    {"id": session.user_id, "value": value},
                )
        if body.region is not None:
            # リージョンが変わったら、待っている SSE を終わらせる
            await execute(db, NOTIFY_QUERY, {"key": user_key(session.user_id)})
        user = await get_user(db, session.user_id)
        if user is None:
            raise APIError(HTTPStatus.NOT_FOUND, "account_not_found")
    return MeResponse.of(user)


@router.put("/profile-image", status_code=HTTPStatus.NO_CONTENT)
async def put_profile_image(
    db: DB, session: CurrentSession, request: Request
) -> Response:
    if request.headers.get("content-type") != "image/jpeg":
        raise APIError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "unsupported_media_type")
    data = await request.body()
    if failure := await jpeg_failure(data, 512):
        if failure == "invalid_image":
            request.state.abuse = "invalid_image"
        raise APIError(HTTPStatus.BAD_REQUEST, "invalid_profile_image")
    async with db.transaction():
        row = await fetch_one(
            db,
            "SELECT profile_image_id FROM users WHERE id = %(id)s FOR NO KEY UPDATE",
            {"id": session.user_id},
        )
        if row is None:
            raise APIError(HTTPStatus.NOT_FOUND, "account_not_found")
        image_id = str(uuid.uuid4())
        await execute(
            db,
            """
            INSERT INTO image_objects (id, owner_user_id, data, media_type)
            VALUES (%(id)s, %(user_id)s, %(data)s, 'image/jpeg')
            """,
            {"id": image_id, "user_id": session.user_id, "data": data},
        )
        await execute(
            db,
            "UPDATE users SET profile_image_id = %(image_id)s, updated_at = clock_timestamp() WHERE id = %(id)s",
            {"id": session.user_id, "image_id": image_id},
        )
        if row["profile_image_id"] is not None:
            await execute(
                db,
                "DELETE FROM image_objects WHERE id = %(id)s",
                {"id": row["profile_image_id"]},
            )
    return Response(status_code=HTTPStatus.NO_CONTENT)


@router.delete("/profile-image", status_code=HTTPStatus.NO_CONTENT)
async def delete_profile_image(db: DB, session: CurrentSession) -> Response:
    async with db.transaction():
        row = await fetch_one(
            db,
            "SELECT profile_image_id FROM users WHERE id = %(id)s FOR NO KEY UPDATE",
            {"id": session.user_id},
        )
        if row is None or row["profile_image_id"] is None:
            raise APIError(HTTPStatus.NOT_FOUND, "account_not_found")
        await execute(
            db,
            "UPDATE users SET profile_image_id = NULL, updated_at = clock_timestamp() WHERE id = %(id)s",
            {"id": session.user_id},
        )
        await execute(
            db,
            "DELETE FROM image_objects WHERE id = %(id)s",
            {"id": row["profile_image_id"]},
        )
    return Response(status_code=HTTPStatus.NO_CONTENT)


@router.post("/account-deletion", status_code=HTTPStatus.NO_CONTENT)
async def delete_account(db: DB, session: RegionalSession) -> Response:
    async with db.transaction():
        row = await fetch_one(
            db,
            "SELECT id FROM users WHERE id = %(id)s AND status = 'active' FOR NO KEY UPDATE",
            {"id": session.user_id},
        )
        if row is None:
            raise APIError(HTTPStatus.NOT_FOUND, "account_not_found")
        await execute(
            db,
            """
            UPDATE users SET status = 'deleted', deleted_at = clock_timestamp(), updated_at = clock_timestamp()
            WHERE id = %(id)s
            """,
            {"id": session.user_id},
        )
        await execute(
            db,
            """
            UPDATE login_sessions
            SET active = false, invalidated_at = clock_timestamp(), reason = 'account_deletion'
            WHERE user_id = %(id)s AND active = true
            """,
            {"id": session.user_id},
        )
        # 待っている SSE を終わらせる
        await execute(db, NOTIFY_QUERY, {"key": user_key(session.user_id)})
    return Response(status_code=HTTPStatus.NO_CONTENT)


_export_manifest = TypeAdapter(list[ExportPostResponse])


@router.get("/export")
async def export_posts(db: DB, session: RegionalSession, request: Request) -> Response:
    cache: ImageCache = request.app.state.image_cache
    # 投稿者と投稿一覧は独立なので1往復にまとめる（リージョンをまたぐと1往復が重い）
    async with db.pipeline():
        author_cursor = await db.execute(
            f"SELECT {user_columns()} FROM users WHERE id = %(id)s AND status = 'active'",
            {"id": session.user_id},
        )
        posts_cursor = await db.execute(
            f"""
            SELECT {POST_COLUMNS}
            FROM posts p
            JOIN posting_windows w ON w.id = p.posting_window_id
            WHERE p.author_id = %(user_id)s AND p.deleted_at IS NULL
            ORDER BY p.posted_at, p.id
            """,
            {"user_id": session.user_id},
        )
    author_row = await author_cursor.fetchone()
    if author_row is None:
        raise NoRowsError
    author = from_row(User, author_row)
    rows = await posts_cursor.fetchall()

    # 画像は DB から運ばず、まずローカルキャッシュを見る。
    # 無い分だけまとめて1回で取り、次回以降のためにキャッシュへ入れる。
    images: dict[str, bytes | None] = {}
    missing: list[str] = []
    for row in rows:
        image_id = row["image_id"]
        if image_id in images:
            continue
        images[image_id] = cache.get(image_id)
        if images[image_id] is None:
            missing.append(image_id)
    if missing:
        for row in await fetch_all(
            db,
            "SELECT id, data FROM image_objects WHERE id = ANY(%(ids)s)",
            {"ids": missing},
        ):
            images[row["id"]] = row["data"]
            cache.put(row["id"], row["data"])

    # 画像が見つからない投稿は従来どおり出力しない
    included: list[tuple[Any, bytes]] = []
    for row in rows:
        data = images.get(row["image_id"])
        if data is not None:
            included.append((row, data))
    body = io.BytesIO()
    items: list[ExportPostResponse] = []
    with zipfile.ZipFile(body, "w") as archive:
        for number, (row, image_data) in enumerate(included, start=1):
            image_path = f"images/{number:06d}.jpg"
            archive.writestr(image_path, image_data, compress_type=zipfile.ZIP_STORED)
            items.append(
                ExportPostResponse.of(Post.from_row(row), author, image_path=image_path)
            )
        archive.writestr(
            "posts.json",
            _export_manifest.dump_json(items) + b"\n",
            compress_type=zipfile.ZIP_DEFLATED,
        )
    return Response(
        body.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="isureal-posts.zip"',
            "Cache-Control": "no-store",
        },
    )
