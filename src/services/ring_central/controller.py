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

    def download_attachments(self, attachments: list[dict], op: str = "") -> list:
        """Fetch the bytes for every attachment we can, skipping the ones we cannot.

        Best effort per attachment, for get_messages' reason and one more: by the
        time this runs the text half of the message is already in Slash, so a
        media uri that has expired must not cost the message it belongs to.

        The declared size is checked before the request rather than after the
        download, so a record claiming something implausible never occupies the
        bytes. The model checks the real length too - this one only catches the
        records that are honest about being too big.
        """
        downloads = []
        for attachment in attachments:
            attachment_id = attachment.get('id')
            uri = attachment.get('uri')
            size = attachment.get('size') or 0
            if not uri:
                logger.warning(f"Attachment {attachment_id} has no uri")
                continue
            if size > RingCentralModel.MAX_ATTACHMENT_BYTES:
                logger.warning(
                    f"Attachment {attachment_id} declares {size} bytes, over the cap")
                continue
            try:
                downloads.append(self.client.download(
                    uri,
                    filename=attachment.get('filename') or '',
                    op=op or f"Download attachment {attachment_id}"))
            except Exception as e:
                logger.warning(f"Skipping attachment {attachment_id}: {e}")

        logger.info(f"Downloaded {len(downloads)} of {len(attachments)} attachment(s)")
        return downloads
