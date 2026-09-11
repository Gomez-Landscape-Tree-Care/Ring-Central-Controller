from functools import lru_cache

from config import CL_BOARD_ID, TEST_PHONE
from logger_config import logger
from services.monday.column_ids import CLColIds
from services.monday.model import MondayModel
from utils.normalize import normalize_phone


class MondayController:
    """Controller to handle business logic for all Monday operations"""

    def __init__(self, client: MondayModel = MondayModel()):
        self.client = client

    def create_update_to_cl(self, phone: str, body: str) -> None:
        """Post `body` on the Clients & Leads item for `phone`, if that person has one.

        Everything is swallowed, unlike the Slash writes that run before it.
        Those append rather than upsert, so letting a Monday failure raise would
        DLQ the notification and each of the three redeliveries would leave
        another copy of the same text on the person's Slash record. A missed
        update is a line in CloudWatch; a raise is duplicated data.

        An inbound SMS from a number nobody has ever put on the board is
        ordinary, not an error - there is simply no Updates section to write to.

        The gate compares the `+1XXXXXXXXXX` RingCentral hands over, as
        SlashController's does, so both services go live for the same number at
        the same moment. normalize_phone runs only at the query boundary, where
        Monday's bare `1XXXXXXXXXX` match key is needed.
        """
        if phone != TEST_PHONE:
            return None

        match_key = normalize_phone(phone)
        if not match_key:
            logger.info("Unusable phone %s, skipping Clients & Leads update", phone)
            return None

        try:
            item_id = self.client.find_item_id_by_phone(
                board_id=CL_BOARD_ID, column_id=CLColIds.phone, phone=match_key,
                op=f"Find Clients & Leads item for {phone}")
        except Exception:
            logger.exception("Clients & Leads lookup failed for %s", phone)
            return None

        if not item_id:
            logger.info("No Clients & Leads item for %s", phone)
            return None

        try:
            self.client.create_update(
                item_id=item_id, body=body,
                op=f"Update Clients & Leads item {item_id}")
        except Exception:
            logger.exception("Clients & Leads update failed for %s", phone)
        return None


@lru_cache(maxsize=1)
def get_controller() -> MondayController:
    """The one controller a warm container uses, as slash.controller.get_controller is.

    The requests.Session and its Authorization header are cached on the model, so
    building a new controller per message throws the connection pool away.
    """
    return MondayController()
