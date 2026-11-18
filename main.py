import logging
import os
import sys
from typing import FrozenSet

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from app.access_control import ALLOWED_USERS_ENV_VAR, build_allowlist_middleware, parse_allowed_user_ids
from app.notion_ops import NotionClients, new_notion_clients
from app.slack_events import register_slack_event_handlers

# These libraries log raw Slack payloads and HTTP bodies at DEBUG level.
# Payloads contain user form input, so keep them at INFO or higher at all times.
_THIRD_PARTY_LOGGERS = ("slack_bolt", "slack_sdk", "notion_client", "httpx", "httpcore", "urllib3", "websocket")

REQUIRED_TOKEN_ENV_VARS = ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "NOTION_API_TOKEN")


def configure_logging() -> None:
    level_name = os.environ.get("LOG_LEVEL", "INFO").strip().upper() or "INFO"
    level = logging.getLevelName(level_name)
    if not isinstance(level, int):
        sys.exit(f"Invalid LOG_LEVEL {level_name!r}. Use DEBUG, INFO, WARNING, ERROR or CRITICAL.")
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s %(message)s", level=level)
    for name in _THIRD_PARTY_LOGGERS:
        logging.getLogger(name).setLevel(max(level, logging.INFO))


def build_app(allowed_user_ids: FrozenSet[str], notion: NotionClients, **app_kwargs) -> App:
    """Build the Bolt app. app_kwargs go to App (for example token=...)."""
    app = App(**app_kwargs)
    # Register the allowlist first so that it runs before all other middleware and listeners.
    app.use(build_allowlist_middleware(allowed_user_ids))
    register_slack_event_handlers(app, notion)
    return app


def main() -> None:
    load_dotenv()
    configure_logging()

    # Fail closed: refuse to start without the tokens and the allowlist.
    tokens = {name: os.environ.get(name, "").strip() for name in REQUIRED_TOKEN_ENV_VARS}
    missing = [name for name, value in tokens.items() if not value]
    allowed_user_ids = parse_allowed_user_ids(os.environ.get(ALLOWED_USERS_ENV_VAR))
    if not allowed_user_ids:
        missing.append(ALLOWED_USERS_ENV_VAR)
    if missing:
        sys.exit(
            f"Startup failed. These variables are empty or not set: {', '.join(missing)}. "
            f"Set every token. Set {ALLOWED_USERS_ENV_VAR} to a comma-separated list of Slack user IDs "
            "(for example U01234ABCDE,U05678FGHIJ)."
        )

    app = build_app(allowed_user_ids, new_notion_clients(tokens["NOTION_API_TOKEN"]), token=tokens["SLACK_BOT_TOKEN"])
    SocketModeHandler(app, tokens["SLACK_APP_TOKEN"]).start()


if __name__ == "__main__":
    main()
