"""画像 BLOB をアプリホストのローカルディスクにキャッシュする。

`image_objects.data` を UPDATE する経路が無いので、id に対するバイト列は不変。
行が削除された場合は、キャッシュを読む前に可視性チェックのクエリが落ちるため、
消えた画像がキャッシュから返ることはない（initialize でのクリアも不要）。
"""

import hashlib
import os
import tempfile
from pathlib import Path

# ディスクを埋めないための上限。超えたら古いものから半分まで消す。
MAX_TOTAL_BYTES = 8 * 1024 * 1024 * 1024
# 整理は全ファイルを調べてイベントループを止めるので、頻度を抑える
PRUNE_EVERY = 20000


class ImageCache:
    def __init__(self, directory: str) -> None:
        self._dir = Path(directory)
        self._writes = 0
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            self._enabled = True
        except OSError:
            # 書けない場所でもアプリは起動させる（キャッシュ無しで動く）
            self._enabled = False

    def _path(self, image_id: str) -> Path:
        # image_id は URL から来るので、そのままファイル名にしない
        return self._dir / f"{hashlib.sha256(image_id.encode()).hexdigest()}.img"

    def get(self, image_id: str) -> bytes | None:
        if not self._enabled:
            return None
        try:
            return self._path(image_id).read_bytes()
        except OSError:
            return None

    def put(self, image_id: str, data: bytes) -> None:
        if not self._enabled:
            return
        tmp: Path | None = None
        try:
            # 複数ワーカーが同じ画像を同時に書いても壊れないように置換で入れる
            with tempfile.NamedTemporaryFile(dir=self._dir, delete=False) as f:
                tmp = Path(f.name)
                f.write(data)
            os.replace(tmp, self._path(image_id))
        except OSError:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
            return
        self._writes += 1
        if self._writes % PRUNE_EVERY == 0:
            self._prune()

    def _prune(self) -> None:
        entries: list[tuple[float, int, Path]] = []
        total = 0
        for path in self._dir.iterdir():
            try:
                stat = path.stat()
            except OSError:
                continue
            entries.append((stat.st_mtime, stat.st_size, path))
            total += stat.st_size
        if total <= MAX_TOTAL_BYTES:
            return
        entries.sort()
        for _, size, path in entries:
            if total <= MAX_TOTAL_BYTES // 2:
                break
            path.unlink(missing_ok=True)
            total -= size
