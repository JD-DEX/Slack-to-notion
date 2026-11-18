import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest
from slack_bolt import App, BoltRequest
from slack_bolt.authorization import AuthorizeResult

from app.access_control import DENIED_MESSAGE, build_allowlist_middleware, parse_allowed_user_ids
from app.slack_events import register_slack_event_handlers

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ALLOWED_USER = "UALLOWED1"
DENIED_USER = "UDENIED01"


def _authorize(**kwargs):
    # No real token. The Slack client is mocked in every test that makes a call.
    return AuthorizeResult(enterprise_id=None, team_id="T0000TEST", bot_token="test-token", bot_user_id="UBOT", bot_id="B1")


@pytest.fixture
def app():
    # process_before_response=True runs listeners in the request thread, so the tests are deterministic.
    app = App(
        signing_secret="test",
        authorize=_authorize,
        request_verification_enabled=False,
        process_before_response=True,
    )
    app.use(build_allowlist_middleware(frozenset({ALLOWED_USER})))

    @app.middleware
    def attach_notion_token(context, next):
        context["notion_token"] = "test-notion-token"
        next()

    register_slack_event_handlers(app)
    return app


@pytest.fixture
def slack_api():
    with mock.patch("slack_sdk.WebClient.views_open") as views_open, mock.patch(
        "slack_sdk.WebClient.views_publish"
    ) as views_publish, mock.patch("slack_sdk.WebClient.views_update") as views_update:
        yield {"views_open": views_open, "views_publish": views_publish, "views_update": views_update}


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


def test_main_exits_when_allowlist_is_empty():
    result = _run_main({"ALLOWED_SLACK_USER_IDS": "", "SLACK_BOT_TOKEN": "x", "SLACK_APP_TOKEN": "x"})
    assert result.returncode == 1
    assert "ALLOWED_SLACK_USER_IDS" in result.stderr


@pytest.mark.skipif((PROJECT_ROOT / ".env").exists(), reason="a local .env file could set the variable")
def test_main_exits_when_allowlist_is_unset():
    result = _run_main({"SLACK_BOT_TOKEN": "x", "SLACK_APP_TOKEN": "x"})
    assert result.returncode == 1
    assert "ALLOWED_SLACK_USER_IDS" in result.stderr


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


@pytest.mark.parametrize("payload, block_id", [(select_view_submission, "search-notion-database"), (send_view_submission, "title")])
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


def test_allowed_options_reach_the_handler(app, slack_api):
    fake_options = [{"text": {"type": "plain_text", "text": "Tasks"}, "value": "ds-1"}]
    with mock.patch("app.slack_events.build_notion_database_options", return_value=fake_options), mock.patch(
        "app.slack_events.new_notion_client"
    ):
        resp = dispatch(app, options(ALLOWED_USER))
    assert json.loads(resp.body) == {"options": fake_options}


def test_allowed_home_opened_publishes_home_tab(app, slack_api):
    dispatch(app, home_opened(ALLOWED_USER))
    slack_api["views_publish"].assert_called_once()
