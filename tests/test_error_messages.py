"""Every user-visible error state: the exact message and where Slack shows it."""

import json
import os
import runpy
import subprocess
import sys
import urllib.error
from concurrent.futures import Executor, Future
from pathlib import Path
from unittest import mock

import httpx
import pytest
from notion_client import APIResponseError
from notion_client.errors import RequestTimeoutError, UnknownHTTPResponseError
from slack_bolt import App, BoltRequest
from slack_bolt.authorization import AuthorizeResult
from slack_sdk.errors import SlackApiError
from slack_sdk.web import SlackResponse

from app import slack_events
from app.access_control import DENIED_MESSAGE, build_allowlist_middleware
from app.notion_ops import EMAIL_ERROR, NUMBER_ERROR, TITLE_ERROR, URL_ERROR, validate_submission
from app.slack_events import register_slack_event_handlers

PROJECT_ROOT = Path(__file__).resolve().parent.parent
USER = "UALLOWED1"
VALID_ENV = {
    "SLACK_BOT_TOKEN": "xoxb-test",
    "SLACK_APP_TOKEN": "xapp-test",
    "NOTION_API_TOKEN": "secret-test",
    "ALLOWED_SLACK_USER_IDS": USER,
}
needs_no_dotenv = pytest.mark.skipif((PROJECT_ROOT / ".env").exists(), reason="a local .env file could set variables")


class _SyncExecutor(Executor):
    """Run lazy listeners in the request thread, so the tests are deterministic."""

    def submit(self, fn, *args, **kwargs):
        future = Future()
        future.set_result(fn(*args, **kwargs))
        return future


def _authorize(**kwargs):
    return AuthorizeResult(enterprise_id=None, team_id="T0000TEST", bot_token="test-token", bot_user_id="UBOT", bot_id="B1")


@pytest.fixture
def app():
    app = App(
        signing_secret="test",
        authorize=_authorize,
        request_verification_enabled=False,
        process_before_response=True,
        listener_executor=_SyncExecutor(),
    )
    app.use(build_allowlist_middleware(frozenset({USER})))

    @app.middleware
    def attach_notion_token(context, next):
        context["notion_token"] = "test-notion-token"
        next()

    register_slack_event_handlers(app)
    return app


@pytest.fixture
def slack_api():
    names = ("views_open", "views_publish", "views_update", "chat_postMessage")
    patches = [mock.patch(f"slack_sdk.WebClient.{name}") for name in names]
    mocks = [p.start() for p in patches]
    yield dict(zip(names, mocks))
    for p in patches:
        p.stop()


def dispatch(app, body):
    return app.dispatch(BoltRequest(body=body, mode="socket_mode"))


def _slack_error(error):
    response = SlackResponse(
        client=None, http_verb="POST", api_url="u", req_args={}, data={"ok": False, "error": error}, headers={}, status_code=200
    )
    return SlackApiError(error, response)


def _notion_error(code, status=400):
    return APIResponseError(
        code=code,
        status=status,
        message="internal detail ds-1 secret value",
        headers=httpx.Headers(),
        raw_body_text="",
        additional_data=None,
        request_id="req-1",
    )


def _team():
    return {"id": "T0000TEST"}


def options_request(value=""):
    return {
        "type": "block_suggestion",
        "action_id": "search-notion-database",
        "block_id": "search-notion-database",
        "user": {"id": USER},
        "team": _team(),
        "value": value,
    }


def select_submission(selected_value):
    return {
        "type": "view_submission",
        "user": {"id": USER},
        "team": _team(),
        "view": {
            "type": "modal",
            "id": "V1",
            "callback_id": "select-notion-database",
            "blocks": [{"type": "input", "block_id": "search-notion-database"}],
            "state": {
                "values": {
                    "search-notion-database": {"search-notion-database": {"selected_option": {"value": selected_value}}}
                }
            },
        },
    }


def send_submission(values=None):
    return {
        "type": "view_submission",
        "user": {"id": USER},
        "team": _team(),
        "view": {
            "type": "modal",
            "id": "V2",
            "callback_id": "send-to-notion-database",
            "private_metadata": json.dumps({"notion_data_source_id": "ds-1"}),
            "blocks": [{"type": "input", "block_id": "title"}],
            "state": {"values": values or {"title": {"title": {"value": "Row"}}}},
        },
    }


def _modal_text(views_update):
    return views_update.call_args.kwargs["view"]["blocks"][0]["text"]["text"]


#
# Startup (operator-visible, printed to stderr)
#


def _run_main_subprocess(env):
    base = {k: v for k, v in os.environ.items() if k in ("PATH", "SYSTEMROOT", "HOME")}
    return subprocess.run(
        [sys.executable, "main.py"], cwd=PROJECT_ROOT, env={**base, **env}, capture_output=True, text=True, timeout=30
    )


@needs_no_dotenv
def test_startup_lists_every_missing_setting():
    result = _run_main_subprocess({})
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    assert result.stderr.startswith("Startup failed. Fix these settings, then start the app again:")
    for name in VALID_ENV:
        assert f"- {name} is empty or not set." in result.stderr


@needs_no_dotenv
@pytest.mark.parametrize("name", list(VALID_ENV))
def test_startup_names_the_one_missing_setting(name):
    env = {**VALID_ENV, name: "  "}
    result = _run_main_subprocess(env)
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    assert f"- {name} is empty or not set." in result.stderr
    assert sum(f"- {other} " in result.stderr for other in VALID_ENV) == 1


def _run_main_in_process(env, run_name="not_main"):
    with mock.patch.dict(os.environ, env, clear=True), pytest.raises(SystemExit) as exit_info:
        runpy.run_path(str(PROJECT_ROOT / "main.py"), run_name=run_name)
    return str(exit_info.value.code)


@needs_no_dotenv
def test_startup_reports_rejected_bot_token():
    with mock.patch("slack_sdk.WebClient.auth_test", side_effect=_slack_error("invalid_auth")):
        message = _run_main_in_process(VALID_ENV)
    assert message.startswith("Startup failed: Slack did not accept SLACK_BOT_TOKEN.")
    assert "Settings > Install App" in message


@needs_no_dotenv
def test_startup_reports_unreachable_slack():
    with mock.patch("slack_sdk.WebClient.auth_test", side_effect=urllib.error.URLError("Tunnel connection failed")):
        message = _run_main_in_process(VALID_ENV)
    assert message.startswith("Startup failed: the app could not connect to Slack (")
    assert "Check the network connection" in message


@needs_no_dotenv
def test_startup_reports_rejected_app_token():
    with mock.patch("slack_sdk.WebClient.auth_test", return_value={"ok": True, "user_id": "UBOT", "bot_id": "B1"}), mock.patch(
        "slack_sdk.WebClient.apps_connections_open", side_effect=_slack_error("invalid_auth")
    ):
        message = _run_main_in_process(VALID_ENV, run_name="__main__")
    assert message.startswith("Startup failed: Slack did not accept SLACK_APP_TOKEN (Slack error: invalid_auth).")
    assert "connections:write" in message


@needs_no_dotenv
def test_startup_reports_invalid_log_level():
    with mock.patch("slack_sdk.WebClient.auth_test", return_value={"ok": True, "user_id": "UBOT", "bot_id": "B1"}):
        message = _run_main_in_process({**VALID_ENV, "LOG_LEVEL": "loud"}, run_name="__main__")
    assert message == "Startup failed: LOG_LEVEL 'LOUD' is not valid. Use DEBUG, INFO, WARNING, ERROR or CRITICAL."


#
# Access denied
#


def test_denied_user_sees_reason_on_home_tab(app, slack_api):
    body = {"type": "event_callback", "team_id": "T0000TEST", "event": {"type": "app_home_opened", "user": "UDENIED", "tab": "home"}}
    assert dispatch(app, body).status == 200
    view = slack_api["views_publish"].call_args.kwargs["view"]
    assert view["type"] == "home"
    assert view["blocks"][0]["text"]["text"] == DENIED_MESSAGE
    assert "Sorry" not in DENIED_MESSAGE


def test_denied_messages_tab_open_publishes_nothing(app, slack_api):
    body = {"type": "event_callback", "team_id": "T0000TEST", "event": {"type": "app_home_opened", "user": "UDENIED", "tab": "messages"}}
    dispatch(app, body)
    slack_api["views_publish"].assert_not_called()


#
# Form validation
#


def test_validation_messages_tell_the_user_what_to_enter():
    errors = validate_submission(
        {
            "t": {"title": {"value": "  "}},
            "n": {"number": {"value": "twelve"}},
            "u": {"url": {"value": "httpexample.com"}},
            "e": {"email": {"value": "nobody"}},
        }
    )
    assert errors == {"t": TITLE_ERROR, "n": NUMBER_ERROR, "u": URL_ERROR, "e": EMAIL_ERROR}
    assert TITLE_ERROR == "Enter a title."
    assert URL_ERROR == "Enter a full web address that starts with http:// or https://"


def test_url_validation_accepts_both_schemes_in_any_case():
    assert validate_submission({"u": {"url": {"value": "HTTPS://example.com"}}}) == {}
    assert validate_submission({"u": {"url": {"value": "http://example.com"}}}) == {}


def test_validation_errors_reach_the_modal(app, slack_api):
    resp = dispatch(app, send_submission({"title": {"title": {"value": " "}}, "n": {"number": {"value": "x"}}}))
    assert json.loads(resp.body) == {"response_action": "errors", "errors": {"title": TITLE_ERROR, "n": NUMBER_ERROR}}


#
# Database search (external select)
#


def test_search_failure_shows_a_notice_option(app, slack_api):
    with mock.patch("app.slack_events.build_notion_database_options", side_effect=_notion_error("rate_limited", 429)):
        resp = dispatch(app, options_request())
    options = json.loads(resp.body)["options"]
    assert options == [{"text": {"type": "plain_text", "text": slack_events.SEARCH_FAILED_OPTION}, "value": "search-failed"}]


def test_empty_search_shows_a_notice_option(app, slack_api):
    with mock.patch("app.slack_events.build_notion_database_options", return_value=[]):
        resp = dispatch(app, options_request("nothing"))
    options = json.loads(resp.body)["options"]
    assert options == [{"text": {"type": "plain_text", "text": slack_events.NO_DATABASES_OPTION}, "value": "no-databases"}]


def test_notice_options_fit_slack_option_limit():
    # Slack: option text has at most 75 characters.
    assert len(slack_events.SEARCH_FAILED_OPTION) <= 75
    assert len(slack_events.NO_DATABASES_OPTION) <= 75


@pytest.mark.parametrize("value", ["search-failed", "no-databases"])
def test_selecting_a_notice_option_shows_a_field_error(app, slack_api, value):
    with mock.patch("app.slack_events.find_notion_data_source") as find:
        resp = dispatch(app, select_submission(value))
    assert json.loads(resp.body) == {
        "response_action": "errors",
        "errors": {"search-notion-database": slack_events.NOTICE_OPTION_ERRORS[value]},
    }
    find.assert_not_called()


#
# Loading the database and saving the data
#

ERROR_CASES = [
    (_notion_error("object_not_found", 404), "Notion cannot find this database."),
    (_notion_error("restricted_resource", 403), "This app does not have permission to use this database."),
    (_notion_error("unauthorized", 401), "Notion did not accept this app's integration token."),
    (_notion_error("rate_limited", 429), "Notion received too many requests. Wait one minute, then try again."),
    (_notion_error("validation_error", 400), "Notion did not accept the request."),
    (_notion_error("conflict_error", 409), "Notion was changing the database at the same time. Try again."),
    (_notion_error("service_unavailable", 503), slack_events.NOT_RESPONDING_TEXT),
    (UnknownHTTPResponseError(status=502), slack_events.NOT_RESPONDING_TEXT),
    (UnknownHTTPResponseError(status=418), slack_events.UNEXPECTED_ERROR_TEXT),
    (RequestTimeoutError(), slack_events.NOT_RESPONDING_TEXT),
    (httpx.ConnectError("refused"), slack_events.NOT_RESPONDING_TEXT),
    (KeyError("url"), slack_events.UNEXPECTED_ERROR_TEXT),
]


@pytest.mark.parametrize("error, reason", ERROR_CASES)
def test_load_failure_explains_the_reason(app, slack_api, error, reason):
    with mock.patch("app.slack_events.find_notion_data_source", side_effect=error):
        dispatch(app, select_submission("ds-1"))
    text = _modal_text(slack_api["views_update"])
    assert text.startswith(slack_events.LOAD_FAILED_TEXT)
    assert reason in text
    assert "internal detail" not in text
    assert slack_api["chat_postMessage"].call_args.kwargs == {"channel": USER, "text": text}


@pytest.mark.parametrize("error, reason", ERROR_CASES)
def test_save_failure_explains_the_reason(app, slack_api, error, reason):
    with mock.patch("app.slack_events.send_to_notion", side_effect=error):
        dispatch(app, send_submission())
    text = _modal_text(slack_api["views_update"])
    assert text.startswith(slack_events.SAVE_FAILED_TEXT)
    assert reason in text
    assert "internal detail" not in text
    assert slack_api["chat_postMessage"].call_args.kwargs == {"channel": USER, "text": text}


def test_save_failure_reaches_user_when_modal_is_closed(app, slack_api):
    slack_api["views_update"].side_effect = _slack_error("not_found")
    with mock.patch("app.slack_events.send_to_notion", side_effect=_notion_error("object_not_found", 404)):
        dispatch(app, send_submission())
    text = slack_api["chat_postMessage"].call_args.kwargs["text"]
    assert text.startswith(slack_events.SAVE_FAILED_TEXT)


def test_saved_page_is_not_reported_as_failed_when_modal_is_closed(app, slack_api):
    slack_api["views_update"].side_effect = _slack_error("not_found")
    with mock.patch("app.slack_events.send_to_notion", return_value={"url": "https://www.notion.so/page"}):
        dispatch(app, send_submission())
    text = slack_api["chat_postMessage"].call_args.kwargs["text"]
    assert text == ":white_check_mark: Your data is saved in Notion.\n\nhttps://www.notion.so/page"


def test_success_in_open_modal_sends_no_direct_message(app, slack_api):
    with mock.patch("app.slack_events.send_to_notion", return_value={"url": "https://www.notion.so/page"}):
        dispatch(app, send_submission())
    assert "https://www.notion.so/page" in _modal_text(slack_api["views_update"])
    slack_api["chat_postMessage"].assert_not_called()


#
# Opening the form and going back
#


def test_form_open_failure_sends_a_direct_message(app, slack_api):
    slack_api["views_open"].side_effect = _slack_error("expired_trigger_id")
    resp = dispatch(app, {"command": "/send-to-notion", "user_id": USER, "team_id": "T0000TEST", "trigger_id": "t"})
    assert resp.status == 200
    assert slack_api["chat_postMessage"].call_args.kwargs == {"channel": USER, "text": slack_events.OPEN_FAILED_TEXT}


def test_go_back_failure_sends_a_direct_message(app, slack_api):
    slack_api["views_update"].side_effect = _slack_error("hash_conflict")
    body = {
        "type": "block_actions",
        "user": {"id": USER},
        "team": _team(),
        "trigger_id": "t",
        "view": {"type": "modal", "id": "V2", "hash": "h1"},
        "actions": [{"action_id": "go-back-to-database-select-view", "type": "button", "block_id": "b"}],
    }
    assert dispatch(app, body).status == 200
    assert slack_api["chat_postMessage"].call_args.kwargs == {"channel": USER, "text": slack_events.GO_BACK_FAILED_TEXT}


def test_load_with_no_selected_database_shows_an_error_not_a_stuck_spinner(app, slack_api):
    body = select_submission("ds-1")
    body["view"]["state"]["values"] = {}
    dispatch(app, body)
    text = _modal_text(slack_api["views_update"])
    assert text == f"{slack_events.LOAD_FAILED_TEXT} {slack_events.UNEXPECTED_ERROR_TEXT}"
