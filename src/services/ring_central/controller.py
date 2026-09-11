import ledger
from logger_config import logger
from services.ring_central import model as RingCentralModel

class RingCentralController:
    def __init__(self, language: str | None = 'Spanish', client = RingCentralModel.get_model()):
        self.language: str | None = language
        self.client = client

    def send_sms(self, text: str, to_number: str, op: str = "") -> str:
        """Send one SMS and register that we are the ones who sent it.

        The only send entry point automation should use. RingCentral echoes
        every outbound message back on the message-store webhook, where nothing
        tells it apart from one an agent typed in the RingCentral app - the
        ledger claim taken here is the entire difference, so a send that went
        round this wrapper would mirror itself onto both boards a second later.

        The claim is the next statement after the send for a reason: the send
        only beats the webhook back while nothing sits between them, and a Slash
        or monday write in the gap would make it a race.

        A failed claim is logged rather than raised, against the rule the rest of
        this module follows: the text is already on the wire by the time it runs,
        and a caller reading the exception as a failed send would send it twice.
        A message mirrored twice on a board costs less than one delivered twice
        to a person.
        """
        message_id = self.client.send_sms(text=text, to_number=to_number, op=op)
        try:
            ledger.claim(message_id)
        except Exception:
            logger.exception("Could not claim sent message %s", message_id)
        return message_id

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
