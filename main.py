import logging
import os
import sys
from typing import Callable
from slack_bolt import App, BoltContext
from slack_bolt.adapter.socket_mode import SocketModeHandler

from app.slack_events import register_slack_event_handlers
from dotenv import load_dotenv

load_dotenv()

# These libraries log raw Slack payloads and HTTP bodies at DEBUG level.
# Payloads contain user form input, so keep them at INFO or higher at all times.
_THIRD_PARTY_LOGGERS = ("slack_bolt", "slack_sdk", "notion_client", "httpx", "httpcore", "urllib3", "websocket")


def configure_logging() -> None:
    level_name = os.environ.get("LOG_LEVEL", "INFO").strip().upper() or "INFO"
    level = logging.getLevelName(level_name)
    if not isinstance(level, int):
        sys.exit(f"Invalid LOG_LEVEL {level_name!r}. Use DEBUG, INFO, WARNING, ERROR or CRITICAL.")
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s %(message)s", level=level)
    for name in _THIRD_PARTY_LOGGERS:
        logging.getLogger(name).setLevel(max(level, logging.INFO))


slack_bot_token = os.environ.get("SLACK_BOT_TOKEN")
slack_app_level_token = os.environ.get("SLACK_APP_TOKEN")
notion_api_token = os.environ.get("NOTION_API_TOKEN")

app = App(token=slack_bot_token)


@app.middleware
def attach_notion_token(context: BoltContext, next_: Callable):
    context["notion_token"] = notion_api_token
    next_()


register_slack_event_handlers(app)

if __name__ == "__main__":
    configure_logging()
    SocketModeHandler(app, slack_app_level_token).start()
