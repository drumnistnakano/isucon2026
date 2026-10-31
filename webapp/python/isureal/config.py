import os
from dataclasses import dataclass
from typing import Self
from urllib.parse import quote

REGIONS = frozenset({"america", "europe", "asia"})

# 既定のDB接続プールサイズ（ワーカー1本あたり）。
# ベンチ 26366 では asia1 が 102 req/s・平均0.5秒で、同時に50本ほど必要だったのに
# 4ワーカー × 7 = 28本しか無く、接続待ちでレイテンシが伸びていた。
# 合計が PostgreSQL の max_connections を超えないよう、リージョンごとに配分する。
# asia 4×14=56 / europe 4×6=24 / america 4×6=24 = 104
DEFAULT_DB_POOL_SIZE = {"asia": 14, "europe": 6, "america": 6}


def _env(key: str, default: str) -> str:
    return os.getenv(key) or default


@dataclass(frozen=True, slots=True)
class Config:
    region: str
    db_host: str
    db_port: str
    db_user: str
    db_password: str
    db_name: str
    db_pool_size: int
    log_level: str
    image_cache_dir: str

    @classmethod
    def from_env(cls) -> Self:
        region = _env("ISUREAL_REGION", "asia")
        return cls(
            region=region,
            db_host=_env("ISUREAL_DB_HOST", "127.0.0.1"),
            db_port=_env("ISUREAL_DB_PORT", "5432"),
            db_user=_env("ISUREAL_DB_USER", "isureal"),
            db_password=_env("ISUREAL_DB_PASSWORD", "isureal"),
            db_name=_env("ISUREAL_DB_NAME", "isureal"),
            db_pool_size=int(
                _env(
                    "ISUREAL_DB_POOL_SIZE",
                    str(DEFAULT_DB_POOL_SIZE.get(region, 7)),
                )
            ),
            log_level=_env("ISUREAL_LOG_LEVEL", "debug"),
            image_cache_dir=_env("ISUREAL_IMAGE_CACHE_DIR", "/home/isucon/cache/images"),
        )

    @property
    def database_dsn(self) -> str:
        host = f"[{self.db_host}]" if ":" in self.db_host else self.db_host
        user, password = quote(self.db_user, safe=""), quote(self.db_password, safe="")
        return f"postgresql://{user}:{password}@{host}:{self.db_port}/{quote(self.db_name, safe='')}"
