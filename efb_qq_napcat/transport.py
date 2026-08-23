from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import websockets


class OneBotError(RuntimeError):
    def __init__(self, action: str, retcode: int | None, message: str):
        super().__init__(f"OneBot action {action!r} failed ({retcode}): {message}")
        self.action = action
        self.retcode = retcode
        self.message = message


EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


class OneBotWebSocket:
    """OneBot 11 WebSocket client with request correlation and reconnects."""

    def __init__(
        self,
        endpoint: str,
        access_token: str = "",
        *,
        api_timeout: float = 60,
        connect_timeout: float = 20,
        reconnect_delay: float = 5,
    ) -> None:
        self.endpoint = endpoint
        self.access_token = access_token
        self.api_timeout = api_timeout
        self.connect_timeout = connect_timeout
        self.reconnect_delay = reconnect_delay
        self.logger = logging.getLogger(__name__)
        self._ws: Any = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._connected = asyncio.Event()
        self._stopping = asyncio.Event()
        self._send_lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    async def run(self, event_handler: EventHandler) -> None:
        headers = None
        if self.access_token:
            headers = {"Authorization": f"Bearer {self.access_token}"}

        while not self._stopping.is_set():
            try:
                async with websockets.connect(
                    self.endpoint,
                    extra_headers=headers,
                    open_timeout=self.connect_timeout,
                    close_timeout=5,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=64 * 1024 * 1024,
                ) as ws:
                    self._ws = ws
                    self._connected.set()
                    self.logger.info("Connected to NapCat at %s", self.endpoint)
                    async for raw in ws:
                        await self._dispatch(raw, event_handler)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._stopping.is_set():
                    self.logger.warning("NapCat connection lost: %s", exc)
            finally:
                self._connected.clear()
                self._ws = None
                self._fail_pending(ConnectionError("NapCat WebSocket disconnected"))

            if not self._stopping.is_set():
                try:
                    await asyncio.wait_for(self._stopping.wait(), self.reconnect_delay)
                except asyncio.TimeoutError:
                    pass

    async def _dispatch(self, raw: str | bytes, event_handler: EventHandler) -> None:
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            self.logger.warning("Ignoring invalid JSON received from NapCat")
            return
        if not isinstance(payload, dict):
            return

        echo = payload.get("echo")
        if echo is not None:
            future = self._pending.pop(str(echo), None)
            if future is not None and not future.done():
                future.set_result(payload)
            return

        try:
            await event_handler(payload)
        except Exception:
            self.logger.exception("Failed to process a NapCat event")

    async def call(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        try:
            await asyncio.wait_for(self._connected.wait(), self.connect_timeout)
        except asyncio.TimeoutError as exc:
            raise ConnectionError("NapCat is not connected") from exc

        echo = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[echo] = future
        request = json.dumps(
            {"action": action, "params": params or {}, "echo": echo},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        try:
            async with self._send_lock:
                if self._ws is None:
                    raise ConnectionError("NapCat is not connected")
                await self._ws.send(request)
            response = await asyncio.wait_for(future, timeout or self.api_timeout)
        except Exception:
            self._pending.pop(echo, None)
            raise

        return self._response_data(action, response)

    async def call_once(self, action: str, params: dict[str, Any] | None = None) -> Any:
        """Make an API call before the long-running event loop is started.

        EFB master channels may request the slave chat list while channels are
        still being initialized, before :meth:`run` is launched by the
        coordinator.  A short-lived connection keeps those startup calls from
        racing the polling thread.
        """
        headers = None
        if self.access_token:
            headers = {"Authorization": f"Bearer {self.access_token}"}

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.connect_timeout
        last_error: Exception | None = None
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise ConnectionError("NapCat is not available during startup") from last_error
            try:
                async with websockets.connect(
                    self.endpoint,
                    extra_headers=headers,
                    open_timeout=remaining,
                    close_timeout=5,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=64 * 1024 * 1024,
                ) as ws:
                    echo = uuid.uuid4().hex
                    await ws.send(
                        json.dumps(
                            {"action": action, "params": params or {}, "echo": echo},
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                    )
                    response_deadline = loop.time() + self.api_timeout
                    while True:
                        response_remaining = response_deadline - loop.time()
                        if response_remaining <= 0:
                            raise TimeoutError(f"NapCat API timed out: {action}")
                        raw = await asyncio.wait_for(ws.recv(), response_remaining)
                        try:
                            response = json.loads(raw)
                        except (TypeError, ValueError):
                            continue
                        if isinstance(response, dict) and str(response.get("echo")) == echo:
                            return self._response_data(action, response)
            except OneBotError:
                raise
            except (asyncio.TimeoutError, OSError, websockets.WebSocketException) as exc:
                last_error = exc
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise ConnectionError("NapCat is not available during startup") from exc
                await asyncio.sleep(min(self.reconnect_delay, remaining))

    @staticmethod
    def _response_data(action: str, response: dict[str, Any]) -> Any:
        retcode = response.get("retcode")
        if response.get("status") not in (None, "ok") or retcode not in (None, 0):
            message = str(response.get("wording") or response.get("message") or "unknown error")
            raise OneBotError(action, retcode, message)
        return response.get("data")

    async def close(self) -> None:
        self._stopping.set()
        self._connected.clear()
        if self._ws is not None:
            await self._ws.close()
        self._fail_pending(ConnectionError("NapCat transport stopped"))

    def _fail_pending(self, exc: Exception) -> None:
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(exc)
