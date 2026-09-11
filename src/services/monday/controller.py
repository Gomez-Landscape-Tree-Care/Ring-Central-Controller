from functools import lru_cache

from config import AB_BOARD_ID, CL_BOARD_ID, TEST_PHONE
from logger_config import logger
from services.monday.column_ids import ABColIds, CLColIds
from services.monday.model import MondayModel
from utils.normalize import normalize_phone


class MondayController:
    """Controller to handle business logic for all Monday operations"""

    def __init__(self, client: MondayModel = MondayModel()):
        self.client = client

    def _create_update_to_board(self, phone: str, body: str, board_id: int,
                                column_id: str, label: str) -> str | None:
        """Updates in Monday represent text messages for this automation.

        Answers the new update's id so a caller can hang a photo off it, and None
        for every way this gives up - outside the rollout, an unusable phone, a
        failed lookup, no item, a failed write. Those are not five outcomes a
        caller can act on differently: None means there is nothing on the board
        to attach to.
        """
        if phone != TEST_PHONE:
            return None

        match_key = normalize_phone(phone)
        if not match_key:
            logger.info("Unusable phone %s, skipping %s update", phone, label)
            return None

        try:
            item_id = self.client.find_item_id_by_phone(
                board_id=board_id, column_id=column_id, phone=match_key,
                op=f"Find {label} item for {phone}")
        except Exception:
            logger.exception("%s lookup failed for %s", label, phone)
            return None

        if not item_id:
            logger.info("No %s item for %s", label, phone)
            return None

        try:
            return self.client.create_update(
                item_id=item_id, body=body, op=f"Update {label} item {item_id}")
        except Exception:
            logger.exception("%s update failed for %s", label, phone)
        return None

    def add_photo_to_update(self, update_id: str | None, filename: str,
                            content: bytes, content_type: str) -> None:
        """Hang one photo off an update already posted, if there is one.

        A falsy update_id is the ordinary case rather than an error: the board
        write answers None for a phone outside the rollout and for a person with
        no item, and both mean there is nothing to attach to.

        Swallowed like the update write above, and for a sharper reason - the
        instant message webhook posts straight to the controller with no queue
        behind it, so a raise here is a 5xx that RingCentral retries, and both
        the Slash message and the board update append rather than upsert. One
        failed photo must not duplicate the message it came with.

        Three scalars rather than the model's Attachment so services/monday does
        not have to import a type out of services/ring_central.
        """
        if not update_id:
            return None

        try:
            self.client.add_file_to_update(
                update_id=update_id, filename=filename, content=content,
                content_type=content_type,
                op=f"Attach {filename} to update {update_id}")
        except Exception:
            logger.exception("Attaching %s to update %s failed", filename, update_id)
        return None

    def create_update_to_cl(self, phone: str, body: str) -> str | None:
        """Post `body` on the Clients & Leads item for `phone`, if that person has one."""
        return self._create_update_to_board(
            phone=phone, body=body, board_id=CL_BOARD_ID,
            column_id=CLColIds.phone, label="Clients & Leads")

    def create_update_to_ab(self, phone: str, body: str) -> str | None:
        """Post `body` on the Applicants Board item for `phone`, if that person has one."""
        return self._create_update_to_board(
            phone=phone, body=body, board_id=AB_BOARD_ID,
            column_id=ABColIds.phone, label="Applicants Board")


@lru_cache(maxsize=1)
def get_controller() -> MondayController:
    """The one controller a warm container uses, as slash.controller.get_controller is.

    The requests.Session and its Authorization header are cached on the model, so
    building a new controller per message throws the connection pool away.
    """
    return MondayController()
