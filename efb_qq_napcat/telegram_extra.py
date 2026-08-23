from __future__ import annotations

from dataclasses import dataclass
from typing import BinaryIO


@dataclass
class TelegramExtraPhoto:
    """Photo result understood by the Telegram Master's patched /extra handler."""

    photo: BinaryIO
    caption: str


def install_telegram_extra_photo_support() -> None:
    """Teach EFB Telegram Master 2.3.x to accept photo results from /extra.

    EFB's standard extra-function protocol only supports strings.  The slave
    channel is initialized before the master, so replacing this class method
    here happens before Telegram registers its command handler.
    """
    try:
        from ehforwarderbot import coordinator
        from ehforwarderbot.types import ExtraCommandName
        from efb_telegram_master.commands import CommandsManager
    except ImportError:
        return

    if getattr(CommandsManager.extra_call, "q2tg_photo_result", False):
        return

    def extra_call(self, update, context):
        assert context.match
        assert update.message

        groupdict = context.match.groupdict()
        module_index = int(groupdict["id"])
        if module_index >= len(coordinator.slaves):
            return self.bot.reply_error(update, self._("Invalid module ID. (XC01)"))

        slaves = coordinator.slaves
        channel = slaves[sorted(slaves)[module_index]]
        functions = channel.get_extra_functions()
        command_name = ExtraCommandName(groupdict["command"])
        if command_name not in functions:
            return self.bot.reply_error(update, self._("Command not found in selected module. (XC02)"))

        header = "{} {}: {}\n-------\n".format(
            channel.channel_emoji,
            channel.channel_name,
            functions[command_name].name,
        )
        waiting = self.bot.send_message(
            update.message.chat.id,
            prefix=header,
            text=self._("Please wait..."),
        )

        text = update.message.text or ""
        result = functions[command_name](" ".join(text.split(" ", 1)[1:]))
        if not isinstance(result, TelegramExtraPhoto):
            return self.bot.edit_message_text(
                prefix=header,
                text=str(result or ""),
                chat_id=update.message.chat.id,
                message_id=waiting.message_id,
            )

        try:
            result.photo.seek(0)
            sent = self.bot.send_photo(
                update.message.chat.id,
                result.photo,
                caption=header + result.caption,
            )
            self.bot.delete_message(update.message.chat.id, waiting.message_id)
            return sent
        finally:
            result.photo.close()

    extra_call.q2tg_photo_result = True
    CommandsManager.extra_call = extra_call
