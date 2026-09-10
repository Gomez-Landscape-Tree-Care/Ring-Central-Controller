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
            status = _status(e)
            if status == 401:
                # The access token died before its stated expiry - revoked, or
                # the app's credentials changed. Clearing it sends the retry
                # back through login rather than replaying a dead token, which
                # is what makes 401 worth retrying where other 4xx are not.
                platform.auth().reset()
            inner = _response(e)
            # Logged here because the exception message alone is not enough:
            # RingCentral's errorCode in the body is what identifies the failure.
            logger.critical(
                f"HTTP {status}: {getattr(inner, 'text', '')[:500]}")
            raise

        return {}
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