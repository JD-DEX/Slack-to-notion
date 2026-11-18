from unittest.mock import MagicMock

import pytest

from app.notion_ops import (
    MAX_OPTIONS,
    NOTION_API_VERSION,
    new_notion_client,
    UNTITLED,
    build_input_blocks,
    build_notion_database_options,
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


def test_client_sends_latest_notion_version():
    notion = new_notion_client("test-notion-token")
    assert NOTION_API_VERSION == "2026-03-11"
    assert notion.client.headers["Notion-Version"] == "2026-03-11"


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
