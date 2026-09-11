import json

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from logger_config import logger
from services.monday import queries

from config import (
    JEFF_BOT_MONDAY_API_KEY,
    MONDAY_API_URL,
    MONDAY_FILE_URL,
)

TIMEOUT = 60

# Ceiling on Monday's own backoff hint. A daily-limit reset can be hours away
MAX_RETRY_AFTER = 90

class MondayRetryAfter(RuntimeError):
    """A Monday GraphQL error, carrying its suggested backoff when one is given."""

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


def _retry_after(error):
    """Monday's suggested backoff in seconds, or None if absent/unparseable."""
    try:
        return float(error.get("extensions", {}).get("retry_in_seconds") or 0) or None
    except (AttributeError, TypeError, ValueError):
        return None


_backoff = wait_exponential(multiplier=8, exp_base=8)


def _wait_monday(retry_state):
    """Honour Monday's retry_in_seconds when present, else exponential backoff.

    Waiting here rather than inside the request means the delay is skipped
    after the final attempt and stays interruptible by tenacity.
    """
    exception = retry_state.outcome.exception() if retry_state.outcome else None
    hinted = getattr(exception, "retry_after", None)
    if hinted:
        wait = min(hinted, MAX_RETRY_AFTER)
        logger.info(f"Monday asked for {hinted}s; waiting {wait}s before retry")
        return wait
    return _backoff(retry_state)


class MondayModel:
    def __init__(self, api_key=JEFF_BOT_MONDAY_API_KEY, timeout=TIMEOUT):
        self._url = MONDAY_API_URL
        self._timeout = timeout
        self._session = requests.Session()
        # Content-Type is deliberately not pinned here: requests derives it per
        # request, application/json for json= and multipart with a boundary for
        # files=, and a pinned value would break the upload transport.
        self._session.headers.update({"Authorization": api_key})

    # ----------------------------------------------------------------------
    # Transport
    # ----------------------------------------------------------------------

    def request(self, query: str, op: str = "", files: dict | None = None) -> dict:
        """Execute a Monday.com GraphQL request and return payload["data"].

        Retries transient failures, then re-raises with `op` named so the alert
        in lambda_handler says which call failed.
        """
        try:
            logger.info(op)
            return self._execute(query, files)
        except Exception as e:
            where = f" [{op}]" if op else ""
            logger.exception(f'Monday Error{where}: {e}\nQuery: {query}')
            if op:
                raise RuntimeError(f"{op}: {e}") from e
            raise

    @retry(wait=_wait_monday, stop=stop_after_attempt(3), reraise=True)
    def _execute(self, query: str, files: dict | None = None) -> dict:
        """Post the GraphQL request and return payload["data"], raising on errors.
        """
        if files:
            # The GraphQL multipart request spec: the mutation travels in `query`,
            # `map` points a form part at the mutation's $file variable, and that
            # part carries the bytes. Uploads go to their own endpoint.
            response = self._session.post(
                MONDAY_FILE_URL,
                data={"query": query, "map": json.dumps({"image": "variables.file"})},
                files=files,
                timeout=self._timeout,
            )
        else:
            response = self._session.post(
                self._url,
                json={"query": query},
                timeout=self._timeout,
            )

        # Parse before raise_for_status: Monday reports rate-limit and complexity
        # errors as HTTP 429 with the backoff hint in the body, which
        # raise_for_status would discard. Error bodies aren't always JSON
        # (gateway HTML, empty 5xx), so fall through to the status check.
        try:
            payload = response.json()
        except ValueError:  # requests raises JSONDecodeError, a ValueError
            payload = None

        if payload and "errors" in payload:
            status = f"HTTP {response.status_code}: " if response.status_code >= 400 else ""
            raise MondayRetryAfter(
                f"{status}monday.com request failed: {payload['errors']}",
                _retry_after(payload["errors"][0]),
            )

        if response.status_code >= 400:
            # Log the body before raise_for_status discards it.
            logger.critical(f"HTTP {response.status_code}: {response.text[:500]}")
        response.raise_for_status()

        if payload is None:
            raise RuntimeError(
                f"monday.com returned a non-JSON body: {response.text[:500]}")

        return payload.get("data") or {}

    @staticmethod
    def _created_id(data: dict, field: str, op: str) -> str:
        """The new item's id, or an error.

        A create that comes back without an id is not a success worth passing on:
        returning None here would let the caller go on to hang a subitem off
        nothing, or report an applicant as written when they are not.

        Monday's GraphQL ID comes back as a string, which is the one
        representation of an item id this codebase uses - str() only in case that
        ever changes.
        """
        item_id = ((data or {}).get(field) or {}).get("id")
        if not item_id:
            raise RuntimeError(f"{field} returned no id{f' [{op}]' if op else ''}: {data!r}")
        return str(item_id)

    def create_update(self, item_id: str, body: str, op: str = "") -> str:
        """Post an update on an item's Updates section and return the update id.

        Authored by whoever this client's token belongs to - Monday attributes an
        update to the account that wrote it, which is why the caller may hand this
        one a key other than Jeff Bot's.
        """
        data = self.request(
            queries.create_update(item_id=str(item_id), body=body),
            op=op,
        )
        return self._created_id(data, "create_update", op)

    def add_file_to_update(self, update_id: str, filename: str, content: bytes,
                           content_type: str, op: str = "") -> str:
        """Attach one file to an existing update and return the new asset's id.

        The files dict is keyed "image" because _execute's map is - see
        queries.add_file_to_update for why both ends are pinned. The three-tuple
        is requests' own form for a part with an explicit filename and content
        type; handing it bare bytes sends the part unnamed and monday stores an
        asset called "image" with no extension, which it then renders as a
        generic file rather than a thumbnail.
        """
        data = self.request(
            queries.add_file_to_update(update_id=str(update_id)),
            op=op,
            files={"image": (filename, content, content_type)},
        )
        return self._created_id(data, "add_file_to_update", op)

    def find_item_id_by_phone(self, board_id: int, column_id: str, phone: str,
                              op: str = "") -> str | None:
        """The id of the first item on `board_id` matching `phone`, or None if there is none.

        A response without a `boards` list is an error rather than a miss:
        returning None there would answer "this number is not on the board" every
        time Monday hiccups, and the caller uses that answer to decide whether to
        write at all.
        """
        data = self.request(
            queries.find_item_id_by_phone(
                board_id=board_id, column_id=column_id, phone=phone),
            op=op,
        )
        boards = data.get("boards")
        if not boards or boards[0] is None:
            raise RuntimeError(
                f"board {board_id} missing from response{f' [{op}]' if op else ''}: {data!r}")
        items = (boards[0].get("items_page") or {}).get("items") or []
        return str(items[0]["id"]) if items else None
