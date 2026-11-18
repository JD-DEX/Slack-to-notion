import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

from app.access_control import DENIED_MESSAGE, build_allowlist_middleware, parse_allowed_user_ids
from tests.helpers import (
    ALLOWED_USER,
    DENIED_USER,
    command,
    dispatch,
    home_button,
    home_opened,
    options,
    property_options,
    select_view_submission,
    send_view_submission,
    shortcut,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


#
# Parsing and startup
#


def test_parse_allowed_user_ids():
    assert parse_allowed_user_ids(" U1, U2 ,,U3 ") == frozenset({"U1", "U2", "U3"})
    assert parse_allowed_user_ids("") == frozenset()
    assert parse_allowed_user_ids(" , ") == frozenset()
    assert parse_allowed_user_ids(None) == frozenset()


def test_middleware_refuses_empty_allowlist():
    with pytest.raises(ValueError):
        build_allowlist_middleware(frozenset())


def _run_main(env):
    base = {k: v for k, v in os.environ.items() if k in ("PATH", "SYSTEMROOT", "HOME")}
    return subprocess.run(
        [sys.executable, "main.py"], cwd=PROJECT_ROOT, env={**base, **env}, capture_output=True, text=True, timeout=30
    )


ALL_TOKENS = {"SLACK_BOT_TOKEN": "x", "SLACK_APP_TOKEN": "x", "NOTION_API_TOKEN": "x"}


def test_main_exits_when_allowlist_is_empty():
    result = _run_main({**ALL_TOKENS, "ALLOWED_SLACK_USER_IDS": ""})
    assert result.returncode == 1
    assert "ALLOWED_SLACK_USER_IDS" in result.stderr


@pytest.mark.skipif((PROJECT_ROOT / ".env").exists(), reason="a local .env file could set the variable")
def test_main_exits_when_allowlist_is_unset():
    result = _run_main(ALL_TOKENS)
    assert result.returncode == 1
    assert "ALLOWED_SLACK_USER_IDS" in result.stderr


@pytest.mark.skipif((PROJECT_ROOT / ".env").exists(), reason="a local .env file could set the variable")
@pytest.mark.parametrize("missing", ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "NOTION_API_TOKEN"])
def test_main_exits_when_a_token_is_unset(missing):
    env = {**ALL_TOKENS, "ALLOWED_SLACK_USER_IDS": "U1"}
    del env[missing]
    result = _run_main(env)
    assert result.returncode == 1
    assert missing in result.stderr


@pytest.mark.skipif((PROJECT_ROOT / ".env").exists(), reason="a local .env file could set the variable")
def test_main_exits_when_a_token_is_blank():
    result = _run_main({**ALL_TOKENS, "ALLOWED_SLACK_USER_IDS": "U1", "NOTION_API_TOKEN": "  "})
    assert result.returncode == 1
    assert "NOTION_API_TOKEN" in result.stderr


#
# Denied user: every entry point
#


def test_denied_command_gets_ephemeral_text(app, slack_api):
    resp = dispatch(app, command(DENIED_USER))
    assert resp.status == 200
    assert resp.body == DENIED_MESSAGE
    slack_api["views_open"].assert_not_called()


def test_denied_shortcut_is_acked_and_sees_denied_modal(app, slack_api):
    resp = dispatch(app, shortcut(DENIED_USER))
    assert resp.status == 200
    assert resp.body == ""
    slack_api["views_open"].assert_called_once()
    view = slack_api["views_open"].call_args.kwargs["view"]
    assert DENIED_MESSAGE in json.dumps(view)


def test_denied_home_button_is_acked_and_sees_denied_modal(app, slack_api):
    resp = dispatch(app, home_button(DENIED_USER))
    assert resp.status == 200
    view = slack_api["views_open"].call_args.kwargs["view"]
    assert DENIED_MESSAGE in json.dumps(view)


def test_denied_options_get_empty_list(app, slack_api):
    with mock.patch("app.slack_events.build_notion_database_options") as build_options:
        resp = dispatch(app, options(DENIED_USER))
    assert resp.status == 200
    assert json.loads(resp.body) == {"options": []}
    build_options.assert_not_called()


@pytest.mark.parametrize(
    "payload, block_id", [(select_view_submission, "search-notion-database"), (send_view_submission, "title")]
)
def test_denied_view_submission_gets_errors(app, slack_api, payload, block_id):
    with mock.patch("app.slack_events.send_to_notion") as send:
        resp = dispatch(app, payload(DENIED_USER))
    assert resp.status == 200
    assert json.loads(resp.body) == {"response_action": "errors", "errors": {block_id: DENIED_MESSAGE}}
    send.assert_not_called()


def test_denied_home_opened_does_not_publish(app, slack_api):
    resp = dispatch(app, home_opened(DENIED_USER))
    assert resp.status == 200
    slack_api["views_publish"].assert_not_called()


def test_request_without_user_is_denied(app, slack_api):
    body = command(DENIED_USER)
    del body["user_id"]
    resp = dispatch(app, body)
    assert resp.body == DENIED_MESSAGE


#
# Allowed user
#


def test_allowed_command_opens_form(app, slack_api):
    resp = dispatch(app, command(ALLOWED_USER))
    assert resp.status == 200
    assert resp.body == ""
    view = slack_api["views_open"].call_args.kwargs["view"]
    assert view["callback_id"] == "select-notion-database"


def test_allowed_options_reach_the_handler(app, slack_api, notion):
    fake_options = [{"text": {"type": "plain_text", "text": "Tasks"}, "value": "ds-1"}]
    with mock.patch("app.slack_events.build_notion_database_options", return_value=fake_options) as build_options:
        resp = dispatch(app, options(ALLOWED_USER))
    assert json.loads(resp.body) == {"options": fake_options}
    assert build_options.call_args.args[0] is notion.search_client


def test_allowed_home_opened_publishes_home_tab(app, slack_api):
    dispatch(app, home_opened(ALLOWED_USER))
    slack_api["views_publish"].assert_called_once()


def _status_data_source(count):
    options = [{"id": f"opt-{i}", "name": f"Status {i}"} for i in range(count)]
    status = {"id": "status-id", "type": "select", "select": {"options": options}}
    return {"id": "ds-1", "properties": {"Name": {"id": "title", "type": "title"}, "Status": status}}


def test_allowed_property_options_come_from_the_data_source(app, slack_api, notion):
    notion.search_client.data_sources.retrieve.return_value = _status_data_source(150)

    resp = dispatch(app, property_options(ALLOWED_USER, "status 14"))

    values = [o["value"] for o in json.loads(resp.body)["options"]]
    assert values == ["opt-14"] + [f"opt-{i}" for i in range(140, 150)]
    notion.search_client.data_sources.retrieve.assert_called_once_with(data_source_id="ds-1")


def test_property_options_are_empty_when_notion_fails(app, slack_api, notion):
    notion.search_client.data_sources.retrieve.side_effect = RuntimeError("Notion is down")

    resp = dispatch(app, property_options(ALLOWED_USER))

    assert json.loads(resp.body) == {"options": []}


def test_denied_property_options_get_empty_list(app, slack_api, notion):
    resp = dispatch(app, property_options(DENIED_USER))

    assert json.loads(resp.body) == {"options": []}
    notion.search_client.data_sources.retrieve.assert_not_called()
