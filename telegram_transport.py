"""Small compatibility boundary around Telethon's download iterator."""
import asyncio
from contextlib import asynccontextmanager

from telethon.client.downloads import _CdnRedirect


class ResumeRestartRequired(Exception):
    """The normal Telethon downloader must negotiate this transfer."""


@asynccontextmanager
async def download_stream(client, message, **kwargs):
    stream = client.iter_download(message, **kwargs)
    try:
        yield stream
    except _CdnRedirect:
        # Do not expose redirect objects: they contain tokens and encryption keys.
        raise ResumeRestartRequired("Telegram redirected the file to a CDN") from None
    finally:
        close = getattr(stream, "close", None) or getattr(stream, "aclose", None)
        if close is not None:
            task = asyncio.ensure_future(close())
            cancelled = False
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    cancelled = True
            task.result()
            if cancelled:
                raise asyncio.CancelledError()
