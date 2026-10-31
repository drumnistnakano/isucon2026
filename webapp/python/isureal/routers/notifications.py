import asyncio
import logging
import time
from collections.abc import AsyncIterator
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from ..auth import RegionalSession
from ..db import DB, Pool, fetch_one, fetch_value
from ..models.session import Session
from ..notification_hub import NotificationHub, region_key, user_key
from ..schemas import NotificationResponse

router = APIRouter(prefix="/api/notifications")
logger = logging.getLogger(__name__)

# LISTEN の接続が切れていても届くように、NOTIFY が来なくてもこの間隔で DB を確認する
FALLBACK_INTERVAL = 2.0
NEXT_NOTIFICATION_QUERY = """
SELECT u.region AS user_region, n.*
FROM users u LEFT JOIN LATERAL (
  SELECT n.id, n.posting_window_id, w.region, n.created_at
  FROM notifications n JOIN posting_windows w ON w.id = n.posting_window_id
  WHERE n.user_id = u.id AND n.created_at > %(connected_at)s AND w.ended_at IS NULL
  ORDER BY n.created_at, n.id LIMIT 1
) n ON true
WHERE u.id = %(user_id)s AND u.status = 'active'
"""
# nginx の proxy_read_timeout（既定60秒）で切られないよう、待機中も定期的に送る
HEARTBEAT_INTERVAL = 10.0


async def wait_any(events: tuple[asyncio.Event, ...], timeout: float) -> None:
    waiters = [asyncio.ensure_future(event.wait()) for event in events]
    try:
        await asyncio.wait(
            waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        for waiter in waiters:
            waiter.cancel()


async def notification_events(
    pool: Pool, hub: NotificationHub, session: Session, connected_at: datetime
) -> AsyncIterator[bytes]:
    """接続後に届いた通知を 1 件送って終わる"""
    last_heartbeat = time.monotonic()
    while True:
        # DB を見る前に受け取っておけば、見た直後に来た NOTIFY も取りこぼさない
        wakeups = (
            hub.event(region_key(session.region)),
            hub.event(user_key(session.user_id)),
        )
        try:
            # 1往復で届け先の確認と通知の取得を済ませる
            async with pool.connection() as db:
                row = await fetch_one(
                    db,
                    NEXT_NOTIFICATION_QUERY,
                    {"user_id": session.user_id, "connected_at": connected_at},
                )
        except Exception:
            logger.exception("failed to get notification")
            return
        # 退会やリージョン変更で届け先がなくなったら終わる
        if row is None or row["user_region"] != session.region:
            return
        if row["id"] is not None:
            notification = NotificationResponse(
                id=row["id"],
                posting_window_id=row["posting_window_id"],
                region=row["region"],
                created_at=row["created_at"],
            )
            yield b"data: " + notification.model_dump_json().encode() + b"\n\n"
            return
        now = time.monotonic()
        if now - last_heartbeat >= HEARTBEAT_INTERVAL:
            # SSE のコメント行。クライアントは無視する
            yield b": keepalive\n\n"
            last_heartbeat = now
        until_heartbeat = HEARTBEAT_INTERVAL - (time.monotonic() - last_heartbeat)
        await wait_any(wakeups, max(0.0, min(FALLBACK_INTERVAL, until_heartbeat)))


@router.get("")
async def get_notifications(
    db: DB, session: RegionalSession, request: Request
) -> StreamingResponse:
    connected_at: datetime = await fetch_value(db, "SELECT clock_timestamp()")
    return StreamingResponse(
        notification_events(
            request.app.state.pool,
            request.app.state.notification_hub,
            session,
            connected_at,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
