import threading
from datetime import datetime

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from config import SLASH_API_URL, SLASH_SERVICE_EMAIL, SLASH_SERVICE_PASSWORD
from logger_config import logger
from utils.normalize import normalize_phone_with_plus

_TIMEOUT = 30


def _wire_phone(phone: str) -> str:
    """A phone in the `+1xxxxxxxxxx` form Slash keys on."""
    return normalize_phone_with_plus(phone) or phone


class SlashError(Exception):
    """Raised when a Slash backend call fails (non-2xx or no usable company)."""


class SlashModel:
    """The single outbound HTTP connection to the Slash backend.

    One `requests.Session` (connection reuse), one cached bearer token, and one
    cached company id — all held for the life of a warm Lambda container, guarded
    by a `threading.RLock` against concurrent callers on the same container. The
    token is refreshed reactively (see `_authed_request`), never on a timer.

    Every public method here is a pure data request: it always raises `SlashError`
    (or lets a `requests.exceptions.RequestException` propagate) on failure rather
    than swallowing it — deciding whether a call is best-effort is the controller's
    job, not the model's.
    """

    def __init__(self) -> None:
        self._session = requests.Session()
        self._base_url = SLASH_API_URL.rstrip("/")
        self._token: str | None = None
        self._company_id: str | None = None
        self._lock = threading.RLock()

    # ---- low-level HTTP ----

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(requests.exceptions.RequestException),
        reraise=True,
    )
    def _send(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        json: dict | None = None,
        params: dict | None = None,
    ) -> requests.Response:
        """Issue one HTTP request through the shared session.

        Retries transient network errors (connection/timeout) up to 3 times with
        exponential backoff; a non-2xx HTTP response is not an exception here and is
        returned as-is for the caller to interpret.

        `params` is the query string, for the read routes — `requests` percent-encodes
        it, so a phone's leading `+` goes out as `%2B` rather than the bare `+` a
        hand-built querystring would leave (which decodes to a space).
        """
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return self._session.request(
            method,
            f"{self._base_url}{path}",
            headers=headers,
            json=json,
            params=params,
            timeout=_TIMEOUT,
        )

    def _authed_request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
        _retried: bool = False,
    ) -> requests.Response:
        """Issue an authenticated request, refreshing the cached token once on a 401/403.

        The token is cached indefinitely (see `_ensure_token`) rather than proactively
        expired, so the only signal that it has gone stale is the backend actually
        rejecting it. On a 401/403 the cached token is dropped and exactly one fresh
        sign-in + retry is attempted before giving up — same shape as the
        retry-and-relogin pattern in `rc_platform.py`, minus the SSM cross-container
        sharing (not needed here; see the module docstring).

        `params` must be forwarded on **both** sends — the first and the post-refresh
        retry. Dropping it on the retry would turn a filtered read into an unfiltered
        one on any 401, which comes back 200 with somebody else's rows rather than as
        an error.
        """
        token = self._ensure_token()
        resp = self._send(method, path, token=token, json=json, params=params)
        if resp.status_code in (401, 403) and not _retried:
            logger.warning(
                f"[slash] {method} {path} returned {resp.status_code}; "
                "refreshing token and retrying once"
            )
            with self._lock:
                self._token = None
            return self._authed_request(
                method, path, json=json, params=params, _retried=True
            )
        return resp

    def _ensure_token(self) -> str:
        """Return the cached bearer token, signing in only if none is cached yet."""
        with self._lock:
            if self._token is None:
                self._token = self._sign_in()
            return self._token

    def _sign_in(self) -> str:
        """POST /auth/sign-in with the service account -> bearer JWT."""
        resp = self._send(
            "POST",
            "/auth/sign-in",
            json={"email": SLASH_SERVICE_EMAIL, "password": SLASH_SERVICE_PASSWORD},
        )
        if resp.status_code != 200:
            logger.error(f"[slash] sign-in failed ({resp.status_code}): {resp.text}")
            raise SlashError(f"sign-in failed ({resp.status_code})")
        return resp.json()["access_token"]

    def _ensure_company_id(self) -> str:
        """Return the cached company id, fetching it (signing in first if needed) if none is cached."""
        with self._lock:
            if self._company_id is None:
                self._company_id = self._first_company_id()
            return self._company_id

    def _first_company_id(self) -> str:
        """GET /companies -> the first company's id (the 'grab the first company' rule)."""
        resp = self._authed_request("GET", "/companies")
        if resp.status_code != 200:
            logger.error(f"[slash] fetching companies failed ({resp.status_code}): {resp.text}")
            raise SlashError(f"fetching companies failed ({resp.status_code})")
        companies = resp.json()
        if not companies:
            raise SlashError("service account belongs to no companies")
        return companies[0]["id"]

    # ---- endpoint methods (pure data requests — always raise, never swallow) ----

    def upsert_person(
        self,
        *,
        phone: str,
        person_type: str,
        first_name: str | None = None,
        last_name: str | None = None,
        email: str | None = None,
    ) -> dict:
        """POST /companies/{id}/persons -> the person, created or matched on phone.

        `person_type` rather than `type`, which is a builtin - the wire name is
        still `type`. A name left None is omitted from the body rather than sent
        as an explicit null, so an upsert that knows less than the row already
        holds cannot blank a field somebody else filled.

        Success is any 2xx, not `== 200` like the two auth calls above: this one
        route answers 201 on the create leg and 200 on the match leg, and both
        mean the person is there.

        `phone` is the bare form every caller holds; _wire_phone puts it in the
        `+` form Slash keys on.
        """
        body = {"phone_number": _wire_phone(phone), "type": person_type}
        for key, value in (("first_name", first_name), ("last_name", last_name),
                           ("email", email)):
            if value:
                body[key] = value

        company_id = self._ensure_company_id()
        resp = self._authed_request("POST", f"/companies/{company_id}/persons", json=body)
        if not resp.ok:
            logger.error(f"[slash] upserting person failed ({resp.status_code}): {resp.text}")
            raise SlashError(f"upserting person failed ({resp.status_code})")
        return resp.json()

    def create_timeline_entry(
        self,
        *,
        phone: str,
        entry: str,
        entry_type: str,
        timestamp: datetime | None = None,
        outbound: bool = True,
    ) -> dict:
        """POST /companies/{id}/persons/timeline -> the entry, matched to a person by phone.

        Not an upsert, unlike the route above: every call appends a new entry, so
        a replayed message leaves a duplicate line rather than a no-op.

        The timestamp is sent as ISO-8601 rather than the datetime itself, which
        `requests`' json= cannot serialize. Left out entirely when None, so the
        backend stamps its own rather than being handed a null.
        """
        body = {
            "phone_number": _wire_phone(phone),
            "entry": entry,
            "entry_type": entry_type,
            "outbound": outbound,
        }
        if timestamp is not None:
            body["timestamp"] = timestamp.isoformat()

        company_id = self._ensure_company_id()
        resp = self._authed_request(
            "POST", f"/companies/{company_id}/persons/timeline", json=body)
        if not resp.ok:
            logger.error(f"[slash] creating timeline entry failed ({resp.status_code}): {resp.text}")
            raise SlashError(f"creating timeline entry failed ({resp.status_code})")
        return resp.json()

    def create_text_message(
        self,
        *,
        phone: str,
        text: str,
        outbound: bool,
        monday_user_id: str | None = None,
        timestamp: datetime | None = None,
    ) -> dict:
        """POST /companies/{id}/persons/messages -> the message, matched to a person by phone.

        Appends like the timeline route rather than upserting like the person one,
        so a replayed message leaves a duplicate text rather than a no-op.

        `monday_user_id` is who sent it, and this is the one route that carries
        one. It goes out as an int while every Monday id on this side is a str -
        the conversion belongs here for the reason _wire_phone's does, so callers
        pass the id they already hold. Omitted entirely when there is no Monday
        user behind the text, as an inbound SMS or a Calendly booking has none.

        The timestamp is sent as ISO-8601 rather than the datetime itself, which
        `requests`' json= cannot serialize. Left out entirely when None, so the
        backend stamps its own rather than being handed a null.
        """
        body = {
            "phone_number": _wire_phone(phone),
            "text": text,
            "outbound": outbound,
        }
        if monday_user_id:
            body["monday_user_id"] = int(monday_user_id)
        if timestamp is not None:
            body["timestamp"] = timestamp.isoformat()

        company_id = self._ensure_company_id()
        resp = self._authed_request(
            "POST", f"/companies/{company_id}/persons/messages", json=body)
        if not resp.ok:
            logger.error(f"[slash] creating text message failed ({resp.status_code}): {resp.text}")
            raise SlashError(f"creating text message failed ({resp.status_code})")
        return resp.json()

    def timeline_entries(self, *, phone: str) -> list[dict]:
        """GET /companies/{id}/persons/timeline -> every entry kept for this phone.

        params= rather than a querystring built into the path: this is the read
        _send's docstring is about, since the phone's leading `+` has to go out
        as %2B and a bare one decodes to a space.

        A phone Slash has never heard of answers with an empty list, which is not
        an error - only a non-2xx is.
        """
        company_id = self._ensure_company_id()
        resp = self._authed_request(
            "GET", f"/companies/{company_id}/persons/timeline",
            params={"phone_number": _wire_phone(phone)})
        if not resp.ok:
            logger.error(f"[slash] fetching timeline failed ({resp.status_code}): {resp.text}")
            raise SlashError(f"fetching timeline failed ({resp.status_code})")
        return resp.json()