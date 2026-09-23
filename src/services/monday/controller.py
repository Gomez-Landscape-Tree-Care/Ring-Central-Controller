from dataclasses import dataclass
from functools import lru_cache

from config import (AB_BOARD_ID, CL_BOARD_ID, JEFF_BOT_USER_ID, MONDAY_USERS)
from logger_config import logger
from services.monday.column_ids import ABColIds, CLColIds
from services.monday.model import MondayModel
from services.monday.statuses import ABOutputs, CLOutputs
from utils.normalize import normalize_phone, normalize_phone_with_plus


@dataclass(frozen=True)
class Board:
    """One board, and the columns this service reads a person off or writes to.

    The board id rides along with the column ids because a column write resolves
    a column against its board, where an update hangs off the item alone - so a
    caller holding an item id still cannot write a column without knowing which
    board it came from.
    """

    id: int
    phone_column: str
    timeline_column: str
    output_column: str
    outputs: type[ABOutputs | CLOutputs]
    label: str
    automations: str


# The two boards spell the same two labels differently - "Text sent" on CL,
# "Text Sent" on AB - and monday matches a label by its exact text, so each
# board carries its own set the way it carries its own column ids.
CL = Board(id=CL_BOARD_ID, phone_column=CLColIds.phone,
           timeline_column=CLColIds.timeline, output_column=CLColIds.output,
           outputs=CLOutputs, label="Clients & Leads", automations=CLColIds.automations)
AB = Board(id=AB_BOARD_ID, phone_column=ABColIds.phone,
           timeline_column=ABColIds.timeline, output_column=ABColIds.output,
           outputs=ABOutputs, label="Applicants Board", automations=ABColIds.automations)

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

    def _find_item(self, board: Board, match_key: str) -> str | None:
        """`board`'s item id for an already-normalized phone, or None.

        None covers both ways this gives up, a failed lookup and no such item.
        Those are not two outcomes a caller can act on differently: either way
        there is nothing on that board to write to.
        """
        try:
            found = self.client.find_item_by_phone(
                board_id=board.id, column_id=board.phone_column, phone=match_key,
                op=f"Find {board.label} item for {match_key}")
        except Exception:
            logger.exception("%s lookup failed for %s", board.label, match_key)
            return None

        if not found:
            logger.info("No %s item for %s", board.label, match_key)
        return found

    def resolve_items(self, phone: str) -> list[tuple[Board, str]]:
        """Every board holding an item for `phone`, paired with that item's id.

        The one board search a message pays for, resolved up front and handed
        back rather than repeated per write: an SMS posts an update and rebuilds
        the Timeline column on the same item, and finding it twice doubles what
        each message costs against monday's complexity budget.
        """
        match_key = normalize_phone(phone)
        if not match_key:
            logger.info("Unusable phone %s, skipping board lookups", phone)
            return []

        items = []
        for board in BOARDS:
            item_id = self._find_item(board, match_key)
            if item_id:
                items.append((board, item_id))
        return items

    def item_phone(self, board: Board, item_id: str) -> str | None:
        """The phone on one item, in the +1xxxxxxxxxx form every caller wants.

        The lookup resolve_items cannot serve: that one searches for the item
        behind a phone, where the create_update webhook arrives naming an item
        and needing the person to text.

        The board comes in for the reason update_columns' does: a phone column
        belongs to a board, so an item id on its own does not say where to read.

        Raises where _find_item swallows, and the difference is what has
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

    def item_name(self, board: Board, item_id: str) -> str | None:
        """The name on one item - the person the board shows, not a column.

        Swallows where item_phone raises, and the difference is what the answer
        is for: a phone decides whether somebody gets texted, where a name only
        decides how a timeline line reads. Every caller holds a fallback, so a
        failed read costs a line that names the number instead of the person.

        The board comes in only to name the failure - a name hangs off the item
        alone, where a column has to be resolved against its board.
        """
        try:
            return self.client.item_name(
                item_id=item_id, op=f"Read {board.label} name on item {item_id}")
        except Exception:
            logger.exception("%s name lookup failed for item %s", board.label, item_id)
            return None

    def create_update(self, board: Board, item_id: str, body: str,
                      monday_user_id: str = JEFF_BOT_USER_ID) -> str | None:
        """Post `body` on one item's Updates section, as `monday_user_id`.

        Updates in Monday represent text messages for this automation. Answers
        the new update's id so a caller can hang a photo off it, and None when
        the write fails, which means there is nothing to attach to.

        The author rides in as an id rather than a key so nothing above the model
        handles token material; who that id resolves to is config.monday_api_key's
        to settle, down to the Jeff Bot fallback for a user holding no key.

        Named in `op` because that string is what CloudWatch shows and what the
        RuntimeError request raises carries, and "as Vig" is the difference
        between a write that failed and one whose key is the wrong one.
        """
        author = (MONDAY_USERS.get(str(monday_user_id)) or {}).get(
            "informal_name", monday_user_id)
        try:
            return self.client.create_update(
                item_id=item_id, body=body, monday_user_id=monday_user_id,
                op=f"Update {board.label} item {item_id} as {author}")
        except Exception:
            logger.exception("%s update failed for item %s", board.label, item_id)
        return None

    def update_columns(self, board: Board, item_id: str, values: dict) -> None:
        """Overwrite every column in `values` on one item, in one monday call.

        `values` carries monday's own shape per column - {"text": ...} for the
        Timeline, {"label": ...} for Outputs - so a caller writing both pays for
        one call rather than two. The instant message webhook is what makes that
        worth doing: it runs every write this service makes against the same
        complexity budget, with no queue to spread them over.

        Swallowed like add_photo_to_update below and for the same reason - this
        runs after the Slash entry and the board updates have landed, and that
        same webhook has no queue behind it, so a raise is a 5xx RingCentral
        retries into a duplicate text and duplicate updates.
        """
        if not values:
            return None

        try:
            self.client.update_column_values(
                board_id=board.id, item_id=item_id, values=values,
                op=f"Write {board.label} columns {', '.join(values)} on item {item_id}")
        except Exception:
            logger.exception("%s column write failed for item %s",
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
