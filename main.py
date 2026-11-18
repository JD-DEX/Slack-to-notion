import logging
import os
import sys
from typing import Callable, List
from slack_bolt import App, BoltContext
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_bolt.error import BoltError
from slack_sdk.errors import SlackApiError

from app.access_control import ALLOWED_USERS_ENV_VAR, build_allowlist_middleware, parse_allowed_user_ids
from app.slack_events import register_slack_event_handlers
from dotenv import load_dotenv

load_dotenv()

# These libraries log raw Slack payloads and HTTP bodies at DEBUG level.
# Payloads contain user form input, so keep them at INFO or higher at all times.
_THIRD_PARTY_LOGGERS = ("slack_bolt", "slack_sdk", "notion_client", "httpx", "httpcore", "urllib3", "websocket")

# Each required setting and the value that the operator must give it.
REQUIRED_SETTINGS = {
    "SLACK_BOT_TOKEN": "the Bot User OAuth Token (xoxb-...) from Settings > Install App",
    "SLACK_APP_TOKEN": "an app-level token (xapp-...) with the connections:write scope",
    "NOTION_API_TOKEN": "the Notion internal integration token",
    ALLOWED_USERS_ENV_VAR: "a comma-separated list of Slack user IDs (for example U01234ABCDE,U05678FGHIJ)",
}


def configure_logging() -> None:
    level_name = os.environ.get("LOG_LEVEL", "INFO").strip().upper() or "INFO"
    level = logging.getLevelName(level_name)
    if not isinstance(level, int):
        sys.exit(f"Startup failed: LOG_LEVEL {level_name!r} is not valid. Use DEBUG, INFO, WARNING, ERROR or CRITICAL.")
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s %(message)s", level=level)
    for name in _THIRD_PARTY_LOGGERS:
        logging.getLogger(name).setLevel(max(level, logging.INFO))


def missing_settings() -> List[str]:
    """Return one line for each required setting that is empty or not set."""
    missing = []
    for name, expected in REQUIRED_SETTINGS.items():
        raw = os.environ.get(name)
        empty = not parse_allowed_user_ids(raw) if name == ALLOWED_USERS_ENV_VAR else not (raw or "").strip()
        if empty:
            missing.append(f"- {name} is empty or not set. Set it to {expected}.")
    return missing


# Fail closed: refuse to start without every token and the allowlist.
_missing = missing_settings()
if _missing:
    sys.exit("Startup failed. Fix these settings, then start the app again:\n" + "\n".join(_missing))

slack_bot_token = os.environ["SLACK_BOT_TOKEN"].strip()
slack_app_level_token = os.environ["SLACK_APP_TOKEN"].strip()
notion_api_token = os.environ["NOTION_API_TOKEN"].strip()
allowed_user_ids = parse_allowed_user_ids(os.environ[ALLOWED_USERS_ENV_VAR])

try:
    # Bolt calls auth.test here to check the bot token.
    app = App(token=slack_bot_token)
except BoltError:
    sys.exit(
        "Startup failed: Slack did not accept SLACK_BOT_TOKEN. "
        "Copy the current Bot User OAuth Token (xoxb-...) from Settings > Install App, then start the app again."
    )
except OSError as e:
    sys.exit(
        f"Startup failed: the app could not connect to Slack ({e}). "
        "Check the network connection and proxy settings, then start the app again."
    )

# Register the allowlist first so that it runs before all other middleware and listeners.
app.use(build_allowlist_middleware(allowed_user_ids))


@app.middleware
def attach_notion_token(context: BoltContext, next_: Callable):
    context["notion_token"] = notion_api_token
    next_()


register_slack_event_handlers(app)

if __name__ == "__main__":
    configure_logging()
    try:
        SocketModeHandler(app, slack_app_level_token).start()
    except SlackApiError as e:
        sys.exit(
            f"Startup failed: Slack did not accept SLACK_APP_TOKEN (Slack error: {e.response.get('error')}). "
            "Create an app-level token with the connections:write scope under Settings > Basic Information > "
            "App-Level Tokens, then start the app again."
        )
