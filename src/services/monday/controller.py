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
                                column_id: str, label: str) -> None:
        """Updates in Monday represent text messages for this automation."""
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
            self.client.create_update(
                item_id=item_id, body=body, op=f"Update {label} item {item_id}")
        except Exception:
            logger.exception("%s update failed for %s", label, phone)
        return None

    def create_update_to_cl(self, phone: str, body: str) -> None:
        """Post `body` on the Clients & Leads item for `phone`, if that person has one."""
        self._create_update_to_board(
            phone=phone, body=body, board_id=CL_BOARD_ID,
            column_id=CLColIds.phone, label="Clients & Leads")

    def create_update_to_ab(self, phone: str, body: str) -> None:
        """Post `body` on the Applicants Board item for `phone`, if that person has one."""
        self._create_update_to_board(
            phone=phone, body=body, board_id=AB_BOARD_ID,
            column_id=ABColIds.phone, label="Applicants Board")


@lru_cache(maxsize=1)
def get_controller() -> MondayController:
    """The one controller a warm container uses, as slash.controller.get_controller is.

    The requests.Session and its Authorization header are cached on the model, so
    building a new controller per message throws the connection pool away.
    """
    return MondayController()
