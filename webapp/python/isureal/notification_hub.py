"""通知の SSE を、DB の LISTEN/NOTIFY で起こす。

SSE の接続ごとに DB をポーリングすると、接続数 × 回数だけ問い合わせが走り、
asia1 の CPU を使う。ワーカーごとに 1 本だけ LISTEN し、通知・退会・リージョン変更が
あったときだけ、該当する接続を起こす。

NOTIFY のキー:
  region:<リージョン>  そのリージョンの投稿期間が開き、通知が作られた
  user:<ユーザー ID>   退会やリージョン変更で、届け先が変わった
"""

import asyncio
import contextlib
import logging

from psycopg import AsyncConnection
from psycopg import Error as PsycopgError

CHANNEL = "isureal_events"
RECONNECT_DELAY = 1.0

logger = logging.getLogger(__name__)


def region_key(region: str) -> str:
    return f"region:{region}"


def user_key(user_id: str) -> str:
    return f"user:{user_id}"


NOTIFY_QUERY = f"SELECT pg_notify('{CHANNEL}', %(key)s)"


class NotificationHub:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._events: dict[str, asyncio.Event] = {}
        self._task: asyncio.Task[None] | None = None

    def event(self, key: str) -> asyncio.Event:
        """key に NOTIFY が来たら set される Event。DB を見る前に受け取っておくこと"""
        event = self._events.get(key)
        if event is None:
            event = self._events[key] = asyncio.Event()
        return event

    def _wake(self, key: str) -> None:
        event = self._events.pop(key, None)
        if event is not None:
            event.set()

    def _wake_all(self) -> None:
        events, self._events = self._events, {}
        for event in events.values():
            event.set()

    async def _listen(self) -> None:
        while True:
            try:
                conn = await AsyncConnection.connect(self._dsn, autocommit=True)
                async with conn:
                    await conn.execute(f"LISTEN {CHANNEL}")
                    # つなぎ直すまでの NOTIFY を取りこぼしたかもしれないので、全員に DB を確認させる
                    self._wake_all()
                    async for notify in conn.notifies():
                        self._wake(notify.payload)
            except (PsycopgError, OSError) as e:
                # DB の再起動（デプロイのたび）でも切れるので、トレースバックは出さない。
                # deploy.yml はアプリのログに Traceback があると失敗にする
                logger.warning("notification listener disconnected: %s", e)
            await asyncio.sleep(RECONNECT_DELAY)

    def start(self) -> None:
        self._task = asyncio.create_task(self._listen())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
