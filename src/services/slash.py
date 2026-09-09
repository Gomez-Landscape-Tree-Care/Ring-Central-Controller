"""Client for the Slash backend."""


class SlashClient:
    """Requests to the Slash backend.

    The service password is passed in by the caller (see ``app.get_config``).
    """

    def __init__(self, service_password):
        self._service_password = service_password

    def _authenticate(self):
        raise NotImplementedError
