"""Allowlist of Slack users who can use this app.

The Notion integration token can read and write every database shared with it.
Without this check, any member of the Slack workspace could use those rights.
"""

import logging
from typing import Callable, FrozenSet, Optional

from slack_bolt import Ack, BoltContext, BoltResponse
from slack_bolt.request.payload_utils import (
    is_action,
    is_options,
    is_shortcut,
    is_slash_command,
    is_view_submission,
)
from slack_sdk import WebClient

ALLOWED_USERS_ENV_VAR = "ALLOWED_SLACK_USER_IDS"
DENIED_MESSAGE = "Sorry, you do not have access to this app. Ask the app's administrator for access."


def parse_allowed_user_ids(raw: Optional[str]) -> FrozenSet[str]:
    """Parse a comma-separated list of Slack user IDs. Empty items are ignored."""
    if raw is None:
        return frozenset()
    return frozenset(item.strip() for item in raw.split(",") if item.strip())


def _first_input_block_id(view: dict) -> Optional[str]:
    for block in view.get("blocks", []):
        if block.get("type") == "input" and block.get("block_id"):
            return block["block_id"]
    return None


def _denied_modal() -> dict:
    return {
        "type": "modal",
        "title": {"type": "plain_text", "text": "Send to Notion"},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": [{"type": "section", "text": {"type": "plain_text", "text": DENIED_MESSAGE}}],
    }


def _deny(ack: Ack, body: dict, client: WebClient, logger: logging.Logger) -> BoltResponse:
    """Send the correct acknowledgement for each payload type.

    Returning the ack() response without calling next() stops Bolt from running any listener.
    """
    if is_slash_command(body):
        # The text of a slash command ack is shown only to the user who ran the command.
        return ack(text=DENIED_MESSAGE)
    if is_options(body):
        return ack(options=[])
    if is_view_submission(body):
        view = body.get("view", {})
        block_id = _first_input_block_id(view)
        if block_id is not None:
            return ack(response_action="errors", errors={block_id: DENIED_MESSAGE})
        return ack(response_action="update", view=_denied_modal())
    if is_shortcut(body) or is_action(body):
        response = ack()
        # Shortcuts and the Home tab button have no other place to show a message.
        # Buttons inside a modal are skipped: a denied user cannot reach them.
        trigger_id = body.get("trigger_id")
        from_home_tab = body.get("view", {}).get("type") == "home"
        if trigger_id and (is_shortcut(body) or from_home_tab):
            try:
                client.views_open(trigger_id=trigger_id, view=_denied_modal())
            except Exception as e:
                logger.warning("Could not open the access denied modal (%s)", type(e).__name__)
        return response
    # Events (such as app_home_opened), view_closed and anything else: ack and do nothing.
    return ack()


def build_allowlist_middleware(allowed_user_ids: FrozenSet[str]) -> Callable:
    """Return a global Bolt middleware that admits only the given Slack user IDs."""
    if not allowed_user_ids:
        # Fail closed. An empty allowlist must never mean "allow everyone".
        raise ValueError(f"{ALLOWED_USERS_ENV_VAR} must contain at least one Slack user ID")

    def allowlist_middleware(
        ack: Ack,
        body: dict,
        client: WebClient,
        context: BoltContext,
        logger: logging.Logger,
        next: Callable[[], None],
    ):
        user_id = context.user_id
        if user_id is not None and user_id in allowed_user_ids:
            return next()
        # Log only the user ID and payload type. Never log the body.
        logger.warning("Denied request from Slack user %s (payload type: %s)", user_id, body.get("type", "command"))
        return _deny(ack, body, client, logger)

    return allowlist_middleware
