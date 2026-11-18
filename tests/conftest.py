"""Shared fixtures: the production app with mocked Slack and Notion clients."""

from concurrent.futures import Executor, Future
from unittest import mock

import pytest
from slack_bolt.authorization import AuthorizeResult

from app.notion_ops import NotionClients
from main import build_app
from tests.helpers import ALLOWED_USER


def _authorize(**kwargs):
    # No real token. The Slack client is mocked in every test that makes a call.
    return AuthorizeResult(enterprise_id=None, team_id="T0000TEST", bot_token="test-token", bot_user_id="UBOT", bot_id="B1")


class _InlineExecutor(Executor):
    """Run lazy listeners at once in the test thread, so the tests are deterministic."""

    def submit(self, fn, /, *args, **kwargs):
        future = Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except Exception as e:
            future.set_exception(e)
        return future


@pytest.fixture
def notion():
    return mock.MagicMock()


@pytest.fixture
def app(notion):
    # Use the production wiring from main.py. Only the Slack authorization is replaced.
    # process_before_response=True runs listeners in the request thread, so the tests are deterministic.
    return build_app(
        frozenset({ALLOWED_USER}),
        NotionClients(search=notion.search_client, default=notion.default_client),
        signing_secret="test",
        authorize=_authorize,
        request_verification_enabled=False,
        process_before_response=True,
        listener_executor=_InlineExecutor(),
    )


@pytest.fixture
def slack_api():
    with mock.patch("slack_sdk.WebClient.views_open") as views_open, mock.patch(
        "slack_sdk.WebClient.views_publish"
    ) as views_publish, mock.patch("slack_sdk.WebClient.views_update") as views_update, mock.patch(
        "slack_sdk.WebClient.chat_postMessage"
    ) as chat_post_message:
        yield {
            "views_open": views_open,
            "views_publish": views_publish,
            "views_update": views_update,
            "chat_postMessage": chat_post_message,
        }
