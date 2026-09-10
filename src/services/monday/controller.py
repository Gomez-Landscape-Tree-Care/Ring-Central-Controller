from services.monday.model import MondayModel

class MondayController:
    """Controller to handle business logic for all Monday operations"""

    def __init__(self, client: MondayModel = MondayModel()):
        self.client = client