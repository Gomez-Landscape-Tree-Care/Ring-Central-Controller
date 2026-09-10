from logger_config import logger
from services.ring_central import model as RingCentralModel

class RingCentralController:
    def __init__(self, language: str | None = 'Spanish', client = RingCentralModel.get_model()):
        self.language: str | None = language
        self.client = client

    def get_messages(self, extension_id, message_ids: list, op: str = "") -> list:
        """Fetch the message-store records for the given ids, one request each.

        A failed id is skipped rather than raising: a webhook handler that lets
        this raise risks RingCentral retrying (or dead-lettering) the whole
        notification over one fetch, and the records that did arrive are still
        worth processing. One purged or inaccessible message therefore does not
        cost us its neighbours.
        """
        records = []
        for message_id in message_ids:
            try:
                records.append(
                    self.client.get_message(extension_id, message_id, op=op))
            except Exception as e:
                logger.warning(f"Skipping message {message_id}: {e}")

        logger.info(f"Records: {records}")
        return records