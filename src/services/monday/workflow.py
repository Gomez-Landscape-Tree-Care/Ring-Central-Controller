"""Route inbound monday.com webhook payloads by trigger type."""

from logger_config import logger

CREATE_UPDATE = "create_update"


def process_monday(payload):
    """Route one monday webhook by the trigger that fired it."""
    event = payload.get("event") or {}
    trigger = event.get("type")
    if trigger == CREATE_UPDATE:
        return process_create_update(event)

    logger.info("Ignoring monday webhook of type %s", trigger)
    return None


def process_create_update(event):
    # TODO: act on the update. Skip anything authored by Jeff Bot
    # (config.JEFF_BOT_USER_ID) or carrying config.SELF_AUTHORED_UPDATE_MARKER
    # first, or the SMS this repo already posts to boards will loop back in.
    logger.info(
        "monday create_update board=%s item=%s update=%s user=%s",
        event.get("boardId"),
        event.get("pulseId") or event.get("itemId"),
        event.get("updateId"),
        event.get("userId"),
    )
    return None
