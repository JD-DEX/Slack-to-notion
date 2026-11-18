import json
import logging
from typing import Optional

import httpx
from slack_bolt import App, Ack, BoltContext
from slack_sdk import WebClient

from notion_client import APIResponseError
from notion_client.errors import HTTPResponseError, RequestTimeoutError

from app.notion_ops import (
    find_notion_data_source,
    new_notion_client,
    build_notion_database_options,
    build_input_blocks,
    send_to_notion,
    validate_submission,
)


# Option values that are not data source IDs. Slack cannot show a message in an
# external select, so these options tell the user why the list has no databases.
SEARCH_FAILED_VALUE = "search-failed"
NO_DATABASES_VALUE = "no-databases"
SEARCH_FAILED_OPTION = "Notion search did not work. Type again to retry."
NO_DATABASES_OPTION = "No database found. Share it with this app in Notion."
NOTICE_OPTION_ERRORS = {
    SEARCH_FAILED_VALUE: "Notion did not answer the search. Type in the box to search again. "
    "If the list still shows this message, wait a few minutes and try again.",
    NO_DATABASES_VALUE: "No Notion database matches your search. Type a different name. "
    "If the database is still not listed, ask its owner to add this app under Connections in Notion.",
}

LOAD_FAILED_TEXT = ":x: The Notion database did not load."
SAVE_FAILED_TEXT = ":x: Your data was not saved to Notion."
OPEN_FAILED_TEXT = ":x: The Send to Notion form did not open. Try again."
GO_BACK_FAILED_TEXT = ":x: The database list did not open. Close the form, then open it again."
UNEXPECTED_ERROR_TEXT = (
    "Something unexpected went wrong. Try again. "
    "If the problem continues, ask the app's administrator to check the app logs."
)
NOT_RESPONDING_TEXT = "Notion did not respond. Wait a few minutes, then try again."
NOTION_ERROR_TEXTS = {
    "object_not_found": "Notion cannot find this database. It was deleted, or it is no longer shared with this app. "
    "Select a different database, or ask the database owner to add this app under Connections in Notion.",
    "restricted_resource": "This app does not have permission to use this database. "
    "Ask the app's administrator to give the Notion integration the Read content and Insert content capabilities.",
    "unauthorized": "Notion did not accept this app's integration token. "
    "Ask the app's administrator to update the Notion token.",
    "rate_limited": "Notion received too many requests. Wait one minute, then try again.",
    "validation_error": "Notion did not accept the request. The database may have changed. "
    "Open the form again to get its current fields, then try again.",
    "conflict_error": "Notion was changing the database at the same time. Try again.",
    "internal_server_error": NOT_RESPONDING_TEXT,
    "service_unavailable": NOT_RESPONDING_TEXT,
    "gateway_timeout": NOT_RESPONDING_TEXT,
}


def notion_error_text(e: Exception) -> str:
    """Return a plain-language reason and recovery step for a failed Notion call.

    Never show the exception text. Notion messages can contain internal IDs and form values.
    """
    if isinstance(e, HTTPResponseError):
        if str(e.code) in NOTION_ERROR_TEXTS:
            return NOTION_ERROR_TEXTS[str(e.code)]
        # An unknown code with a 5xx status is a Notion outage, for example an HTML 502 page.
        return NOT_RESPONDING_TEXT if (e.status or 0) >= 500 else UNEXPECTED_ERROR_TEXT
    # notion-client converts timeouts to RequestTimeoutError, but not connection errors.
    if isinstance(e, (RequestTimeoutError, httpx.TransportError)):
        return NOT_RESPONDING_TEXT
    return UNEXPECTED_ERROR_TEXT


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


def notify_user(client: WebClient, user_id: Optional[str], text: str, logger: logging.Logger) -> None:
    """Send a direct message. Use it when the modal cannot show the message."""
    try:
        client.chat_postMessage(channel=user_id, text=text)
    except Exception as e:
        logger.warning("Could not send a direct message to the user (%s)", type(e).__name__)


def show_result(
    client: WebClient, view_id: str, user_id: Optional[str], text: str, logger: logging.Logger, also_notify: bool
) -> None:
    """Show the result in the modal. Send a direct message when also_notify is True or the modal is closed."""
    try:
        client.views_update(view_id=view_id, view=result_view(text))
    except Exception as e:
        # For example, the user closed the modal before the result was ready.
        logger.warning("Could not update the modal with the result (%s)", type(e).__name__)
        also_notify = True
    if also_notify:
        notify_user(client, user_id, text, logger)


def result_view(text: str) -> dict:
    return {
        "type": "modal",
        "callback_id": "send-to-notion-database",
        "title": {"type": "plain_text", "text": "Send to Notion"},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
    }


def register_slack_event_handlers(app: App):
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
        blocks = [
            {
                "type": "input",
                "block_id": "search-notion-database",
                "element": {
                    "type": "external_select",
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Find a Notion database",
                    },
                    "action_id": "search-notion-database",
                    "min_query_length": 0,
                },
                "label": {"type": "plain_text", "text": "Notion Database"},
            }
        ]

        return {
            "type": "modal",
            "callback_id": "select-notion-database",
            "title": {"type": "plain_text", "text": "Send to Notion"},
            "submit": {"type": "plain_text", "text": "Next"},
            "close": {"type": "plain_text", "text": "Close"},
            "blocks": blocks,
        }

    @app.action("link-button")
    def just_ack_link_buttons(ack: Ack):
        ack()

    @app.shortcut("open-notion-form")
    @app.action("open-notion-form")
    @app.command("/send-to-notion")
    def open_notion_form(ack: Ack, client: WebClient, body: dict, context: BoltContext, logger: logging.Logger):
        ack()
        try:
            client.views_open(
                trigger_id=body["trigger_id"],
                view=build_database_selection_view(),
            )
        except Exception as e:
            # For example, the trigger ID expired because Slack took more than 3 seconds to deliver the request.
            logger.warning("Opening the form failed (%s)", type(e).__name__)
            notify_user(client, context.user_id, OPEN_FAILED_TEXT, logger)

    @app.action("select-notion-workspace")
    def select_notion_workspace(ack: Ack, body: dict, client: WebClient):
        ack()
        client.views_update(
            view_id=body.get("view").get("id"),
            view=build_database_selection_view(),
        )

    @app.action("go-back-to-database-select-view")
    def go_back_to_initial_modal_view(ack: Ack, client: WebClient, body: dict, context: BoltContext, logger: logging.Logger):
        ack()
        try:
            client.views_update(
                view_id=body["view"]["id"],
                hash=body["view"]["hash"],
                view=build_database_selection_view(),
            )
        except Exception as e:
            logger.warning("Going back to the database list failed (%s)", type(e).__name__)
            notify_user(client, context.user_id, GO_BACK_FAILED_TEXT, logger)

    @app.options("search-notion-database")
    def search_notion_database(ack: Ack, payload: dict, context: BoltContext, logger: logging.Logger):
        try:
            notion = new_notion_client(context["notion_token"])
            options = build_notion_database_options(notion, payload)
            if not options:
                options = [notice_option(NO_DATABASES_OPTION, NO_DATABASES_VALUE)]
        except Exception as e:
            log_failure(logger, "Searching Notion databases failed", e)
            options = [notice_option(SEARCH_FAILED_OPTION, SEARCH_FAILED_VALUE)]
        ack(options=options)

    def notice_option(text: str, value: str) -> dict:
        return {"text": {"type": "plain_text", "text": text}, "value": value}

    def select_notion_database_ack(ack: Ack, view: dict):
        block_key = "search-notion-database"
        selected = view["state"]["values"].get(block_key, {}).get(block_key, {}).get("selected_option") or {}
        if selected.get("value") in NOTICE_OPTION_ERRORS:
            ack(response_action="errors", errors={block_key: NOTICE_OPTION_ERRORS[selected["value"]]})
            return
        ack(response_action="update", view=result_view(":hourglass: Wait a second. Now loading the database ..."))

    def select_notion_database_lazy(
        view: dict,
        client: WebClient,
        context: BoltContext,
        logger: logging.Logger,
    ):
        try:
            block_key = "search-notion-database"
            data_source_id = view["state"]["values"][block_key][block_key]["selected_option"]["value"]
            if data_source_id in NOTICE_OPTION_ERRORS:
                # The ack listener already showed a field error.
                return
            notion = new_notion_client(context["notion_token"])
            data_source = find_notion_data_source(notion, data_source_id)
            blocks = build_input_blocks(data_source)
            client.views_update(
                view_id=view["id"],
                view={
                    "type": "modal",
                    "callback_id": "send-to-notion-database",
                    "title": {"type": "plain_text", "text": "Send to Notion"},
                    "submit": {"type": "plain_text", "text": "Submit"},
                    "close": {"type": "plain_text", "text": "Close"},
                    "private_metadata": json.dumps({"notion_data_source_id": data_source_id}),
                    "blocks": blocks,
                },
            )
        except Exception as e:
            log_failure(logger, "Loading the Notion database failed", e)
            text = f"{LOAD_FAILED_TEXT} {notion_error_text(e)}"
            show_result(client, view["id"], context.user_id, text, logger, also_notify=True)

    app.view("select-notion-database")(
        ack=select_notion_database_ack,
        lazy=[select_notion_database_lazy],
    )

    def sent_to_notion_ack(ack: Ack, view: dict):
        errors = validate_submission(view["state"]["values"])
        if len(errors) > 0:
            ack(response_action="errors", errors=errors)
            return

        ack(response_action="update", view=result_view(":hourglass: Wait a second. Now sending the data to Notion ..."))

    def sent_to_notion_lazy(
        view: dict,
        context: BoltContext,
        client: WebClient,
        logger: logging.Logger,
    ):
        errors = validate_submission(view["state"]["values"])
        if len(errors) > 0:
            return
        try:
            data_source_id = json.loads(view["private_metadata"])["notion_data_source_id"]
            notion = new_notion_client(context["notion_token"])
            new_page = send_to_notion(notion, data_source_id, view["state"]["values"])
        except Exception as e:
            log_failure(logger, "Saving the data to Notion failed", e)
            text = f"{SAVE_FAILED_TEXT} {notion_error_text(e)}"
            show_result(client, view["id"], context.user_id, text, logger, also_notify=True)
            return
        # The page exists now. If the modal is closed, the user gets the link in a direct message.
        text = f":white_check_mark: Your data is saved in Notion.\n\n{new_page.get('url', '')}".rstrip()
        show_result(client, view["id"], context.user_id, text, logger, also_notify=False)

    app.view("send-to-notion-database")(
        ack=sent_to_notion_ack,
        lazy=[sent_to_notion_lazy],
    )
