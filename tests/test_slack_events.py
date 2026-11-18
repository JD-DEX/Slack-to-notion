"""Integration tests for the modal flows, from the Slack request to the Slack and Notion calls."""

import json

import pytest
from slack_sdk.errors import SlackApiError

from tests.helpers import ALLOWED_USER, _team, dispatch, send_view_submission


def select_database_submission(user, data_source_id="ds-1"):
    option = {"value": data_source_id}
    return {
        "type": "view_submission",
        "user": {"id": user},
        "team": _team(),
        "view": {
            "type": "modal",
            "id": "V1",
            "callback_id": "select-notion-database",
            "blocks": [{"type": "input", "block_id": "search-notion-database"}],
            "state": {"values": {"search-notion-database": {"search-notion-database": {"selected_option": option}}}},
        },
    }


def _views(slack_api):
    return [c.kwargs["view"] for c in slack_api["views_update"].call_args_list]


def _data_source():
    return {"id": "ds-1", "title": [{"plain_text": "Tasks"}], "properties": {"Name": {"id": "title", "type": "title"}}}


#
# Database selected: load the form
#


def test_selected_database_loads_the_form(app, slack_api, notion):
    notion.default_client.data_sources.retrieve.return_value = _data_source()

    resp = dispatch(app, select_database_submission(ALLOWED_USER))

    assert json.loads(resp.body)["response_action"] == "update"
    view = _views(slack_api)[-1]
    assert view["callback_id"] == "send-to-notion-database"
    assert json.loads(view["private_metadata"]) == {"notion_data_source_id": "ds-1"}
    slack_api["chat_postMessage"].assert_not_called()


def test_load_failure_shows_one_error_in_the_modal(app, slack_api, notion):
    notion.default_client.data_sources.retrieve.side_effect = RuntimeError("Notion is down")

    dispatch(app, select_database_submission(ALLOWED_USER))

    assert "Loading the Notion database failed" in json.dumps(_views(slack_api)[-1])
    slack_api["chat_postMessage"].assert_not_called()


def test_load_failure_sends_a_dm_when_the_modal_is_gone(app, slack_api, notion):
    notion.default_client.data_sources.retrieve.side_effect = RuntimeError("Notion is down")
    slack_api["views_update"].side_effect = SlackApiError("not_found", {"ok": False, "error": "not_found"})

    dispatch(app, select_database_submission(ALLOWED_USER))

    slack_api["chat_postMessage"].assert_called_once()
    kwargs = slack_api["chat_postMessage"].call_args.kwargs
    assert kwargs["channel"] == ALLOWED_USER
    assert "Loading the Notion database failed" in kwargs["text"]


def test_failure_when_slack_is_unreachable_does_not_raise(app, slack_api, notion, caplog):
    notion.default_client.data_sources.retrieve.side_effect = RuntimeError("Notion is down")
    slack_api["views_update"].side_effect = SlackApiError("not_found", {"ok": False, "error": "not_found"})
    slack_api["chat_postMessage"].side_effect = SlackApiError("not_in_channel", {"ok": False})

    resp = dispatch(app, select_database_submission(ALLOWED_USER))

    assert resp.status == 200
    assert "Could not tell the user the result" in caplog.text


#
# Form submitted: create the page
#


def test_submission_creates_the_page_and_shows_its_url(app, slack_api, notion):
    notion.default_client.pages.create.return_value = {"url": "https://www.notion.so/page"}

    dispatch(app, send_view_submission(ALLOWED_USER))

    kwargs = notion.default_client.pages.create.call_args.kwargs
    assert kwargs["parent"] == {"type": "data_source_id", "data_source_id": "ds-1"}
    assert "https://www.notion.so/page" in json.dumps(_views(slack_api)[-1])
    slack_api["chat_postMessage"].assert_not_called()


def test_saved_page_is_sent_as_a_dm_when_the_modal_is_gone(app, slack_api, notion):
    notion.default_client.pages.create.return_value = {"url": "https://www.notion.so/page"}
    slack_api["views_update"].side_effect = SlackApiError("not_found", {"ok": False, "error": "not_found"})

    dispatch(app, send_view_submission(ALLOWED_USER))

    text = slack_api["chat_postMessage"].call_args.kwargs["text"]
    assert "https://www.notion.so/page" in text
    assert "failed" not in text
    notion.default_client.pages.create.assert_called_once()


def test_invalid_submission_is_not_sent_to_notion(app, slack_api, notion):
    # Bolt starts lazy listeners also when the ack returns errors.
    body = send_view_submission(ALLOWED_USER)
    body["view"]["state"]["values"]["title"]["title"]["value"] = "   "

    resp = dispatch(app, body)

    assert json.loads(resp.body)["response_action"] == "errors"
    notion.default_client.pages.create.assert_not_called()
    slack_api["views_update"].assert_not_called()


def test_save_failure_shows_one_error_in_the_modal(app, slack_api, notion):
    notion.default_client.pages.create.side_effect = RuntimeError("Notion is down")

    dispatch(app, send_view_submission(ALLOWED_USER))

    assert "Saving your data failed" in json.dumps(_views(slack_api)[-1])
    slack_api["chat_postMessage"].assert_not_called()


def test_save_failure_sends_a_dm_when_the_modal_is_gone(app, slack_api, notion):
    notion.default_client.pages.create.side_effect = RuntimeError("Notion is down")
    slack_api["views_update"].side_effect = SlackApiError("not_found", {"ok": False, "error": "not_found"})

    dispatch(app, send_view_submission(ALLOWED_USER))

    assert "Saving your data failed" in slack_api["chat_postMessage"].call_args.kwargs["text"]


#
# Removed handlers
#


@pytest.mark.parametrize("action_id", ["link-button", "select-notion-workspace"])
def test_unused_actions_have_no_handler(app, slack_api, action_id):
    body = {
        "type": "block_actions",
        "user": {"id": ALLOWED_USER},
        "team": _team(),
        "trigger_id": "t",
        "view": {"type": "modal", "id": "V1", "hash": "h"},
        "actions": [{"action_id": action_id, "type": "button", "block_id": "b"}],
    }
    assert dispatch(app, body).status == 404
