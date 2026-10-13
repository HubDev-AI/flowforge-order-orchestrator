import asyncio
import json

import uvicorn
from orchestrator.app.main import MAX_BODY_BYTES, create_app


async def raw_post(port: int, head: str, body: bytes) -> int:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(head.encode() + b"\r\n" + body)
    await writer.drain()
    status_line = await reader.readline()
    writer.close()
    await writer.wait_closed()
    return int(status_line.split()[1])


def chunked(body: bytes, size: int = 4096) -> bytes:
    pieces = [body[i : i + size] for i in range(0, len(body), size)]
    return b"".join(b"%x\r\n%s\r\n" % (len(piece), piece) for piece in pieces) + b"0\r\n\r\n"


async def test_a_chunked_body_that_understates_its_length_is_still_capped(runtime):
    app = create_app(with_lifespan=False)
    app.state.runtime = runtime
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error"))
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    oversized = json.dumps({"payload": {"blob": "x" * MAX_BODY_BYTES}}).encode()

    try:
        status = await raw_post(
            port,
            "POST /api/orders HTTP/1.1\r\nHost: test\r\nContent-Type: application/json\r\n"
            "Idempotency-Key: k1\r\nTransfer-Encoding: chunked\r\nContent-Length: 1\r\n",
            chunked(oversized),
        )
    finally:
        server.should_exit = True
        await serving

    assert status == 413, status
    assert await runtime.store.get_order_by_key("k1") is None
