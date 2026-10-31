import hashlib
import uuid
from datetime import datetime, timedelta
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from pydantic import Field

from ..auth import CurrentAdminSession
from ..binding import RequestBody, json_body
from ..db import DB, execute, fetch_one, fetch_value
from ..errors import APIError, NoRowsError
from ..models.posting_window import get_posting_window
from ..models.session import create_admin_session, invalidate_admin_session
from ..models.user import valid_region, valid_username
from ..notification_hub import NOTIFY_QUERY, region_key
from ..schemas import AdminLoginResponse, PostingWindowResponse
from ..timeutil import parse_date

router = APIRouter(prefix="/api/admin")


class AdminLoginRequest(RequestBody):
    username: str = ""
    password: str = ""


@router.post("/auth/login")
async def admin_login(
    db: DB, body: Annotated[AdminLoginRequest, Depends(json_body(AdminLoginRequest))]
) -> AdminLoginResponse:
    if not valid_username(body.username):
        raise APIError(HTTPStatus.UNAUTHORIZED, "invalid_credentials")
    administrator = await fetch_one(
        db,
        "SELECT id, password_hash FROM administrators WHERE username = %(username)s",
        {"username": body.username},
    )
    password_hash = hashlib.sha256(body.password.encode()).hexdigest()
    if administrator is None or administrator["password_hash"] != password_hash:
        raise APIError(HTTPStatus.UNAUTHORIZED, "invalid_credentials")
    async with db.transaction():
        token = await create_admin_session(db, administrator["id"])
    return AdminLoginResponse(token=token, administrator_id=administrator["id"])


@router.post("/auth/logout", status_code=HTTPStatus.NO_CONTENT)
async def admin_logout(db: DB, session: CurrentAdminSession) -> Response:
    await invalidate_admin_session(db, session)
    return Response(status_code=HTTPStatus.NO_CONTENT)


@router.put("/users/{user_id}/certification", status_code=HTTPStatus.NO_CONTENT)
async def certify_user(db: DB, session: CurrentAdminSession, user_id: str) -> Response:
    if not await execute(
        db,
        """
        UPDATE users SET account_type = 'certified_public', updated_at = clock_timestamp()
        WHERE id = %(id)s AND status = 'active' AND account_type = 'regular'
        """,
        {"id": user_id},
    ):
        raise APIError(HTTPStatus.CONFLICT, "certification_conflict")
    return Response(status_code=HTTPStatus.NO_CONTENT)


# 2026-08-26: 認定されたユーザーを戻すことは禁止する
@router.delete("/users/{user_id}/certification", status_code=HTTPStatus.NO_CONTENT)
async def delete_user_certification(
    session: CurrentAdminSession, user_id: str
) -> Response:
    raise APIError(HTTPStatus.CONFLICT, "certification_irreversible")


class OpenPostingWindowRequest(RequestBody):
    region: str = ""
    local_date: str = ""
    on_time_seconds: Annotated[int, Field(ge=-(2**63), le=2**63 - 1)] = 24 * 60 * 60


@router.post("/posting-window", status_code=HTTPStatus.CREATED)
async def open_posting_window(
    db: DB,
    session: CurrentAdminSession,
    body: Annotated[
        OpenPostingWindowRequest, Depends(json_body(OpenPostingWindowRequest))
    ],
) -> PostingWindowResponse:
    local_date = parse_date(body.local_date)
    if not valid_region(body.region) or local_date is None:
        raise APIError(HTTPStatus.BAD_REQUEST, "invalid_posting_window")
    async with db.transaction():
        # 同じリージョンの「投稿期間を開く」処理だけを直列化する。
        # 以前は LOCK TABLE users IN SHARE ROW EXCLUSIVE MODE で users 全体の
        # 書き込み（INSERT/UPDATE/DELETE）を、下の通知作成が終わるまで止めていた。
        await execute(
            db,
            "SELECT pg_advisory_xact_lock(hashtext(%(key)s)::bigint)",
            {"key": f"posting_window:{body.region}"},
        )
        if await fetch_value(
            db,
            """
            SELECT EXISTS(
              SELECT 1 FROM posting_windows WHERE region = %(region)s AND local_date >= %(local_date)s
            )
            """,
            {"region": body.region, "local_date": local_date},
        ):
            raise APIError(HTTPStatus.CONFLICT, "posting_window_conflict")
        starts_at: datetime = await fetch_value(db, "SELECT clock_timestamp()")
        await execute(
            db,
            "UPDATE posting_windows SET ended_at = %(starts_at)s WHERE region = %(region)s AND ended_at IS NULL",
            {"region": body.region, "starts_at": starts_at},
        )
        window_id = str(uuid.uuid4())
        await execute(
            db,
            """
            INSERT INTO posting_windows (id, region, local_date, starts_at, on_time_deadline)
            VALUES (%(id)s, %(region)s, %(local_date)s, %(starts_at)s, %(on_time_deadline)s)
            """,
            {
                "id": window_id,
                "region": body.region,
                "local_date": local_date,
                "starts_at": starts_at,
                "on_time_deadline": starts_at + timedelta(seconds=body.on_time_seconds),
            },
        )
        # 通知は SSE の接続後に作られたものだけが届き、接続にはログインが必要。
        # 初期化以降にログインしていないユーザーの行は誰にも読まれないので作らない
        window = await get_posting_window(db, window_id)
        if window is None:
            raise NoRowsError

    # 通知の作成は投稿期間の行ロックを離してから行う。
    # ここは対象ユーザー全員ぶんの INSERT で数百msかかるため、トランザクション内に
    # 入れたままだと、その間ずっと posting_windows の行が FOR SHARE で待たされ、
    # 投稿（フィード検証10点の前提）が詰まる。
    # 待っている SSE を起こす。NOTIFY は COMMIT 時に届くので、受け取った側には通知の行が見える
    await execute(
        db,
        f"""
        WITH inserted AS (
          INSERT INTO notifications (id, user_id, posting_window_id, created_at)
          SELECT gen_random_uuid()::text, u.id, %(window_id)s, %(starts_at)s
          FROM users u
          WHERE u.region = %(region)s AND u.status = 'active'
            AND EXISTS (SELECT 1 FROM login_sessions s WHERE s.user_id = u.id)
        )
        {NOTIFY_QUERY}
        """,
        {
            "region": body.region,
            "window_id": window_id,
            "starts_at": starts_at,
            "key": region_key(body.region),
        },
    )
    return PostingWindowResponse.of(window)
