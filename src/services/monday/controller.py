from dataclasses import dataclass
from functools import lru_cache

from config import AB_BOARD_ID, CL_BOARD_ID, TEST_PHONE
from logger_config import logger
from services.monday.column_ids import ABColIds, CLColIds
from services.monday.model import MondayModel
from utils.normalize import normalize_phone, normalize_phone_with_plus


@dataclass(frozen=True)
class Board:
    """One board, and the columns this service reads a person off or writes to.

    The board id rides along with the column ids because change_column_value
    resolves a column against its board, where an update hangs off the item
    alone - so a caller holding an item id still cannot write a column without
    knowing which board it came from.
    """

    id: int
    phone_column: str
    timeline_column: str
    label: str


CL = Board(id=CL_BOARD_ID, phone_column=CLColIds.phone,
           timeline_column=CLColIds.timeline, label="Clients & Leads")
AB = Board(id=AB_BOARD_ID, phone_column=ABColIds.phone,
           timeline_column=ABColIds.timeline, label="Applicants Board")

# Both boards carry the same person, so every write this service makes goes to
# whichever of them has them. Order is the order the writes land in.
BOARDS = (CL, AB)

# A webhook names a board by id, where every read and write below needs the
# column ids that go with it.
BOARDS_BY_ID = {board.id: board for board in BOARDS}


def board_for_id(board_id) -> Board | None:
    """The Board `board_id` names, or None for one this service does not handle.

    None rather than a raise: a webhook can fire from any board somebody points a
    subscription at - the AB subitems board among them - and a board this service
    holds no columns for is a configuration fact rather than a failure three
    retries and a dead letter entry would improve.

    The id is int()ed because a Board holds one and a webhook may put a string
    there, the mirror of the str() RING_USERS lookups do.
    """
    try:
        return BOARDS_BY_ID.get(int(board_id))
    except (TypeError, ValueError):
        logger.info("Unusable board id %r", board_id)
        return None


class MondayController:
    """Controller to handle business logic for all Monday operations"""

    def __init__(self, client: MondayModel = MondayModel()):
        self.client = client

    def _find_item_id(self, board: Board, match_key: str) -> str | None:
        """The id of `board`'s item for an already-normalized phone, or None.

        None covers both ways this gives up, a failed lookup and no such item.
        Those are not two outcomes a caller can act on differently: either way
        there is nothing on that board to write to.
        """
        try:
            item_id = self.client.find_item_id_by_phone(
                board_id=board.id, column_id=board.phone_column, phone=match_key,
                op=f"Find {board.label} item for {match_key}")
        except Exception:
            logger.exception("%s lookup failed for %s", board.label, match_key)
            return None

        if not item_id:
            logger.info("No %s item for %s", board.label, match_key)
        return item_id

    def resolve_items(self, phone: str) -> list[tuple[Board, str]]:
        """Every board holding an item for `phone`, paired with that item's id.

        The one board search a message pays for, resolved up front and handed
        back rather than repeated per write: an SMS posts an update and rebuilds
        the Timeline column on the same item, and finding it twice doubles what
        each message costs against monday's complexity budget.

        The rollout gate lives here rather than at the writes below, because
        every one of them needs an item id and this is where item ids come from.
        A phone outside the rollout resolves to no boards, which leaves the
        callers nothing to write to and nothing to guard.
        """
        if phone != TEST_PHONE:
            return []

        match_key = normalize_phone(phone)
        if not match_key:
            logger.info("Unusable phone %s, skipping board lookups", phone)
            return []

        items = []
        for board in BOARDS:
            item_id = self._find_item_id(board, match_key)
            if item_id:
                items.append((board, item_id))
        return items

    def item_phone(self, board: Board, item_id: str) -> str | None:
        """The phone on one item, in the +1xxxxxxxxxx form every caller wants.

        The lookup resolve_items cannot serve: that one searches for the item
        behind a phone, where the create_update webhook arrives naming an item
        and needing the person to text.

        The board comes in for the reason write_timeline's does: a phone column
        belongs to a board, so an item id on its own does not say where to read.

        Normalized here rather than by the caller because the column holds
        whatever an agent typed, and everything done with this answer wants the
        one form: TEST_PHONE is compared against it, RingCentral sends to it, and
        Slash keys on it.

        Raises where _find_item_id swallows, and the difference is what has
        happened by the time each runs: this is the first call the handler makes,
        so a failed read costs a free retry off the queue, where swallowing it
        would drop an agent's text in silence.
        """
        text = self.client.item_column_text(
            item_id=item_id, column_id=board.phone_column,
            op=f"Read {board.label} phone on item {item_id}")
        if not text:
            logger.info("No phone on %s item %s", board.label, item_id)
            return None

        phone = normalize_phone_with_plus(text)
        if not phone:
            logger.warning("Unusable phone %r on %s item %s",
                           text, board.label, item_id)
        return phone

    def create_update(self, board: Board, item_id: str, body: str) -> str | None:
        """Post `body` on one item's Updates section.

        Updates in Monday represent text messages for this automation. Answers
        the new update's id so a caller can hang a photo off it, and None when
        the write fails, which means there is nothing to attach to.
        """
        try:
            return self.client.create_update(
                item_id=item_id, body=body,
                op=f"Update {board.label} item {item_id}")
        except Exception:
            logger.exception("%s update failed for item %s", board.label, item_id)
        return None

    def write_timeline(self, board: Board, item_id: str, text: str) -> None:
        """Overwrite one item's Timeline column with `text`.

        A replace rather than a prepend: the caller renders the whole column
        from Slash, which is the record this service and both board automations
        share, so a rebuild also picks up whatever the other two have written
        since this one last looked.

        Swallowed like add_photo_to_update below and for the same reason - this
        runs after the Slash entry and the board updates have landed, and the
        instant message webhook has no queue behind it, so a raise is a 5xx
        RingCentral retries into a duplicate text and duplicate updates.
        """
        try:
            self.client.write_long_text_column(
                board_id=board.id, item_id=item_id,
                column_id=board.timeline_column, text=text,
                op=f"Rebuild {board.label} timeline on item {item_id}")
        except Exception:
            logger.exception("%s timeline column failed for item %s",
                             board.label, item_id)
        return None

    def add_photo_to_update(self, update_id: str | None, filename: str,
                            content: bytes, content_type: str) -> None:
        """Hang one photo off an update already posted, if there is one.

        A falsy update_id is the ordinary case rather than an error: create_update
        answers None for a write that failed, and a person outside the rollout or
        with no item on the board never gets that far, because resolve_items
        hands their caller no item to post on in the first place.

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


@lru_cache(maxsize=1)
def get_controller() -> MondayController:
    """The one controller a warm container uses, as slash.controller.get_controller is.

    The requests.Session and its Authorization header are cached on the model, so
    building a new controller per message throws the connection pool away.
    """
    return MondayController()
