"""Route inbound RingCentral webhook notifications by event type."""

from logger_config import logger
from services.ring_central.controller import RingCentralController


def process_ring_central(event):
    """Route one RingCentral notification by the event filter it arrived on."""
    # The "event" string is the subscription's own event filter echoed back.
    # The plain (non-instant) message-store filter has no further suffix, which
    # is what tells it apart from a per-extension instant SMS filter.
    filter_path = event.get('event') or ''
    if filter_path.rstrip('/').endswith('/message-store'):
        return process_message_store(event)

    logger.info("Ignoring RingCentral notification on %s", filter_path)
    return None


def process_message_store(event):
    """A batch of new/updated messages: bulk-fetch the new ones, if any."""
    body = event.get('body') or {}
    extension_id = body.get('extensionId')
    message_ids = extract_new_message_ids(body)
    if not message_ids:
        return None

    controller = RingCentralController()
    messages = controller.get_messages(extension_id, message_ids)

    logger.info("Fetched %d new message(s) for extension %s",
                len(messages), extension_id)
    return messages


def extract_new_message_ids(body):
    """Flatten newMessageIds across every change in one message-store notification."""
    ids = []
    for change in body.get('changes') or []:
        ids.extend(change.get('newMessageIds') or [])
    return ids
