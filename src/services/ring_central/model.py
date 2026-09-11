import mimetypes
import os
from dataclasses import dataclass
from functools import lru_cache

from ringcentral import SDK
from ringcentral.http.api_exception import ApiException
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from config import (
    MAIN_COMPANY_LINE,
    RC_CLIENT_ID,
    RC_CLIENT_SECRET,
    RC_JWT,
    RC_SERVER,
)
from logger_config import logger

# Ceiling on RingCentral's own backoff hint, same reasoning as MondayClient's:
# a daily-limit reset can be further away than we are willing to hold the queue.
MAX_RETRY_AFTER = 90

# RingCentral rejects an SMS body longer than this.
MAX_SMS_CHARS = 1000

# RingCentral caps one MMS attachment near 1.5 MB, so this sits well clear of a
# real photo. It is here to stop a record that misreports its own size, not to be
# a budget - a 512 MB Lambda does not notice ten of these.
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024

def _response(exception: ApiException):
    """The requests.Response behind an ApiException, or None.

    None is a real case, not defensiveness: on a connection error or timeout the
    SDK builds its ApiException from a request with no response attached.
    """
    api_response = exception.api_response()
    return api_response.response() if api_response else None


def _status(exception: ApiException) -> int | None:
    """The HTTP status behind an ApiException, or None if it never got one."""
    return getattr(_response(exception), "status_code", None)


def _retry_after(exception: ApiException) -> float | None:
    """RingCentral's Retry-After in seconds, or None if absent/unparseable."""
    headers = getattr(_response(exception), "headers", None) or {}
    try:
        return float(headers.get("Retry-After") or 0) or None
    except (AttributeError, TypeError, ValueError):
        return None


def _is_retryable(exception: BaseException) -> bool:
    """Whether a second attempt could plausibly do better."""
    if not isinstance(exception, ApiException):
        return False
    status = _status(exception)
    if status is None:
        # Never reached RingCentral - connection error, timeout, DNS.
        return True
    return status in (401, 429) or status >= 500


_backoff = wait_exponential(multiplier=8, exp_base=8)


@dataclass(frozen=True)
class Attachment:
    """One downloaded message-store attachment, in the three fields an upload needs."""

    filename: str
    content: bytes
    content_type: str


def _media_type(response) -> str:
    """The bare media type off a response, without the charset parameter."""
    header = (getattr(response, "headers", None) or {}).get("Content-Type") or ""
    return header.split(";")[0].strip() or "application/octet-stream"


def _attachment_name(uri: str, content_type: str, hint: str = "") -> str:
    """A filename for downloaded bytes, preferring the one RingCentral gave.

    basename() because the hint is whatever the sending handset put in the MMS
    part and monday takes it verbatim. The fallback keeps the attachment id off
    the end of the uri so two photos on one message cannot collide, and always
    carries an extension - monday decides whether to render a thumbnail from the
    name, so a bare id shows up as a generic file.

    The subtype is the fallback for the extension because mimetypes does not
    know every media type a handset sends: image/heic answers None there and
    would otherwise be stored as .bin.
    """
    if hint:
        return os.path.basename(hint)

    suffix = mimetypes.guess_extension(content_type)
    if not suffix:
        subtype = content_type.rsplit("/", 1)[-1]
        suffix = f".{subtype}" if subtype.isalnum() else ".bin"
    return f"mms-{uri.split('?')[0].rstrip('/').rsplit('/', 1)[-1]}{suffix}"


def _wait_rc(retry_state):
    """Honour RingCentral's Retry-After when present, else exponential backoff.

    Waiting here rather than inside the request means the delay is skipped after
    the final attempt and stays interruptible by tenacity.
    """
    exception = retry_state.outcome.exception() if retry_state.outcome else None
    hinted = _retry_after(exception) if isinstance(exception, ApiException) else None
    if hinted:
        wait = min(hinted, MAX_RETRY_AFTER)
        logger.info(f"RingCentral asked for {hinted}s; waiting {wait}s before retry")
        return wait
    return _backoff(retry_state)


class RingCentralModel:
    def __init__(self):
        self._sdk = SDK(RC_CLIENT_ID, RC_CLIENT_SECRET, RC_SERVER)
        self._from_phone_number = "+18187187470"

    # ----------------------------------------------------------------------
    # Auth
    # ----------------------------------------------------------------------

    def _platform(self):
        """The SDK platform, logged in.

        `logged_in()` swallows the failed refresh described in the module
        docstring and answers False, which is exactly the cue to log in again.
        """
        platform = self._sdk.platform()
        if not platform.logged_in():
            platform.login(jwt=RC_JWT)
            logger.info("RingCentral logged in")
        return platform

    # ----------------------------------------------------------------------
    # Transport
    # ----------------------------------------------------------------------

    def request(self, method: str, path: str, body: dict | None = None,
                query_params: dict | None = None, op: str = "") -> dict:
        """Execute a RingCentral request and return the decoded response body.

        Retries transient failures, then re-raises with `op` named so the caller
        says which call failed. Alerting is not done here - see MondayClient for
        why that belongs to the handler.
        """
        try:
            logger.info(op)
            return self._execute(method, path, body, query_params)
        except Exception as e:
            where = f" [{op}]" if op else ""
            logger.exception(f"RingCentral Error{where}: {e}")
            if op:
                raise RuntimeError(f"{op}: {e}") from e
            raise

    @retry(
        wait=_wait_rc,
        stop=stop_after_attempt(3),
        retry=retry_if_exception(_is_retryable),
        reraise=True,
    )
    def _execute(self, method: str, path: str, body: dict | None = None,
                 query_params: dict | None = None) -> dict:
        """Send one request through the SDK and return the decoded body."""
        platform = self._platform()
        try:
            if method.upper() == "GET":
                response = platform.get(path, query_params=query_params)
                return response.json_dict()
            elif method.upper() == "POST":
                response = platform.post(path, body=body)
                return response.json_dict()
        except ApiException as e:
            self._note_api_failure(platform, e)
            raise

        return {}

    @staticmethod
    def _note_api_failure(platform, exception: ApiException) -> None:
        """Log what RingCentral actually said, and drop a token it has stopped honouring.

        The access token dying before its stated expiry - revoked, or the app's
        credentials changed - is what makes 401 worth retrying where other 4xx
        are not: clearing it sends the retry back through login rather than
        replaying a dead token. Shared with _download rather than written twice
        because a download that skipped the reset would replay the same dead
        token through all three attempts.

        The body is logged because the exception message alone is not enough -
        RingCentral's errorCode in the body is what identifies the failure.
        """
        status = _status(exception)
        if status == 401:
            platform.auth().reset()
        inner = _response(exception)
        logger.critical(f"HTTP {status}: {getattr(inner, 'text', '')[:500]}")

    def download(self, uri: str, filename: str = "", op: str = "") -> Attachment:
        """Fetch one attachment's bytes and the type RingCentral served them as.

        Separate from `request` because `_execute` answers `json_dict()`, which
        raises 'Response is not JSON' on an image. Login, retry and the 401
        token reset are the same, so `_download` carries the same decorator.

        Raises like every other method here; whether a missing photo is worth
        losing the message over is the caller's call, not this one's.
        """
        try:
            logger.info(op)
            return self._download(uri, filename)
        except Exception as e:
            where = f" [{op}]" if op else ""
            logger.exception(f"RingCentral Error{where}: {e}")
            if op:
                raise RuntimeError(f"{op}: {e}") from e
            raise

    @retry(
        wait=_wait_rc,
        stop=stop_after_attempt(3),
        retry=retry_if_exception(_is_retryable),
        reraise=True,
    )
    def _download(self, uri: str, filename: str = "") -> Attachment:
        """Fetch one absolute media uri and return its bytes.

        `body()` rather than `json_dict()`, and the absolute uri passes through
        create_url untouched while inflate_request still signs it, so the
        media.ringcentral.com host needs no token plumbing of its own.

        An oversized body raises a plain RuntimeError rather than an
        ApiException so _is_retryable answers False - there is nothing to gain
        from pulling the same blob down twice more.
        """
        platform = self._platform()
        try:
            response = platform.get(uri)
        except ApiException as e:
            self._note_api_failure(platform, e)
            raise

        content = response.body()
        if len(content) > MAX_ATTACHMENT_BYTES:
            raise RuntimeError(
                f"attachment is {len(content)} bytes, over the "
                f"{MAX_ATTACHMENT_BYTES} byte cap")

        content_type = _media_type(response.response())
        return Attachment(
            filename=_attachment_name(uri, content_type, filename),
            content=content,
            content_type=content_type,
        )

    # ----------------------------------------------------------------------
    # Domain
    # ----------------------------------------------------------------------

    def send_sms(self, text: str, to_number: str, op: str = "") -> str:
        """Send one SMS and return the message id.

        An unnormalizable destination raises rather than returning None: there is
        no partial success to report, and a caller that treats a dropped message
        as a sent one is worse off than one that sees the failure.
        """
        data = self.request(
            "POST",
            "/restapi/v1.0/account/~/extension/~/sms",
            body={
                "from": {"phoneNumber": self._from_phone_number},
                "to": [{"phoneNumber": to_number}],
                "text": text[:MAX_SMS_CHARS],
            },
            op=op or f"send_sms to {to_number}",
        )
        return str(data.get("id") or "")

    def get_message(self, extension_id, message_id, op: str = "") -> dict:
        """Fetch the message-store record for one id.

        Fetched by path because the list endpoint has no id filter: passing
        `messageId` there is ignored and answers the mailbox's first page.
        """
        return self.request(
            "GET",
            f"/restapi/v1.0/account/~/extension/{extension_id}"
            f"/message-store/{message_id}",
            op=op or f"get_message {message_id}",
        )


@lru_cache(maxsize=1)
def get_model() -> RingCentralModel:
    """The process-wide client, so a warm Lambda logs in once."""
    return RingCentralModel()