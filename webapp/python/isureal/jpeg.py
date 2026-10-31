import io

from PIL import Image
from starlette.concurrency import run_in_threadpool


def _jpeg_failure(data: bytes, max_side: int) -> str | None:
    try:
        with Image.open(io.BytesIO(data), formats=["JPEG"]) as image:
            if image.width > max_side or image.height > max_side:
                return "too_large"
            # 全データの解読は必要だが、縮小デコードなら出力を作る分のCPUが減る
            image.draft(None, (1, 1))
            image.load()
            return None
    except OSError, SyntaxError, ValueError, Image.DecompressionBombError:
        return "invalid_image"


async def jpeg_failure(data: bytes, max_side: int) -> str | None:
    return await run_in_threadpool(_jpeg_failure, data, max_side)


async def valid_jpeg(data: bytes, max_side: int) -> bool:
    return await jpeg_failure(data, max_side) is None
