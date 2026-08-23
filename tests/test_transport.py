import asyncio
import json

import pytest
import websockets

from efb_qq_napcat.transport import OneBotError, OneBotWebSocket


@pytest.mark.asyncio
async def test_websocket_calls_and_events() -> None:
    received_events: list[dict] = []

    async def server_handler(websocket):
        await websocket.send(json.dumps({"post_type": "meta_event", "meta_event_type": "lifecycle"}))
        async for raw in websocket:
            request = json.loads(raw)
            if request["action"] == "fail":
                response = {"status": "failed", "retcode": 1404, "message": "unsupported", "echo": request["echo"]}
            else:
                response = {"status": "ok", "retcode": 0, "data": {"action": request["action"]}, "echo": request["echo"]}
            await websocket.send(json.dumps(response))

    async with websockets.serve(server_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        transport = OneBotWebSocket(
            f"ws://127.0.0.1:{port}", api_timeout=2, connect_timeout=2, reconnect_delay=0.05
        )

        assert (await transport.call_once("get_version_info"))["action"] == "get_version_info"

        async def on_event(event: dict) -> None:
            received_events.append(event)

        run_task = asyncio.create_task(transport.run(on_event))
        try:
            for _ in range(100):
                if transport.connected and received_events:
                    break
                await asyncio.sleep(0.01)
            assert (await transport.call("get_status"))["action"] == "get_status"
            with pytest.raises(OneBotError) as error:
                await transport.call("fail")
            assert error.value.retcode == 1404
            assert received_events[0]["post_type"] == "meta_event"
        finally:
            await transport.close()
            await asyncio.wait_for(run_task, 2)
