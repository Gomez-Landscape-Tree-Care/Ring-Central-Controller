from functools import lru_cache
import os

import boto3

ssm = boto3.client("ssm")

@lru_cache
def _get_param(env_var_name):
    """Resolve the SSM parameter whose path is held in ``env_var_name``."""
    path = os.environ[env_var_name]
    response = ssm.get_parameter(Name=path, WithDecryption=True)
    value = response["Parameter"]["Value"]
    return value

MAIN_COMPANY_LINE = "+18187187470"
# The RingCentral extension a call was placed *from* -> the name that extension
# reads as in a timeline entry, as in "Vig is calling Jane Doe", and (where the
# extension has one) its monday user id. Also the switch for whose calls are
# tracked at all: process_call drops a session from an extension not named here
# rather than writing the raw id onto the board.
RING_USERS: dict[str, dict] = {
    "63643255028": {"name": "Vig", "monday_user_id": "69942525"},
    "63536719028": {"name": "Jaime", "monday_user_id": "14382341"},
    "393708028": {"name": "Gilbert", "monday_user_id": "14382599"},
    "405497028": {"name": "GomezLTC"},
    "663812029": {"name": "Call Queue"},
}
CALL_QUEUE_EXTENSION = "663812029"
RC_CLIENT_ID = "WZPBY9P51XYfyE87ZiCHHS"
RC_SERVER = "https://platform.ringcentral.com"

SLASH_API_URL = "https://api.useslash.com"
SLASH_SERVICE_EMAIL = "admin@gomezltc.com"

MONDAY_API_URL = "https://api.monday.com/v2"
MONDAY_FILE_URL = f"{MONDAY_API_URL}/file"
JEFF_BOT = 'JEFF_BOT_MONDAY_API_KEY'
JEFF_BOT_USER_ID = '76113590'
MONDAY_USERS: dict[str, dict] = {
    '14382341': {
        'informal_name': 'Jaime',
        'full_name': 'Jaime Gomez',
        'api_key': 'JAIME_MONDAY_API_KEY',
    },
    '69942525': {
        'informal_name': 'Vig',
        'full_name': 'Vignesh Selveraj',
        'api_key': 'VIG_MONDAY_API_KEY',
    },
    '95263034': {
        'informal_name': 'Anderson',
        'full_name': 'Anderson Lee',
        'api_key': 'ANDERSON_MONDAY_API_KEY',
    },
    JEFF_BOT_USER_ID: {
        'informal_name': 'Jeff Bot',
        'full_name': 'Jeff Bot',
        'api_key': JEFF_BOT,
    },
}

LOCAL_TESTING = "AWS_LAMBDA_FUNCTION_NAME" not in os.environ

if LOCAL_TESTING:
    # .env is git-ignored and never read by the deployed Lambda - it only
    # exists so local runs have something to export into os.environ. Note it
    # holds the token itself, whereas in Lambda the env var holds the SSM path.
    from dotenv import load_dotenv

    load_dotenv()

    JEFF_BOT_MONDAY_API_KEY = os.environ[JEFF_BOT]
    RC_CLIENT_SECRET = os.environ['RC_CLIENT_SECRET']
    RC_JWT = os.environ['RC_JWT']
    SLASH_SERVICE_PASSWORD = os.environ['SLASH_SERVICE_PASSWORD']
else:
    JEFF_BOT_MONDAY_API_KEY = _get_param(JEFF_BOT)
    RC_CLIENT_SECRET = _get_param("RC_CLIENT_SECRET")
    RC_JWT = _get_param("RC_JWT")
    SLASH_SERVICE_PASSWORD = _get_param("SLASH_SERVICE_PASSWORD")