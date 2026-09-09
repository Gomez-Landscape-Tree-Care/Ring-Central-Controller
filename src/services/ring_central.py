"""Client for the Ring Central Service."""

TOKEN_URL = "/restapi/oauth/token"
JWT_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:jwt-bearer"


class RingCentralService:
    """Ring Central operations: calls, SMS, and subscription management.

    Credentials are passed in by the caller (see ``app.get_config``) so that all
    SSM resolution stays in one place.
    """

    def __init__(self, client_secret, jwt):
        self._client_secret = client_secret
        self._jwt = jwt
        self._access_token = None

    def _authenticate(self):
        """Exchange the JWT for an access token and cache it."""
        raise NotImplementedError
