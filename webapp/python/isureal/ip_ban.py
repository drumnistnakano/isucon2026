import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus

from fastapi.responses import JSONResponse
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from .db import Pool, fetch_all
from .timeutil import now

RELOAD_INTERVAL = 0.2

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class IPBanSnapshot:
    loaded_at: float
    bans: dict[str, datetime]


class IPBanList:
    def __init__(self) -> None:
        self.snapshot: IPBanSnapshot | None = None
        self.reloading = False
        self.reload_task: asyncio.Task[None] | None = None

    async def _reload(self, pool: Pool) -> None:
        self.reloading = True
        try:
            self.snapshot = await load_ip_bans(pool)
        finally:
            self.reloading = False

    async def _reload_in_background(self, pool: Pool) -> None:
        try:
            await self._reload(pool)
        except Exception:
            # 古い一覧のまま続け、次のリクエストで読み直す
            logger.exception("failed to reload ip bans")

    async def banned(self, pool: Pool, ip: str) -> bool:
        snapshot = self.snapshot
        if snapshot is None:
            if not self.reloading:
                await self._reload(pool)
                snapshot = self.snapshot
        elif (
            time.monotonic() - snapshot.loaded_at >= RELOAD_INTERVAL
            and not self.reloading
        ):
            # リージョンをまたぐと1往復が重いので、リクエストを待たせずに読み直す
            self.reloading = True
            self.reload_task = asyncio.create_task(self._reload_in_background(pool))
        if snapshot is None:
            return False
        until = snapshot.bans.get(ip)
        return until is not None and now() < until


async def load_ip_bans(pool: Pool) -> IPBanSnapshot:
    async with pool.connection() as db:
        rows = await fetch_all(
            db,
            "SELECT ip, banned_until FROM ip_bans WHERE banned_until > clock_timestamp()",
        )
    return IPBanSnapshot(
        loaded_at=time.monotonic(),
        bans={row["ip"]: row["banned_until"] for row in rows},
    )


def client_ip(scope: Scope) -> str:
    if ip := Headers(scope=scope).get("x-client-ip"):
        return ip
    client = scope.get("client")
    return client[0] if client else ""


class IPBanMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.ip_bans = IPBanList()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and await self.ip_bans.banned(
            scope["app"].state.pool, client_ip(scope)
        ):
            response = JSONResponse(
                {"error": "ip_banned"}, status_code=HTTPStatus.FORBIDDEN
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
