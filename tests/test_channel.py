import asyncio
import base64
from io import BytesIO
from pathlib import Path

import pytest
from ehforwarderbot import Message, MsgType
from ehforwarderbot.chat import GroupChat, PrivateChat
from ehforwarderbot.exceptions import EFBChatNotFound

from efb_qq_napcat.channel import NapCatAPIError, QQMessengerChannel
from efb_qq_napcat.config import NapCatConfig
from efb_qq_napcat.emoji import format_qq_face
from efb_qq_napcat.telegram_extra import TelegramExtraPhoto
from efb_qq_napcat.transport import OneBotError


def make_channel() -> QQMessengerChannel:
    channel = object.__new__(QQMessengerChannel)
    channel.config = NapCatConfig(max_media_bytes=1024)
    return channel


def test_chat_and_message_id_compatibility() -> None:
    assert QQMessengerChannel._split_chat_uid("private_123") == ("private", "123")
    assert QQMessengerChannel._split_chat_uid("group_456") == ("group", "456")
    assert QQMessengerChannel._onebot_message_id("456_789_1") == "789"
    with pytest.raises(EFBChatNotFound):
        QQMessengerChannel._split_chat_uid("new-format")


def test_text_reply_uses_native_onebot_segments() -> None:
    channel = make_channel()
    chat = GroupChat(channel=channel, uid="group_456", name="group")
    member = chat.add_member(uid="123", name="Alice")
    target = Message(chat=chat, author=member, uid="456_789", type=MsgType.Text, text="old")
    outbound = Message(chat=chat, author=chat.self, type=MsgType.Text, text="new", target=target)
    assert channel._build_outbound_segments(outbound, "group") == [
        {"type": "reply", "data": {"id": "789"}},
        {"type": "at", "data": {"qq": "123"}},
        {"type": "text", "data": {"text": "new"}},
    ]


def test_media_is_base64_encoded() -> None:
    channel = make_channel()
    chat = PrivateChat(channel=channel, uid="private_123", name="Alice")
    outbound = Message(
        chat=chat,
        author=chat.self,
        type=MsgType.Image,
        text="caption",
        file=BytesIO(b"image"),
        filename="photo.jpg",
    )
    segments = channel._build_outbound_segments(outbound, "private")
    assert segments[0]["type"] == "image"
    assert segments[0]["data"]["file"] == "base64://aW1hZ2U="
    assert segments[1] == {"type": "text", "data": {"text": "caption"}}


def test_startup_api_uses_short_lived_connection() -> None:
    class FakeTransport:
        async def call_once(self, action: str, params: dict | None = None) -> dict:
            return {"action": action, "params": params}

    channel = make_channel()
    channel.transport = FakeTransport()
    channel._loop = None
    assert channel._api("get_friend_list", {"cached": True}) == {
        "action": "get_friend_list",
        "params": {"cached": True},
    }


def test_media_without_url_uses_get_file_base64() -> None:
    class FakeTransport:
        async def call(self, action: str, params: dict | None = None) -> dict:
            assert action == "get_file"
            assert params == {"file": "media-id"}
            return {
                "base64": base64.b64encode(b"png-data").decode("ascii"),
                "file_name": "photo.png",
            }

    channel = make_channel()
    channel.transport = FakeTransport()
    media = asyncio.run(
        channel._download_element(
            {"file": "photo.png", "file_id": "media-id"}, "image"
        )
    )
    assert media is not None
    file_obj, filename, mime = media
    try:
        assert file_obj.read() == b"png-data"
        assert filename == "photo.png"
        assert mime == "image/png"
    finally:
        file_obj.close()


def test_send_listener_timeout_is_ambiguous_delivery() -> None:
    error = NapCatAPIError(
        OneBotError(
            "send_msg",
            1200,
            "Timeout: NodeIKernelMsgService/sendMsg ListenerName:onMsgInfoListUpdate",
        )
    )
    assert QQMessengerChannel._is_ambiguous_send_timeout(error)


def test_downloaded_media_exposes_a_real_path_for_telegram_master() -> None:
    class FakeTransport:
        async def call(self, action: str, params: dict | None = None) -> dict:
            return {
                "base64": base64.b64encode(b"file-data").decode("ascii"),
                "file_name": "clip.mp4",
            }

    channel = make_channel()
    channel.transport = FakeTransport()
    media = asyncio.run(channel._download_element({"file_id": "video-id"}, "video"))
    assert media is not None
    file_obj, _, _ = media
    try:
        assert file_obj.name
        assert file_obj.read() == b"file-data"
    finally:
        file_obj.close()


def test_incoming_media_message_sets_path(monkeypatch) -> None:
    channel = make_channel()
    chat = PrivateChat(channel=channel, uid="private_123", name="Alice")
    downloaded = BytesIO(b"image")
    downloaded.name = "/tmp/incoming.jpg"
    captured: list[Message] = []

    async def fake_chat_from_event(event: dict) -> PrivateChat:
        return chat

    async def fake_reply_target(*args) -> None:
        return None

    async def fake_convert(elements: list[dict]):
        return [(MsgType.Image, "", downloaded, "incoming.jpg", "image/jpeg")]

    channel._chat_from_event = fake_chat_from_event
    channel._reply_target = fake_reply_target
    channel._convert_elements = fake_convert
    monkeypatch.setattr("efb_qq_napcat.channel.coordinator.master", object(), raising=False)
    monkeypatch.setattr("efb_qq_napcat.channel.coordinator.send_message", captured.append)

    asyncio.run(
        channel._handle_message_event(
            {
                "message_type": "private",
                "user_id": 123,
                "message_id": 456,
                "sender": {"nickname": "Alice"},
                "message": [{"type": "image", "data": {}}],
            }
        )
    )
    assert captured[0].path is not None
    assert captured[0].path.name == "incoming.jpg"


def test_qq_face_uses_original_emoji_mapping() -> None:
    assert format_qq_face({"id": "0"}) == "😮"
    assert format_qq_face({"id": 76}) == "👍"
    assert format_qq_face({"id": "66"}) == "❤️"


def test_unmapped_qq_face_prefers_napcat_description() -> None:
    assert format_qq_face({"id": "285", "raw": {"faceText": "/摸鱼"}}) == "[摸鱼]"
    assert format_qq_face({"id": "9999"}) == "[QQ表情:9999]"


def test_qrcode_extra_returns_photo_when_qq_is_logged_out(tmp_path: Path) -> None:
    qrcode = tmp_path / "qrcode.png"
    qrcode.write_bytes(b"png")

    class DisconnectedTransport:
        connected = False

    channel = make_channel()
    channel.config = NapCatConfig(qrcode_path=str(qrcode))
    channel.transport = DisconnectedTransport()
    result = channel.qq_login_qrcode()
    assert isinstance(result, TelegramExtraPhoto)
    assert result.photo.read() == b"png"
    result.photo.close()


def test_qrcode_extra_reports_existing_login() -> None:
    class ConnectedTransport:
        connected = True

    channel = make_channel()
    channel.transport = ConnectedTransport()
    channel._api = lambda action, params=None: (
        {"online": True} if action == "get_status" else {"nickname": "Alice", "user_id": 123}
    )
    assert channel.qq_login_qrcode() == "QQ 当前已登录：Alice（123），无需扫码。"


def test_qrcode_extra_does_not_treat_empty_login_as_logged_in(tmp_path: Path) -> None:
    qrcode = tmp_path / "qrcode.png"
    qrcode.write_bytes(b"png")

    class ConnectedTransport:
        connected = True

    channel = make_channel()
    channel.config = NapCatConfig(qrcode_path=str(qrcode))
    channel.transport = ConnectedTransport()
    channel._api = lambda action, params=None: (
        {"online": True} if action == "get_status" else {"nickname": "", "user_id": ""}
    )
    result = channel.qq_login_qrcode()
    assert isinstance(result, TelegramExtraPhoto)
    assert result.photo.read() == b"png"
    result.photo.close()


def test_qrcode_is_listed_as_an_extra_function() -> None:
    channel = make_channel()
    assert "qq_login_qrcode" in channel.get_extra_functions()
