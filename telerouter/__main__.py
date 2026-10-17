"""Entry point: python -m telerouter"""

from __future__ import annotations

import asyncio
import logging

import uvicorn

from . import tls
from .logbuffer import LogBuffer
from .settings import Settings
from .web.app import create_app, setup_logging


async def serve() -> None:
    settings = Settings()
    logbuf = LogBuffer()
    setup_logging(settings, logbuf)
    files = tls.ensure(settings.data_dir, settings.tls_names, settings.tls_cert, settings.tls_key)
    logging.getLogger("telerouter").info("TLS CA sha256 %s", files.ca_sha256)
    app, internal = create_app(settings, files, logbuf)
    public = uvicorn.Server(uvicorn.Config(
        app, host=settings.bind, port=settings.https_port, ssl_certfile=files.cert, ssl_keyfile=files.key,
        log_config=None, proxy_headers=False, access_log=False, ws_max_size=128 * 1024,
        ws_ping_interval=None, server_header=False, timeout_keep_alive=30, limit_concurrency=200))
    # loopback only: the agents' SSH reverse tunnels end here (MCP, bearer token required)
    loop_srv = uvicorn.Server(uvicorn.Config(
        internal, host="127.0.0.1", port=settings.internal_port, log_config=None, proxy_headers=False,
        access_log=False, lifespan="off", server_header=False))
    loop_srv.install_signal_handlers = lambda: None   # type: ignore[method-assign]

    async def run_public() -> None:
        try:
            await public.serve()
        finally:
            loop_srv.should_exit = True

    await asyncio.gather(run_public(), loop_srv.serve())


def main() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    main()
