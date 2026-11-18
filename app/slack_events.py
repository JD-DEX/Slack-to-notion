import json
import logging
import os

from slack_bolt import App, Ack, Say, BoltContext
from slack_sdk import WebClient

from notion_client import APIResponseError

from app.notion_ops import (
    find_notion_data_source,
    new_notion_client,
    build_notion_database_options,
    build_input_blocks,
    send_to_notion,
    validate_submission,
)


GENERIC_ERROR_TEXT = "Sorry, something went wrong. Please try again later or contact the app's administrator."


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

    def resolve_notion_token(context: BoltContext) -> str:
        token = context.get("notion_token")
        if token is None:
            raise Exception("Unexpectedly Notion installation is not found!")
        return token

    @app.action("link-button")
    def just_ack_link_buttons(ack: Ack):
        ack()

    @app.shortcut("open-notion-form")
    @app.action("open-notion-form")
    @app.command("/send-to-notion")
    def open_notion_form(ack: Ack, client: WebClient, body: dict):
        ack()
        client.views_open(
            trigger_id=body["trigger_id"],
            view=build_database_selection_view(),
        )

    @app.action("select-notion-workspace")
    def select_notion_workspace(ack: Ack, body: dict, client: WebClient):
        ack()
        client.views_update(
            view_id=body.get("view").get("id"),
            view=build_database_selection_view(),
        )

    @app.action("go-back-to-database-select-view")
    def go_back_to_initial_modal_view(ack: Ack, client: WebClient, body: dict):
        ack()
        client.views_update(
            view_id=body["view"]["id"],
            hash=body["view"]["hash"],
            view=build_database_selection_view(),
        )

    @app.options("search-notion-database")
    def search_notion_database(ack: Ack, payload: dict, context: BoltContext, logger: logging.Logger):
        try:
            notion = new_notion_client(resolve_notion_token(context))
            options = build_notion_database_options(notion, payload)
        except Exception as e:
            log_failure(logger, "Searching Notion databases failed", e)
            options = []
        ack(options=options)

    def select_notion_database_ack(ack: Ack):
        ack(
            response_action="update",
            view={
                "type": "modal",
                "callback_id": "select-notion-database",
                "title": {"type": "plain_text", "text": "Send to Notion"},
                "close": {"type": "plain_text", "text": "Close"},
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": ":hourglass: Wait a second. Now loading the database ...",
                        },
                    }
                ],
            },
        )

    def select_notion_database_lazy(
        view: dict,
        client: WebClient,
        say: Say,
        context: BoltContext,
        logger: logging.Logger,
    ):
        try:
            block_key = "search-notion-database"
            data_source_id = view["state"]["values"][block_key][block_key]["selected_option"]["value"]
            notion = new_notion_client(resolve_notion_token(context))
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
            client.views_update(
                view_id=view["id"],
                view={
                    "type": "modal",
                    "callback_id": "send-to-notion-database",
                    "title": {"type": "plain_text", "text": "Send to Notion"},
                    "close": {"type": "plain_text", "text": "Close"},
                    "blocks": [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f":x: Loading the Notion database failed. {GENERIC_ERROR_TEXT}",
                            },
                        }
                    ],
                },
            )
            say(
                channel=context.user_id,
                text=f":x: Loading the Notion database failed. {GENERIC_ERROR_TEXT}",
            )

    app.view("select-notion-database")(
        ack=select_notion_database_ack,
        lazy=[select_notion_database_lazy],
    )

    def sent_to_notion_ack(ack: Ack, view: dict):
        errors = validate_submission(view["state"]["values"])
        if len(errors) > 0:
            ack(response_action="errors", errors=errors)
            return

        ack(
            response_action="update",
            view={
                "type": "modal",
                "callback_id": "send-to-notion-database",
                "title": {"type": "plain_text", "text": "Send to Notion"},
                "close": {"type": "plain_text", "text": "Close"},
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": ":hourglass: Wait a second. Now sending the data to Notion ...",
                        },
                    }
                ],
            },
        )

    def sent_to_notion_lazy(
        view: dict,
        body: dict,
        say: Say,
        context: BoltContext,
        client: WebClient,
        logger: logging.Logger,
    ):
        errors = validate_submission(view["state"]["values"])
        if len(errors) > 0:
            return
        try:
            data_source_id = json.loads(view["private_metadata"])["notion_data_source_id"]
            notion = new_notion_client(resolve_notion_token(context))
            new_page = send_to_notion(notion, data_source_id, view["state"]["values"])
            page_url = new_page["url"]
            client.views_update(
                view_id=view["id"],
                view={
                    "type": "modal",
                    "callback_id": "send-to-notion-database",
                    "title": {"type": "plain_text", "text": "Send to Notion"},
                    "close": {"type": "plain_text", "text": "Close"},
                    "blocks": [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f":white_check_mark: Your data has been successfully saved!\n\n{page_url}",
                            },
                        }
                    ],
                },
            )
        except Exception as e:
            log_failure(logger, "Saving the data to Notion failed", e)
            client.views_update(
                view_id=view["id"],
                view={
                    "type": "modal",
                    "callback_id": "send-to-notion-database",
                    "title": {"type": "plain_text", "text": "Send to Notion"},
                    "close": {"type": "plain_text", "text": "Close"},
                    "blocks": [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f":x: Saving your data failed. {GENERIC_ERROR_TEXT}",
                            },
                        }
                    ],
                },
            )
            say(
                channel=context.user_id,
                text=f":x: Saving your data to Notion failed. {GENERIC_ERROR_TEXT}",
            )

    app.view("send-to-notion-database")(
        ack=sent_to_notion_ack,
        lazy=[sent_to_notion_lazy],
    )
