from logger_config import logger
from services.ring_central import model as RingCentralModel

class RingCentralController:
    def __init__(self, language: str | None = 'Spanish', client = RingCentralModel.get_model()):
        self.language: str | None = language
        self.client = client

    def get_messages(self, extension_id, message_ids: list, op: str = "") -> list:
        """Bulk-fetch message-store records for the given ids, swallowing failures.

        A webhook handler that lets this raise risks RingCentral retrying (or
        dead-lettering) the whole notification over a fetch failure; an empty
        list here just means nothing gets processed this round.
        """
        try:
            return self.client.get_messages(extension_id, message_ids, op=op)
        except Exception as e:
            logger.exception(f"RingCentralController.get_messages failed: {e}")
            return []