import geckomsg
import wptfyimsg
from .. import bug


def message_for_sync(sync):
    wptfyi_parts = wptfyimsg.for_sync(sync)
    gecko_parts = geckomsg.for_sync(sync)

    msg_parts = []
    if wptfyi_parts is not None:
        msg_parts.extend(wptfyi_parts)
    if gecko_parts is not None:
        msg_parts.extend(gecko_parts)

    if not msg_parts:
        return

    truncated, truncated_message = truncate_message(msg_parts)
    if truncated:
        message = "\n".join(msg_parts)
    else:
        message = truncated_message
        truncated_message = None

    return message, truncated_message


def truncate_message(parts):
    # This is currently the number of codepoints, with the proviso that
    # only BMP characters are supported

    suffix = "(See attachment for full changes)"
    message = ""
    truncated = False

    padding = len(suffix) + 1
    for part in parts:
        if len(message) + len(part) + 1 > bug.max_comment_length - padding:
            truncated = True
            part = suffix
        if message:
            message += "\n"
        message += part
        if truncated:
            break

    return truncated, message
