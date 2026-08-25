#!/usr/bin/env python3
"""Patch ETM 2.3.1 to allow linked-group members and preserve senders."""

from pathlib import Path

ROOT = Path("/usr/local/lib/python3.11/site-packages/efb_telegram_master")


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise SystemExit(f"unexpected patch anchor count in {path}: {text.count(old)}")
    path.write_text(text.replace(old, new), encoding="utf-8")


replace_once(
    ROOT / "bot_manager.py",
    """        whitelist_filter = ~Filters.user(user_id=self.admins)
        self.dispatcher.add_handler(
            MessageHandler(whitelist_filter, lambda update, context: ...))
""",
    """        # Keep private chats and commands admin-only, but allow ordinary
        # messages from members of linked Telegram groups to reach the bridge.
        whitelist_filter = (
            ~Filters.user(user_id=self.admins) & (Filters.private | Filters.command)
        )
        self.dispatcher.add_handler(
            MessageHandler(whitelist_filter, lambda update, context: ...))
""",
)

replace_once(
    ROOT / "master_message.py",
    """    def process_telegram_message(self, update: Update, context: CallbackContext,
                                 destination: EFBChannelChatIDStr, quote: bool = False,
                                 edited: Optional[\"MsgLog\"] = None):
""",
    """    @staticmethod
    def telegram_sender_author(message: Message, chat):
        if message.chat.type not in (Chat.GROUP, Chat.SUPERGROUP) or message.from_user is None:
            return chat.self or chat.add_self()
        user = message.from_user
        sender_uid = ChatID(f\"telegram_{user.id}\")
        try:
            return chat.get_member(sender_uid)
        except (EFBChatNotFound, KeyError):
            name = user.full_name or user.username or str(user.id)
            alias = f\"@{user.username}\" if user.username else None
            return chat.add_member(name=name, alias=alias, uid=sender_uid)

    def process_telegram_message(self, update: Update, context: CallbackContext,
                                 destination: EFBChannelChatIDStr, quote: bool = False,
                                 edited: Optional[\"MsgLog\"] = None):
""",
)

replace_once(
    ROOT / "master_message.py",
    """            m.chat = self.chat_manager.get_chat(channel, uid, build_dummy=True)
            m.author = m.chat.self or m.chat.add_self()
""",
    """            m.chat = self.chat_manager.get_chat(channel, uid, build_dummy=True)
            m.author = self.telegram_sender_author(message, m.chat)
""",
)

print("patched efb-telegram-master 2.3.1 group sender handling")
