"""Slack request payloads and helpers for the tests."""

import json

from slack_bolt import BoltRequest

ALLOWED_USER = "UALLOWED1"
DENIED_USER = "UDENIED01"


def _team():
    return {"id": "T0000TEST"}


def command(user):
    return {"command": "/send-to-notion", "user_id": user, "team_id": "T0000TEST", "trigger_id": "trigger"}


def shortcut(user):
    return {"type": "shortcut", "callback_id": "open-notion-form", "user": {"id": user}, "team": _team(), "trigger_id": "t"}


def home_button(user):
    return {
        "type": "block_actions",
        "user": {"id": user},
        "team": _team(),
        "trigger_id": "t",
        "view": {"type": "home", "id": "VHOME"},
        "actions": [{"action_id": "open-notion-form", "type": "button", "block_id": "b"}],
    }


def options(user):
    return {
        "type": "block_suggestion",
        "action_id": "search-notion-database",
        "block_id": "search-notion-database",
        "user": {"id": user},
        "team": _team(),
        "value": "",
    }


def property_options(user, query=""):
    return {
        "type": "block_suggestion",
        "action_id": "select",
        "block_id": "status-id",
        "user": {"id": user},
        "team": _team(),
        "value": query,
        "view": {
            "type": "modal",
            "id": "V2",
            "callback_id": "send-to-notion-database",
            "private_metadata": json.dumps({"notion_data_source_id": "ds-1"}),
        },
    }


def select_view_submission(user):
    return {
        "type": "view_submission",
        "user": {"id": user},
        "team": _team(),
        "view": {
            "type": "modal",
            "id": "V1",
            "callback_id": "select-notion-database",
            "blocks": [{"type": "input", "block_id": "search-notion-database"}],
            "state": {"values": {}},
        },
    }


def send_view_submission(user):
    return {
        "type": "view_submission",
        "user": {"id": user},
        "team": _team(),
        "view": {
            "type": "modal",
            "id": "V2",
            "callback_id": "send-to-notion-database",
            "private_metadata": json.dumps({"notion_data_source_id": "ds-1"}),
            "blocks": [{"type": "section"}, {"type": "input", "block_id": "title"}],
            "state": {"values": {"title": {"title": {"value": "Row"}}}},
        },
    }


def home_opened(user):
    return {"type": "event_callback", "team_id": "T0000TEST", "event": {"type": "app_home_opened", "user": user}}


def dispatch(app, body):
    return app.dispatch(BoltRequest(body=body, mode="socket_mode"))
