"""Narrow restart relay for the Runtime Collector's shared PID namespace."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import signal


SOCKET = Path("/var/lib/devassistant/collector-control/collector-reload.sock")


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        command = await asyncio.wait_for(reader.readline(), timeout=2)
        process = Path("/proc/1/cmdline").read_bytes().replace(b"\0", b" ")
        if command != b"restart\n" or b"devassistant-otelcol" not in process:
            writer.write(b"rejected\n")
            await writer.drain()
            return
        os.kill(1, signal.SIGTERM)
        writer.write(b"restarting\n")
        await writer.drain()
    except (asyncio.TimeoutError, OSError):
        pass
    finally:
        writer.close()
        await writer.wait_closed()


async def main() -> None:
    SOCKET.parent.mkdir(parents=True, exist_ok=True)
    SOCKET.unlink(missing_ok=True)
    server = await asyncio.start_unix_server(handle, path=str(SOCKET), limit=128)
    os.chmod(SOCKET, 0o660)
    async with server:
        await server.serve_forever()


asyncio.run(main())
