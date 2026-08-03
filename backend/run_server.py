"""Windows-safe AlphaDesk server launcher.

Uvicorn creates its event loop before importing an ASGI application.  Setting
the Windows selector-loop policy inside ``app.main`` is therefore too late
when launching with ``python -m uvicorn``.  This module applies the policy
before Uvicorn starts, preventing Proactor websocket disconnect crashes.
"""
from __future__ import annotations

import asyncio
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        from asyncio.proactor_events import _ProactorBasePipeTransport
        orig_call = _ProactorBasePipeTransport._call_connection_lost
        def patched_call(self, exc):
            try:
                orig_call(self, exc)
            except ConnectionResetError:
                pass
            except OSError as e:
                # 10054 = WSAECONNRESET, 64 = ERROR_NETNAME_DELETED
                # Both are harmless client-disconnect races on Windows.
                if getattr(e, "winerror", None) in (64, 10054):
                    pass
                else:
                    raise
        _ProactorBasePipeTransport._call_connection_lost = patched_call
    except ImportError:
        pass

    # Suppress WinError 64 / ERROR_NETNAME_DELETED that the IOCP proactor
    # raises from its accept() coroutine when a client drops the connection
    # during the TLS accept phase. This is a benign Windows-specific race.
    try:
        import asyncio.windows_events as _wev
        _orig_loop = _wev.IocpProactor.loop
        async def _patched_loop(self, future=None):
            try:
                await _orig_loop(self, future)
            except OSError as e:
                if getattr(e, "winerror", None) == 64:
                    pass
                else:
                    raise
        _wev.IocpProactor.loop = _patched_loop
    except (ImportError, AttributeError):
        pass


import uvicorn

from app.config import REPO_ROOT, get_settings


def main() -> None:
    settings = get_settings()

    keyfile = REPO_ROOT / "key.pem"
    certfile = REPO_ROOT / "cert.pem"
    ssl_options: dict[str, str] = {}
    if keyfile.is_file() and certfile.is_file():
        ssl_options = {"ssl_keyfile": str(keyfile), "ssl_certfile": str(certfile)}

    uvicorn.run("app.main:app", host=settings.host, port=settings.port, **ssl_options)


if __name__ == "__main__":
    main()
