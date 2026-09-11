from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import mimetypes
import tempfile
import uuid
from collections.abc import Collection
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import Any, BinaryIO

import httpx
from ehforwarderbot import Chat, Message, MsgType, Status, coordinator, utils as efb_utils
from ehforwarderbot.channel import SlaveChannel
from ehforwarderbot.chat import ChatMember, GroupChat, PrivateChat, SelfChatMember
from ehforwarderbot.exceptions import (
    EFBChatNotFound,
    EFBMessageError,
    EFBOperationNotSupported,
)
from ehforwarderbot.status import ChatUpdates, MessageRemoval
from ehforwarderbot.types import ChatID, InstanceID, MessageID

from .config import NapCatConfig, load_config
from .emoji import format_qq_face
from .telegram_extra import TelegramExtraPhoto, install_telegram_extra_photo_support
from .transport import OneBotError, OneBotWebSocket


class NapCatAPIError(EFBMessageError):
    def __init__(self, error: OneBotError):
        super().__init__(str(error))
        self.action = error.action
        self.retcode = error.retcode
        self.detail = error.message


class QQMessengerChannel(SlaveChannel):
    """EFB slave channel backed by NapCat's OneBot 11 WebSocket server.

    The historical ``milkice.qq`` channel ID and chat/message ID layout are
    intentionally retained so that efb-telegram-master databases created by
    efb-qq-slave continue to resolve their chat associations.
    """

    channel_name = "QQ (NapCat)"
    channel_emoji = "🐧"
    channel_id = "milkice.qq"
    __version__ = "0.1.0"

    supported_message_types = {
        MsgType.Text,
        MsgType.Link,
        MsgType.Image,
        MsgType.Sticker,
        MsgType.Animation,
        MsgType.Voice,
        MsgType.Video,
        MsgType.File,
    }

    def __init__(self, instance_id: InstanceID = None):
        super().__init__(instance_id)
        install_telegram_extra_photo_support()
        self.logger = logging.getLogger(__name__)
        self.config: NapCatConfig = load_config(efb_utils.get_config_path(self.channel_id))
        self.transport = OneBotWebSocket(
            self.config.endpoint,
            self.config.access_token,
            api_timeout=self.config.api_timeout,
            connect_timeout=self.config.connect_timeout,
            reconnect_delay=self.config.reconnect_delay,
        )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._chats: dict[str, Chat] = {}
        self._stopping = False

    # --- EFB lifecycle -------------------------------------------------

    def poll(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self.transport.run(self._handle_event))
        finally:
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self._loop.close()
            self._loop = None

    def stop_polling(self) -> None:
        self._stopping = True
        if self._loop and self._loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self.transport.close(), self._loop)
            try:
                future.result(timeout=10)
            except Exception:
                self.logger.exception("Failed to stop the NapCat transport cleanly")

    @efb_utils.extra(
        name="QQ 扫码登录",
        desc="获取当前 NapCat QQ 登录二维码。QQ 掉线时使用。\n\n用法：{function_name}",
    )
    def qq_login_qrcode(self, param: str = "") -> str | TelegramExtraPhoto:
        if self.transport.connected:
            try:
                status = self._api("get_status") or {}
                info = self._api("get_login_info") or {}
                nickname = str(info.get("nickname") or "QQ").strip() or "QQ"
                user_id = str(info.get("user_id") or "").strip()
                # The OneBot WebSocket can remain connected after Tencent
                # kicks the QQ session offline. A connected transport alone
                # is not proof that the account is logged in.
                if status.get("online") is True and user_id and user_id != "0":
                    return f"QQ 当前已登录：{nickname}（{user_id}），无需扫码。"
            except EFBMessageError:
                pass

        qrcode_path = Path(self.config.qrcode_path)
        try:
            payload = qrcode_path.read_bytes()
        except FileNotFoundError:
            return "NapCat 尚未生成登录二维码，请稍候几秒后再次执行此命令。"
        except OSError as exc:
            self.logger.warning("Unable to read NapCat QR code: %s", exc)
            return "无法读取 NapCat 登录二维码，请检查服务状态。"
        if not payload:
            return "NapCat 登录二维码文件为空，请稍候几秒后重试。"

        photo = io.BytesIO(payload)
        photo.name = "qq-login-qrcode.png"
        return TelegramExtraPhoto(
            photo=photo,
            caption="请使用手机 QQ 扫描二维码并在手机端确认。二维码过期后可再次执行此命令刷新。",
        )

    def _api(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        if self._loop is None or not self._loop.is_running():
            try:
                return asyncio.run(self.transport.call_once(action, params))
            except OneBotError as exc:
                raise NapCatAPIError(exc) from exc
            except Exception as exc:
                raise EFBMessageError(f"NapCat startup API failed: {action}: {exc}") from exc
        future = asyncio.run_coroutine_threadsafe(
            self.transport.call(action, params, timeout=timeout), self._loop
        )
        try:
            return future.result(
                timeout=(timeout or self.config.api_timeout) + self.config.connect_timeout + 2
            )
        except FutureTimeoutError as exc:
            future.cancel()
            raise EFBMessageError(f"NapCat API timed out: {action}") from exc
        except OneBotError as exc:
            raise NapCatAPIError(exc) from exc
        except Exception as exc:
            raise EFBMessageError(f"NapCat API failed: {action}: {exc}") from exc

    # --- Outbound Telegram -> QQ --------------------------------------

    def send_message(self, msg: Message) -> Message:
        chat_type, target_id = self._split_chat_uid(str(msg.chat.uid))

        if msg.edit and msg.uid:
            try:
                self._api("delete_msg", {"message_id": self._onebot_message_id(str(msg.uid))})
            except EFBMessageError as exc:
                raise EFBOperationNotSupported("QQ message can no longer be edited/recalled") from exc

        segments = self._build_outbound_segments(msg, chat_type)
        params: dict[str, Any] = {
            "message_type": chat_type,
            "message": segments,
            "timeout": int(self.config.send_timeout * 1000),
        }
        params["user_id" if chat_type == "private" else "group_id"] = target_id
        try:
            data = self._api(
                "send_msg", params, timeout=self.config.send_timeout + 10
            ) or {}
            message_id = data.get("message_id")
        except NapCatAPIError as exc:
            if not self._is_ambiguous_send_timeout(exc):
                raise
            message_id = self._recover_sent_message_id(chat_type, target_id)
            if message_id is None:
                message_id = f"unconfirmed-{uuid.uuid4().hex}"
            self.logger.warning(
                "NapCat did not confirm a delivered QQ message; using recovered/fallback ID"
            )
        if message_id is None:
            raise EFBMessageError("NapCat did not return a message_id")
        msg.uid = MessageID(f"{target_id}_{message_id}")
        return msg

    @staticmethod
    def _is_ambiguous_send_timeout(error: NapCatAPIError) -> bool:
        detail = error.detail.lower()
        return (
            error.action == "send_msg"
            and error.retcode == 1200
            and "timeout" in detail
            and "sendmsg" in detail
        )

    def _recover_sent_message_id(self, chat_type: str, target_id: str) -> str | None:
        action = "get_friend_msg_history" if chat_type == "private" else "get_group_msg_history"
        key = "user_id" if chat_type == "private" else "group_id"
        try:
            login = self._api("get_login_info") or {}
            self_id = str(login.get("user_id") or "")
            history = self._api(
                action,
                {key: target_id, "count": 10, "reverse_order": False, "quick_reply": True},
            ) or {}
        except EFBMessageError:
            return None
        messages = history.get("messages") if isinstance(history, dict) else None
        if not isinstance(messages, list):
            return None
        candidates: list[tuple[float, str]] = []
        for item in messages:
            if not isinstance(item, dict):
                continue
            sender = item.get("sender") or {}
            sender_id = str(sender.get("user_id") or item.get("user_id") or "")
            if self_id and sender_id and sender_id != self_id:
                continue
            message_id = item.get("message_id", item.get("id"))
            if message_id is None:
                continue
            timestamp = float(item.get("time") or item.get("message_seq") or 0)
            candidates.append((timestamp, str(message_id)))
        if not candidates:
            return None
        return max(candidates, key=lambda item: item[0])[1]

    def _build_outbound_segments(self, msg: Message, chat_type: str) -> list[dict[str, Any]]:
        segments: list[dict[str, Any]] = []
        if msg.target and msg.target.uid:
            segments.append({"type": "reply", "data": {"id": self._onebot_message_id(str(msg.target.uid))}})
            if chat_type == "group" and msg.target.author and not isinstance(msg.target.author, SelfChatMember):
                segments.append({"type": "at", "data": {"qq": str(msg.target.author.uid)}})

        if msg.type in (MsgType.Text, MsgType.Link):
            segments.append({"type": "text", "data": {"text": msg.text or ""}})
            return segments

        segment_type = {
            MsgType.Image: "image",
            MsgType.Sticker: "image",
            MsgType.Animation: "image",
            MsgType.Voice: "record",
            MsgType.Video: "video",
            MsgType.File: "file",
        }.get(msg.type)
        if segment_type is None:
            raise EFBOperationNotSupported(f"Unsupported Telegram message type: {msg.type}")

        payload = self._read_message_file(msg)
        data: dict[str, Any] = {"file": "base64://" + base64.b64encode(payload).decode("ascii")}
        if msg.filename:
            data["name"] = msg.filename
        segments.append({"type": segment_type, "data": data})
        if msg.text:
            segments.append({"type": "text", "data": {"text": msg.text}})
        return segments

    def _read_message_file(self, msg: Message) -> bytes:
        stream = msg.file
        should_close = False
        if stream is None and msg.path:
            stream = Path(msg.path).open("rb")
            should_close = True
        if stream is None:
            raise EFBMessageError("Media message has no readable file")
        try:
            try:
                stream.seek(0)
            except (AttributeError, OSError):
                pass
            payload = stream.read(self.config.max_media_bytes + 1)
        finally:
            if should_close:
                stream.close()
        if len(payload) > self.config.max_media_bytes:
            raise EFBMessageError(f"Media exceeds the {self.config.max_media_bytes}-byte upload limit")
        return payload

    def send_status(self, status: Status) -> None:
        if isinstance(status, MessageRemoval):
            if not status.message.uid:
                raise EFBOperationNotSupported("Message has no QQ message ID")
            self._api("delete_msg", {"message_id": self._onebot_message_id(str(status.message.uid))})
            return
        raise EFBOperationNotSupported(f"Unsupported status: {type(status).__name__}")

    # --- Incoming QQ -> Telegram --------------------------------------

    async def _handle_event(self, event: dict[str, Any]) -> None:
        post_type = event.get("post_type")
        if post_type in ("message", "message_sent"):
            await self._handle_message_event(event)
        elif post_type == "notice":
            await self._handle_notice_event(event)
        elif post_type == "request":
            await self._handle_request_event(event)

    async def _handle_message_event(self, event: dict[str, Any]) -> None:
        chat = await self._chat_from_event(event)
        author = self._author_from_event(chat, event)
        elements = event.get("message")
        if isinstance(elements, str):
            elements = [{"type": "text", "data": {"text": elements}}]
        if not isinstance(elements, list):
            elements = [{"type": "text", "data": {"text": str(event.get("raw_message", ""))}}]

        target = await self._reply_target(chat, author, elements)
        converted = await self._convert_elements(elements)
        if not converted:
            converted = [(MsgType.Unsupported, "[无法解析的 QQ 消息]", None, None, None)]

        raw_id = str(event.get("message_id", event.get("real_id", "unknown")))
        chat_numeric_id = str(chat.uid).split("_", 1)[-1]
        for index, (msg_type, text, file_obj, filename, mime) in enumerate(converted):
            uid = f"{chat_numeric_id}_{raw_id}" + (f"_{index}" if index else "")
            message = Message(
                chat=chat,
                author=author,
                deliver_to=coordinator.master,
                type=msg_type,
                text=text,
                file=file_obj,
                filename=filename,
                mime=mime,
                path=file_obj.name if file_obj is not None else None,
                uid=MessageID(uid),
                target=target,
                vendor_specific={"onebot": event},
            )
            try:
                coordinator.send_message(message)
            finally:
                if file_obj is not None:
                    file_obj.close()

    async def _convert_elements(
        self, elements: list[dict[str, Any]]
    ) -> list[tuple[MsgType, str, BinaryIO | None, str | None, str | None]]:
        result: list[tuple[MsgType, str, BinaryIO | None, str | None, str | None]] = []
        text_parts: list[str] = []

        for element in elements:
            if not isinstance(element, dict):
                text_parts.append(str(element))
                continue
            kind = str(element.get("type", "unknown"))
            data = element.get("data") or {}
            if kind == "reply":
                continue
            if kind == "text":
                text_parts.append(str(data.get("text", "")))
            elif kind == "at":
                qq = str(data.get("qq", ""))
                text_parts.append("@" + str(data.get("name") or ("全体成员" if qq == "all" else qq)))
            elif kind == "face":
                text_parts.append(format_qq_face(data))
            elif kind == "mface":
                text_parts.append(str(data.get("summary") or "[商城表情]"))
            elif kind in ("image", "record", "video", "file", "onlinefile"):
                media_kind = "file" if kind == "onlinefile" else kind
                if kind == "onlinefile":
                    data = {
                        **data,
                        "file": data.get("fileName") or data.get("file"),
                        "name": data.get("fileName") or data.get("name"),
                    }
                media = await self._download_element(data, media_kind)
                if media is None:
                    text_parts.append(f"[{media_kind} 无法下载]")
                    continue
                file_obj, filename, mime = media
                msg_type = {
                    "image": MsgType.Image,
                    "record": MsgType.Voice,
                    "video": MsgType.Video,
                    "file": MsgType.File,
                }[media_kind]
                result.append((msg_type, "".join(text_parts), file_obj, filename, mime))
                text_parts = []
            elif kind == "forward":
                text_parts.append(await self._forward_text(data))
            elif kind == "json":
                text_parts.append(self._json_summary(data.get("data")))
            elif kind == "markdown":
                text_parts.append(str(data.get("content", "")))
            elif kind == "location":
                text_parts.append(
                    f"[位置] {data.get('title') or ''} {data.get('content') or ''} "
                    f"({data.get('lat')}, {data.get('lon')})"
                )
            elif kind in ("dice", "rps"):
                text_parts.append(f"[{kind}: {data.get('result', '?')}]")
            else:
                text_parts.append(f"[不支持的 QQ 消息段: {kind}]")

        if text_parts or not result:
            result.append((MsgType.Text, "".join(text_parts), None, None, None))
        return result

    async def _download_element(
        self, data: dict[str, Any], kind: str
    ) -> tuple[BinaryIO, str, str] | None:
        url = data.get("url")
        name_source = data.get("name") or data.get("file") or kind
        name = Path(str(name_source)).name or kind
        payload: bytes | None = None

        if isinstance(url, str) and url.startswith(("http://", "https://")):
            try:
                async with httpx.AsyncClient(
                    follow_redirects=True, timeout=self.config.download_timeout
                ) as client:
                    response = await client.get(url)
                    response.raise_for_status()
                    payload = response.content
            except Exception as exc:
                self.logger.warning("Failed to download QQ %s from its reported URL: %s", kind, exc)

        if payload is None:
            locator = data.get("file_id") or data.get("file") or data.get("path")
            if locator:
                try:
                    file_info = await self.transport.call("get_file", {"file": str(locator)}) or {}
                    encoded = file_info.get("base64") if isinstance(file_info, dict) else None
                    fallback_url = file_info.get("url") if isinstance(file_info, dict) else None
                    if isinstance(encoded, str) and encoded:
                        payload = base64.b64decode(encoded)
                    elif isinstance(fallback_url, str) and fallback_url.startswith(("http://", "https://")):
                        async with httpx.AsyncClient(
                            follow_redirects=True, timeout=self.config.download_timeout
                        ) as client:
                            response = await client.get(fallback_url)
                            response.raise_for_status()
                            payload = response.content
                    fallback_name = file_info.get("file_name") if isinstance(file_info, dict) else None
                    if fallback_name:
                        name = Path(str(fallback_name)).name or name
                except Exception as exc:
                    self.logger.warning("Failed to retrieve QQ %s through get_file: %s", kind, exc)

        if payload is None:
            return None
        suffix = Path(name).suffix
        if not suffix:
            suffix = {"image": ".jpg", "record": ".silk", "video": ".mp4", "file": ".bin"}[kind]
            name += suffix
        if len(payload) > self.config.max_media_bytes:
            self.logger.warning("Dropping oversized QQ media (%d bytes)", len(payload))
            return None
        file_obj = tempfile.NamedTemporaryFile(suffix=suffix)
        file_obj.write(payload)
        file_obj.seek(0)
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        return file_obj, name, mime

    async def _forward_text(self, data: dict[str, Any]) -> str:
        content = data.get("content")
        if content is None and data.get("id"):
            try:
                response = await self.transport.call("get_forward_msg", {"message_id": data["id"]})
                content = (response or {}).get("messages") or (response or {}).get("message")
            except Exception as exc:
                self.logger.warning("Unable to expand forwarded QQ message: %s", exc)
        if not isinstance(content, list):
            return "[合并转发消息]"
        lines = ["[合并转发消息]"]
        for node in content:
            node_data = node.get("data", node) if isinstance(node, dict) else {}
            sender = node_data.get("nickname") or node_data.get("name") or "QQ用户"
            nested = node_data.get("content") or node_data.get("message") or []
            if isinstance(nested, str):
                body = nested
            else:
                parts = []
                for segment in nested if isinstance(nested, list) else []:
                    if segment.get("type") == "text":
                        parts.append(str((segment.get("data") or {}).get("text", "")))
                    else:
                        parts.append(f"[{segment.get('type', '消息')}]")
                body = "".join(parts)
            lines.append(f"{sender}: {body}")
        return "\n".join(lines)

    @staticmethod
    def _json_summary(value: Any) -> str:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                return value
        if isinstance(value, dict):
            meta = value.get("meta") or {}
            detail = next(iter(meta.values()), {}) if isinstance(meta, dict) and meta else {}
            if isinstance(detail, dict):
                title = detail.get("title") or detail.get("desc")
                url = detail.get("jumpUrl") or detail.get("qqdocurl")
                if title or url:
                    return "\n".join(str(i) for i in (title, url) if i)
        return "[JSON 卡片]"

    async def _reply_target(
        self, chat: Chat, fallback_author: ChatMember, elements: list[dict[str, Any]]
    ) -> Message | None:
        reply = next((e for e in elements if isinstance(e, dict) and e.get("type") == "reply"), None)
        if reply is None:
            return None
        reply_id = str((reply.get("data") or {}).get("id") or "")
        if not reply_id:
            return None
        author = fallback_author
        text = "[回复的 QQ 消息]"
        try:
            data = await self.transport.call("get_msg", {"message_id": reply_id}) or {}
            sender = data.get("sender") or {}
            if isinstance(chat, GroupChat) and sender.get("user_id") is not None:
                author = self._group_member(chat, sender)
            elif isinstance(chat, PrivateChat):
                author = chat.other
            text = str(data.get("raw_message") or text)
        except Exception:
            pass
        chat_numeric_id = str(chat.uid).split("_", 1)[-1]
        return Message(
            chat=chat,
            author=author,
            type=MsgType.Text,
            text=text,
            uid=MessageID(f"{chat_numeric_id}_{reply_id}"),
        )

    async def _handle_notice_event(self, event: dict[str, Any]) -> None:
        notice = event.get("notice_type")
        if notice in ("group_recall", "friend_recall"):
            chat = await self._chat_from_event(event)
            raw_id = str(event.get("message_id"))
            chat_numeric_id = str(chat.uid).split("_", 1)[-1]
            message = Message(chat=chat, uid=MessageID(f"{chat_numeric_id}_{raw_id}"))
            coordinator.send_status(MessageRemoval(self, coordinator.master, message))
        elif notice == "friend_add":
            uid = ChatID(f"private_{event.get('user_id')}")
            coordinator.send_status(ChatUpdates(self, new_chats=(uid,)))
        elif notice in ("group_increase", "group_decrease"):
            uid = ChatID(f"group_{event.get('group_id')}")
            coordinator.send_status(ChatUpdates(self, modified_chats=(uid,)))

    async def _handle_request_event(self, event: dict[str, Any]) -> None:
        request_type = event.get("request_type")
        comment = str(event.get("comment") or "")
        user_id = event.get("user_id")
        group_id = event.get("group_id")
        text = f"[QQ {request_type} 请求] 用户 {user_id}"
        if group_id:
            text += f"，群 {group_id}"
        if comment:
            text += f"：{comment}"
        chat = await self._chat_from_event(event)
        author = self._author_from_event(chat, event)
        message = Message(
            chat=chat,
            author=author,
            deliver_to=coordinator.master,
            type=MsgType.Text,
            text=text,
            uid=MessageID(f"request_{event.get('flag', event.get('time', 'unknown'))}"),
            is_system=True,
        )
        coordinator.send_message(message)

    # --- Chat directory and compatibility -----------------------------

    async def _chat_from_event(self, event: dict[str, Any]) -> Chat:
        if event.get("message_type") == "group" or event.get("group_id") is not None:
            group_id = str(event.get("group_id"))
            uid = f"group_{group_id}"
            cached = self._chats.get(uid)
            if cached is not None:
                return cached
            name = str(event.get("group_name") or group_id)
            if name == group_id:
                try:
                    info = await self.transport.call("get_group_info", {"group_id": group_id, "no_cache": False})
                    name = str((info or {}).get("group_name") or group_id)
                except Exception:
                    pass
            chat = GroupChat(channel=self, uid=ChatID(uid), name=name, vendor_specific={"is_discuss": False})
        else:
            user_id = str(event.get("user_id"))
            uid = f"private_{user_id}"
            cached = self._chats.get(uid)
            if cached is not None:
                return cached
            sender = event.get("sender") or {}
            name = str(sender.get("nickname") or user_id)
            alias = sender.get("remark") or None
            chat = PrivateChat(channel=self, uid=ChatID(uid), name=name, alias=alias)
        self._chats[uid] = chat
        return chat

    def _author_from_event(self, chat: Chat, event: dict[str, Any]) -> ChatMember:
        sender = event.get("sender") or {}
        if isinstance(chat, PrivateChat):
            return chat.other
        return self._group_member(chat, sender | {"user_id": event.get("user_id", sender.get("user_id"))})

    @staticmethod
    def _group_member(chat: GroupChat, sender: dict[str, Any]) -> ChatMember:
        uid = str(sender.get("user_id", "unknown"))
        try:
            return chat.get_member(uid)
        except KeyError:
            card = str(sender.get("card") or "")
            nickname = str(sender.get("nickname") or uid)
            return chat.add_member(name=card or nickname, alias=nickname if card else None, uid=ChatID(uid))

    def get_chats(self) -> Collection[Chat]:
        friends = self._api("get_friend_list") or []
        groups = self._api("get_group_list") or []
        chats: list[Chat] = []
        for item in friends:
            uid = f"private_{item['user_id']}"
            chat = PrivateChat(
                channel=self,
                uid=ChatID(uid),
                name=str(item.get("nickname") or item["user_id"]),
                alias=item.get("remark") or None,
            )
            self._chats[uid] = chat
            chats.append(chat)
        for item in groups:
            uid = f"group_{item['group_id']}"
            chat = GroupChat(
                channel=self,
                uid=ChatID(uid),
                name=str(item.get("group_name") or item["group_id"]),
                vendor_specific={"is_discuss": False},
            )
            self._chats[uid] = chat
            chats.append(chat)
        return chats

    def get_chat(self, chat_uid: ChatID) -> Chat:
        uid = str(chat_uid)
        if uid in self._chats:
            return self._chats[uid]
        chat_type, numeric_id = self._split_chat_uid(uid)
        if chat_type == "group":
            item = self._api("get_group_info", {"group_id": numeric_id, "no_cache": False}) or {}
            if not item:
                raise EFBChatNotFound(uid)
            chat: Chat = GroupChat(
                channel=self,
                uid=chat_uid,
                name=str(item.get("group_name") or numeric_id),
                vendor_specific={"is_discuss": False},
            )
        else:
            item = self._api("get_stranger_info", {"user_id": numeric_id, "no_cache": False}) or {}
            if not item:
                raise EFBChatNotFound(uid)
            chat = PrivateChat(channel=self, uid=chat_uid, name=str(item.get("nickname") or numeric_id))
        self._chats[uid] = chat
        return chat

    def get_chat_picture(self, chat: Chat) -> BinaryIO:
        chat_type, numeric_id = self._split_chat_uid(str(chat.uid))
        if chat_type == "group":
            url = f"https://p.qlogo.cn/gh/{numeric_id}/{numeric_id}/640"
        else:
            url = f"https://q1.qlogo.cn/g?b=qq&nk={numeric_id}&s=640"
        return self._download_avatar(url)

    def get_chat_member_picture(self, chat_member: ChatMember) -> BinaryIO:
        return self._download_avatar(f"https://q1.qlogo.cn/g?b=qq&nk={chat_member.uid}&s=640")

    def _download_avatar(self, url: str) -> BinaryIO:
        try:
            response = httpx.get(url, follow_redirects=True, timeout=self.config.download_timeout)
            response.raise_for_status()
        except Exception as exc:
            raise EFBChatNotFound(f"Unable to download QQ avatar: {exc}") from exc
        file_obj = tempfile.NamedTemporaryFile(suffix=".jpg")
        file_obj.write(response.content)
        file_obj.seek(0)
        return file_obj

    def get_message_by_id(self, chat: Chat, msg_id: MessageID) -> Message | None:
        data = self._api("get_msg", {"message_id": self._onebot_message_id(str(msg_id))}) or {}
        if not data:
            return None
        if isinstance(chat, PrivateChat):
            author = chat.other
        else:
            author = self._group_member(chat, data.get("sender") or {"user_id": data.get("user_id")})
        return Message(
            chat=chat,
            author=author,
            type=MsgType.Text,
            text=str(data.get("raw_message") or ""),
            uid=msg_id,
        )

    @staticmethod
    def _split_chat_uid(uid: str) -> tuple[str, str]:
        prefix, separator, numeric_id = uid.partition("_")
        if not separator or prefix not in ("private", "group") or not numeric_id:
            raise EFBChatNotFound(f"Unsupported QQ chat ID: {uid}")
        return prefix, numeric_id

    @staticmethod
    def _onebot_message_id(uid: str) -> str:
        parts = uid.split("_")
        if len(parts) < 2 or not parts[1]:
            raise EFBOperationNotSupported(f"Invalid QQ message ID: {uid}")
        return parts[1]
