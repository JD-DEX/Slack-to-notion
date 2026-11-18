import json
import logging
from unittest.mock import MagicMock

import httpx
import pytest
from notion_client import APIResponseError, RequestTimeoutError
from slack_sdk.models.basic_objects import JsonObject
from slack_sdk.models.views import View

from app.notion_ops import (
    MAX_OPTIONS,
    NOTION_API_VERSION,
    new_notion_clients,
    UNTITLED,
    build_input_blocks,
    build_notion_database_options,
    build_property_options,
    parse_number,
    send_to_notion,
    to_page_properties,
    validate_submission,
)

#
# to_page_properties
#


def test_to_page_properties_maps_every_supported_type():
    state_values = {
        "title-id": {"title": {"type": "plain_text_input", "value": "My row"}},
        "select-id": {"select": {"selected_option": {"value": "opt-1"}}},
        "multi-id": {"multi_select": {"selected_options": [{"value": "a"}, {"value": "b"}]}},
        "text-id": {"rich_text": {"value": "Some notes"}},
        "int-id": {"number": {"value": "42"}},
        "float-id": {"number": {"value": "-1.5"}},
        "date-id": {"date": {"selected_date": "2026-11-18"}},
        "check-id": {"checkbox": {"selected_options": [{"value": "checked"}]}},
        "url-id": {"url": {"value": "https://example.com"}},
        "email-id": {"email": {"value": "someone@example.com"}},
        "phone-id": {"phone_number": {"value": "+44 20 0000 0000"}},
    }

    assert to_page_properties(state_values) == {
        "title-id": {"title": [{"type": "text", "text": {"content": "My row"}}]},
        "select-id": {"select": {"id": "opt-1"}},
        "multi-id": {"multi_select": [{"id": "a"}, {"id": "b"}]},
        "text-id": {"rich_text": [{"text": {"content": "Some notes"}}]},
        "int-id": {"number": 42},
        "float-id": {"number": -1.5},
        "date-id": {"date": {"start": "2026-11-18"}},
        "check-id": {"checkbox": True},
        "url-id": {"url": "https://example.com"},
        "email-id": {"email": "someone@example.com"},
        "phone-id": {"phone_number": "+44 20 0000 0000"},
    }


def test_to_page_properties_skips_empty_optional_values():
    state_values = {
        "title-id": {"title": {"value": "Row"}},
        "select-id": {"select": {"selected_option": None}},
        "text-id": {"rich_text": {"value": None}},
        "num-id": {"number": {"value": None}},
        "date-id": {"date": {"selected_date": None}},
    }
    assert to_page_properties(state_values) == {
        "title-id": {"title": [{"type": "text", "text": {"content": "Row"}}]},
    }


def test_to_page_properties_keeps_integer_type_for_whole_numbers():
    props = to_page_properties({"n": {"number": {"value": "3.0"}}})
    assert props["n"]["number"] == 3
    assert isinstance(props["n"]["number"], int)


#
# Number parsing and validation
#


@pytest.mark.parametrize(
    "raw, expected",
    [("3", 3), ("-2", -2), ("+7", 7), ("0.5", 0.5), ("-.25", -0.25), ("10.", 10), (" 4 ", 4)],
)
def test_parse_number_accepts_negatives_and_decimals(raw, expected):
    assert parse_number(raw) == expected
    assert type(parse_number(raw)) is type(expected)


@pytest.mark.parametrize("raw", ["", "abc", "1e5", "nan", "inf", "1_000", "1,5", "--1", "."])
def test_parse_number_rejects_invalid_values(raw):
    assert parse_number(raw) is None


def test_validate_submission_accepts_valid_values():
    state_values = {
        "t": {"title": {"value": "Row"}},
        "n1": {"number": {"value": "-3"}},
        "n2": {"number": {"value": "2.75"}},
        "u": {"url": {"value": "https://example.com"}},
        "e": {"email": {"value": "a@example.com"}},
        "empty-number": {"number": {"value": None}},
    }
    assert validate_submission(state_values) == {}


def test_validate_submission_reports_each_invalid_field():
    state_values = {
        "t": {"title": {"value": "   "}},
        "n": {"number": {"value": "twelve"}},
        "u": {"url": {"value": "example.com"}},
        "e": {"email": {"value": "not-an-email"}},
    }
    errors = validate_submission(state_values)
    assert set(errors) == {"t", "n", "u", "e"}
    assert "title" in errors["t"].lower()


def test_validate_submission_rejects_missing_title():
    assert "t" in validate_submission({"t": {"title": {"value": None}}})


#
# Untitled databases (data sources)
#


def _search_result(results, next_cursor=None):
    return {"object": "list", "results": results, "next_cursor": next_cursor, "has_more": next_cursor is not None}


def test_options_fall_back_to_untitled():
    notion = MagicMock()
    notion.search.return_value = _search_result(
        [
            {"object": "data_source", "id": "ds-empty", "title": []},
            {"object": "data_source", "id": "ds-missing"},
            {"object": "data_source", "id": "ds-named", "title": [{"plain_text": "Tasks"}]},
        ]
    )

    options = build_notion_database_options(notion, {"value": ""})

    assert [o["text"]["text"] for o in options] == [UNTITLED, UNTITLED, "Tasks"]
    assert [o["value"] for o in options] == ["ds-empty", "ds-missing", "ds-named"]


def test_options_search_for_data_sources():
    notion = MagicMock()
    notion.search.return_value = _search_result([])

    build_notion_database_options(notion, {"value": "abc"})

    kwargs = notion.search.call_args.kwargs
    assert kwargs["query"] == "abc"
    assert kwargs["filter"] == {"property": "object", "value": "data_source"}


def test_options_skip_trashed_data_sources():
    notion = MagicMock()
    notion.search.return_value = _search_result(
        [
            {"object": "data_source", "id": "ds-live", "title": [{"plain_text": "Live"}], "in_trash": False},
            {"object": "data_source", "id": "ds-trash", "title": [{"plain_text": "Old"}], "in_trash": True},
        ]
    )

    options = build_notion_database_options(notion, {"value": ""})

    assert [o["value"] for o in options] == ["ds-live"]


def _transport(status, body, calls):
    def handler(request):
        calls.append(request)
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handler)


def _with_transport(notion, transport):
    # The client setter copies the base URL, timeout and headers from the client options.
    notion.client = httpx.Client(transport=transport)
    return notion


def test_clients_send_latest_notion_version():
    clients = new_notion_clients("test-notion-token")
    assert NOTION_API_VERSION == "2026-03-11"
    assert clients.search.client.headers["Notion-Version"] == "2026-03-11"
    assert clients.default.client.headers["Notion-Version"] == "2026-03-11"


def test_making_clients_does_not_add_log_handlers():
    sdk_logger = logging.getLogger("notion_client")
    before = len(sdk_logger.handlers)
    for _ in range(5):
        new_notion_clients("test-notion-token")
    assert len(sdk_logger.handlers) == before


def test_notion_failures_do_not_log_form_values(caplog):
    secret = "secret-user-input"
    body = {"object": "error", "status": 400, "code": "validation_error", "message": f"instead was `{secret}`"}
    notion = _with_transport(new_notion_clients("t").default, _transport(400, body, []))

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(APIResponseError):
            notion.pages.create(parent={"data_source_id": "ds-1"}, properties={})

    assert secret not in caplog.text


def test_search_client_does_not_retry_rate_limits():
    calls = []
    body = {"object": "error", "status": 429, "code": "rate_limited", "message": "slow down"}
    notion = _with_transport(new_notion_clients("t").search, _transport(429, body, calls))

    with pytest.raises(APIResponseError):
        notion.search(query="", filter={"property": "object", "value": "data_source"})

    assert len(calls) == 1


def test_search_client_times_out_inside_the_slack_limit():
    def slow(request):
        raise httpx.ReadTimeout("timed out", request=request)

    notion = _with_transport(new_notion_clients("t").search, httpx.MockTransport(slow))

    with pytest.raises(RequestTimeoutError):
        notion.search(query="")
    # Slack waits 3 seconds for an options response.
    assert notion.client.timeout.read is not None and notion.client.timeout.read < 3


def test_default_client_limits_retry_delay(monkeypatch):
    calls = []
    delays = []
    monkeypatch.setattr("notion_client.client.time.sleep", delays.append)
    body = {"object": "error", "status": 429, "code": "rate_limited", "message": "slow down"}

    def handler(request):
        calls.append(request)
        return httpx.Response(429, json=body, headers={"retry-after": "60"})

    notion = _with_transport(new_notion_clients("t").default, httpx.MockTransport(handler))

    with pytest.raises(APIResponseError):
        notion.pages.create(parent={"data_source_id": "ds-1"}, properties={})

    assert len(calls) == 3
    # The server asks for 60 seconds. The client waits at most 5 seconds for each retry.
    assert delays and max(delays) <= 5


def test_options_respect_slack_limits():
    notion = MagicMock()
    long_title = [{"plain_text": "x" * 200}]
    page = [{"object": "data_source", "id": f"ds-{i}", "title": long_title} for i in range(MAX_OPTIONS)]
    notion.search.return_value = _search_result(page, next_cursor="more")

    options = build_notion_database_options(notion, {"value": ""})

    assert len(options) == MAX_OPTIONS
    assert all(len(o["text"]["text"]) <= 75 for o in options)
    assert notion.search.call_count == 1


def test_input_blocks_for_untitled_data_source():
    data_source = {
        "object": "data_source",
        "id": "ds-1",
        "title": [],
        "properties": {
            "Name": {"id": "title", "type": "title"},
            "Count": {"id": "abc", "type": "number"},
        },
    }

    blocks = build_input_blocks(data_source)

    assert '"Untitled"' in blocks[0]["text"]["text"]
    assert blocks[1]["block_id"] == "title"
    assert blocks[1]["element"]["action_id"] == "title"
    assert blocks[2]["element"]["action_id"] == "number"


def test_send_to_notion_uses_data_source_parent():
    notion = MagicMock()
    notion.pages.create.return_value = {"url": "https://www.notion.so/page"}

    send_to_notion(notion, "ds-1", {"title": {"title": {"value": "Row"}}})

    kwargs = notion.pages.create.call_args.kwargs
    assert kwargs["parent"] == {"type": "data_source_id", "data_source_id": "ds-1"}


#
# Slack limits for the input form
#


def _assert_valid_slack_view(blocks):
    """Validate the blocks with the Slack SDK models, which hold the Block Kit limits."""
    view = View(type="modal", title="Send to Notion", blocks=blocks)

    def walk(obj):
        if isinstance(obj, JsonObject):
            obj.validate_json()
            for value in vars(obj).values():
                walk(value)
        elif isinstance(obj, (list, tuple)):
            for item in obj:
                walk(item)

    walk(view)


def _data_source(**properties):
    return {"object": "data_source", "id": "ds-1", "title": [{"plain_text": "Tasks"}], "properties": properties}


def _select(prop_type, count, name_length=10):
    options = [{"id": f"opt-{i}", "name": f"{i}".ljust(name_length, "x")} for i in range(count)]
    return {"id": f"{prop_type}-id", "type": prop_type, prop_type: {"options": options}}


def test_input_blocks_shorten_long_option_names():
    data_source = _data_source(Name={"id": "title", "type": "title"}, Status=_select("select", 3, name_length=100))

    blocks = build_input_blocks(data_source)

    _assert_valid_slack_view(blocks)
    texts = [o["text"]["text"] for o in blocks[2]["element"]["options"]]
    assert all(len(t) <= 75 for t in texts)
    assert all(t.endswith("…") for t in texts)


@pytest.mark.parametrize(
    "prop_type, element_type", [("select", "external_select"), ("multi_select", "multi_external_select")]
)
def test_input_blocks_use_external_select_above_100_options(prop_type, element_type):
    data_source = _data_source(Name={"id": "title", "type": "title"}, Tags=_select(prop_type, 150))

    blocks = build_input_blocks(data_source)

    _assert_valid_slack_view(blocks)
    element = blocks[2]["element"]
    assert element["type"] == element_type
    # to_page_properties reads the property type from the action ID.
    assert element["action_id"] == prop_type


@pytest.mark.parametrize("prop_type", ["select", "multi_select"])
def test_input_blocks_keep_static_select_up_to_100_options(prop_type):
    data_source = _data_source(Name={"id": "title", "type": "title"}, Tags=_select(prop_type, 100))

    blocks = build_input_blocks(data_source)

    _assert_valid_slack_view(blocks)
    assert len(blocks[2]["element"]["options"]) == 100


def test_property_options_filter_and_respect_slack_limits():
    data_source = _data_source(Name={"id": "title", "type": "title"}, Tags=_select("multi_select", 300, name_length=100))

    all_options = build_property_options(data_source, "multi_select-id", "")
    some_options = build_property_options(data_source, "multi_select-id", "25")

    assert len(all_options) == 100
    assert all(len(o["text"]["text"]) <= 75 for o in all_options)
    assert [o["value"] for o in some_options] == [f"opt-{i}" for i in range(300) if "25" in str(i)]


def test_property_options_match_without_case():
    data_source = _data_source(Status={"id": "s", "type": "select", "select": {"options": [{"id": "a", "name": "Done"}]}})
    assert [o["value"] for o in build_property_options(data_source, "s", "dONE")] == ["a"]


def test_property_options_for_unknown_property_are_empty():
    data_source = _data_source(Name={"id": "title", "type": "title"})
    assert build_property_options(data_source, "missing", "") == []


def test_input_blocks_respect_the_modal_block_limit():
    properties = {"Name": {"id": "title", "type": "title"}}
    properties.update({f"Field {i}": {"id": f"p{i}", "type": "number"} for i in range(150)})

    blocks = build_input_blocks(_data_source(**properties))

    _assert_valid_slack_view(blocks)
    assert len(blocks) == 100
    assert blocks[1]["block_id"] == "title"
    # 97 properties are shown. The last block tells the user that 53 properties are not shown.
    assert sum(1 for b in blocks if b.get("element", {}).get("action_id") == "number") == 97
    assert "53 properties are not shown" in json.dumps(blocks[-1])


def test_text_inputs_accept_at_most_the_notion_text_limit():
    data_source = _data_source(Name={"id": "title", "type": "title"}, Notes={"id": "n", "type": "rich_text"})

    blocks = build_input_blocks(data_source)

    assert blocks[1]["element"]["max_length"] == 2000
    assert blocks[2]["element"]["max_length"] == 2000


def test_database_name_is_plain_text_and_shortened():
    data_source = _data_source(Name={"id": "title", "type": "title"})
    data_source["title"] = [{"plain_text": "<!channel> *bold* & a very long database name"}]

    header = build_input_blocks(data_source)[0]["text"]

    assert header["type"] == "plain_text"
    assert header["text"].endswith('"<!channel> *bold* & a v…"')


def test_checkbox_option_has_visible_text():
    data_source = _data_source(Name={"id": "title", "type": "title"}, Done={"id": "d", "type": "checkbox"})

    option = build_input_blocks(data_source)[2]["element"]["options"][0]

    assert option["text"]["text"].strip() != ""


@pytest.mark.parametrize("url", ["httpfoo", "http://", "ftp://example.com", "https//example.com"])
def test_validate_submission_rejects_invalid_urls(url):
    assert "u" in validate_submission({"u": {"url": {"value": url}}})


@pytest.mark.parametrize("url", ["https://example.com", "http://example.com/a?b=c"])
def test_validate_submission_accepts_http_urls(url):
    assert validate_submission({"u": {"url": {"value": url}}}) == {}
