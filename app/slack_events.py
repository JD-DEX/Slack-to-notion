import json
import logging
from typing import Optional

from slack_bolt import App, Ack, BoltContext
from slack_sdk import WebClient

from notion_client import APIResponseError

from app.notion_ops import (
    NotionClients,
    find_notion_data_source,
    build_notion_database_options,
    build_property_options,
    build_input_blocks,
    send_to_notion,
    validate_submission,
)

GENERIC_ERROR_TEXT = "Sorry, something went wrong. Please try again later or contact the app's administrator."
MODAL_TITLE = {"type": "plain_text", "text": "Send to Notion"}
MODAL_CLOSE = {"type": "plain_text", "text": "Close"}
SEND_CALLBACK_ID = "send-to-notion-database"
SELECT_CALLBACK_ID = "select-notion-database"


def log_failure(logger: logging.Logger, message: str, e: Exception) -> None:
    """Log a failure without user form values.

    Notion validation messages can repeat the submitted values, so for Notion API
    errors log only the HTTP status, error code and request ID.
    """
    if isinstance(e, APIResponseError):
        logger.error(
            "%s: Notion API error (status: %s, code: %s, request_id: %s)",
            message,
            getattr(e, "status", None),
            getattr(e, "code", None),
            getattr(e, "request_id", None),
        )
    else:
        logger.exception(message)


def status_modal(text: str, callback_id: str) -> dict:
    """Return a modal that shows one message and has only a Close button."""
    return {
        "type": "modal",
        "callback_id": callback_id,
        "title": MODAL_TITLE,
        "close": MODAL_CLOSE,
        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
    }


def show_result(client: WebClient, view_id: str, user_id: Optional[str], text: str, logger: logging.Logger) -> None:
    """Show the result in the modal. Send a DM only when the modal cannot be updated.

    The user can close the modal before the lazy listener ends, so the update can fail.
    """
    try:
        client.views_update(view_id=view_id, view=status_modal(text, SEND_CALLBACK_ID))
        return
    except Exception as e:
        logger.warning("Could not show the result in the modal (%s). Sending a DM.", type(e).__name__)
    if not user_id:
        logger.error("Could not tell the user the result (no user ID)")
        return
    try:
        client.chat_postMessage(channel=user_id, text=text)
    except Exception as e:
        logger.error("Could not tell the user the result (%s)", type(e).__name__)


def register_slack_event_handlers(app: App, notion: NotionClients):
    #
    # Home tab
    #

    @app.event("app_home_opened")
    def update_home_tab(client: WebClient, context: BoltContext):
        client.views_publish(
            user_id=context.user_id,
            view={
                "type": "home",
                "blocks": [
                    {
                        "type": "section",
                        "block_id": "header",
                        "text": {"type": "mrkdwn", "text": "*Welcome to Send to Notion in Slack* :raised_hands:"},
                    },
                    {
                        "type": "actions",
                        "block_id": "open-modal-button",
                        "elements": [
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": "Submit data to Notion"},
                                "value": "clicked",
                                "action_id": "open-notion-form",
                            }
                        ],
                    },
                ],
            },
        )

    #
    # Send to Notion
    #

    def build_database_selection_view():
        return {
            "type": "modal",
            "callback_id": SELECT_CALLBACK_ID,
            "title": MODAL_TITLE,
            "submit": {"type": "plain_text", "text": "Next"},
            "close": MODAL_CLOSE,
            "blocks": [
                {
                    "type": "input",
                    "block_id": "search-notion-database",
                    "element": {
                        "type": "external_select",
                        "placeholder": {"type": "plain_text", "text": "Find a Notion database"},
                        "action_id": "search-notion-database",
                        "min_query_length": 0,
                    },
                    "label": {"type": "plain_text", "text": "Notion Database"},
                }
            ],
        }

    @app.shortcut("open-notion-form")
    @app.action("open-notion-form")
    @app.command("/send-to-notion")
    def open_notion_form(ack: Ack, client: WebClient, body: dict):
        ack()
        client.views_open(trigger_id=body["trigger_id"], view=build_database_selection_view())

    @app.action("go-back-to-database-select-view")
    def go_back_to_initial_modal_view(ack: Ack, client: WebClient, body: dict):
        ack()
        client.views_update(
            view_id=body["view"]["id"],
            hash=body["view"]["hash"],
            view=build_database_selection_view(),
        )

    @app.options("search-notion-database")
    def search_notion_database(ack: Ack, payload: dict, logger: logging.Logger):
        try:
            options = build_notion_database_options(notion.search, payload)
        except Exception as e:
            log_failure(logger, "Searching Notion databases failed", e)
            options = []
        ack(options=options)

    # A select or multi_select property with more than 100 options uses an external select menu.
    # Its action ID is the property type and its block ID is the property ID.
    @app.options("select")
    @app.options("multi_select")
    def search_property_options(ack: Ack, payload: dict, body: dict, logger: logging.Logger):
        try:
            data_source_id = json.loads(body["view"]["private_metadata"])["notion_data_source_id"]
            data_source = find_notion_data_source(notion.search, data_source_id)
            options = build_property_options(data_source, payload["block_id"], payload.get("value", ""))
        except Exception as e:
            log_failure(logger, "Searching the property options failed", e)
            options = []
        ack(options=options)

    def select_notion_database_ack(ack: Ack):
        ack(
            response_action="update",
            view=status_modal(":hourglass: Wait a second. Now loading the database ...", SELECT_CALLBACK_ID),
        )

    def select_notion_database_lazy(view: dict, client: WebClient, context: BoltContext, logger: logging.Logger):
        try:
            block_key = "search-notion-database"
            data_source_id = view["state"]["values"][block_key][block_key]["selected_option"]["value"]
            data_source = find_notion_data_source(notion.default, data_source_id)
            client.views_update(
                view_id=view["id"],
                view={
                    "type": "modal",
                    "callback_id": SEND_CALLBACK_ID,
                    "title": MODAL_TITLE,
                    "submit": {"type": "plain_text", "text": "Submit"},
                    "close": MODAL_CLOSE,
                    "private_metadata": json.dumps({"notion_data_source_id": data_source_id}),
                    "blocks": build_input_blocks(data_source),
                },
            )
        except Exception as e:
            log_failure(logger, "Loading the Notion database failed", e)
            text = f":x: Loading the Notion database failed. {GENERIC_ERROR_TEXT}"
            show_result(client, view["id"], context.user_id, text, logger)

    app.view(SELECT_CALLBACK_ID)(ack=select_notion_database_ack, lazy=[select_notion_database_lazy])

    def sent_to_notion_ack(ack: Ack, view: dict):
        errors = validate_submission(view["state"]["values"])
        if len(errors) > 0:
            ack(response_action="errors", errors=errors)
            return
        ack(
            response_action="update",
            view=status_modal(":hourglass: Wait a second. Now sending the data to Notion ...", SEND_CALLBACK_ID),
        )

    def sent_to_notion_lazy(view: dict, context: BoltContext, client: WebClient, logger: logging.Logger):
        # Bolt starts lazy listeners also when the ack returned errors, so check the values again.
        if validate_submission(view["state"]["values"]):
            return
        try:
            data_source_id = json.loads(view["private_metadata"])["notion_data_source_id"]
            new_page = send_to_notion(notion.default, data_source_id, view["state"]["values"])
            text = f":white_check_mark: Your data has been successfully saved!\n\n{new_page['url']}"
        except Exception as e:
            log_failure(logger, "Saving the data to Notion failed", e)
            text = f":x: Saving your data failed. {GENERIC_ERROR_TEXT}"
        # Outside the try block: a failed modal update must not report a saved page as failed.
        show_result(client, view["id"], context.user_id, text, logger)

    app.view(SEND_CALLBACK_ID)(ack=sent_to_notion_ack, lazy=[sent_to_notion_lazy])
