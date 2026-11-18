import logging
import math
import re
from typing import Any, Dict, List, NamedTuple, Optional, Union
from urllib.parse import urlsplit

from notion_client import Client, RetryOptions

# Latest Notion API version. Since 2025-09-03, databases hold one or more data sources.
# 2026-03-11 renames "archived" to "in_trash" (and changes block APIs this app does not use).
# notion-client 3.x sends 2025-09-03 by default, so set the version explicitly here.
NOTION_API_VERSION = "2026-03-11"

UNTITLED = "Untitled"
# Slack limits: an options response has at most 100 options, and option text has at most 75 characters.
MAX_OPTIONS = 100
MAX_OPTION_TEXT_LENGTH = 75
# A modal has at most 100 blocks.
MAX_MODAL_BLOCKS = 100
# Notion accepts at most 2000 characters in one rich text object (title and rich_text values).
MAX_NOTION_TEXT_LENGTH = 2000
# Short, so the database name and the "Select Database" button fit on one line.
MAX_DATABASE_NAME_LENGTH = 24

# Slack waits 3 seconds for an options response. The search must fail before that time.
SEARCH_TIMEOUT_MS = 2_500
# Loading a data source and creating a page run after the ack, so they can wait longer.
DEFAULT_TIMEOUT_MS = 15_000
DEFAULT_RETRY = RetryOptions(max_retries=2, initial_retry_delay_ms=500, max_retry_delay_ms=5_000)

# The SDK logs failed requests at WARNING level with the Notion error message.
# Notion validation messages can repeat the submitted form values, so the SDK logger
# shows only ERROR (the SDK never logs at ERROR). Without a logger, the SDK also adds
# a new console handler for each client.
_SDK_LOGGER = logging.getLogger("app.notion_sdk")
_SDK_LOGGER.setLevel(logging.ERROR)

# An optional sign, then digits with an optional fraction (for example: 3, -2, 0.5, -.25, 10.).
_NUMBER_PATTERN = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)$")


def plain_title(rich_text: Optional[List[dict]]) -> str:
    """Return the plain text of a Notion title, or "Untitled" when the title is empty."""
    text = "".join(item.get("plain_text", "") for item in (rich_text or [])).strip()
    return text or UNTITLED


def shorten(text: str, limit: int) -> str:
    """Return the text, or its first characters and "…" when it is longer than the limit."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def parse_number(value: Optional[str]) -> Optional[Union[int, float]]:
    """Parse a number from a form input. Return None if the value is not a finite number.

    Values with no fractional part are returned as int (for example "3" and "3.0" give 3).
    """
    if value is None:
        return None
    text = value.strip()
    if not _NUMBER_PATTERN.match(text):
        return None
    if "." not in text:
        return int(text)
    number = float(text)
    if not math.isfinite(number):
        return None
    return int(number) if number.is_integer() else number


def _is_http_url(value: str) -> bool:
    parts = urlsplit(value.strip())
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def validate_submission(state_values: dict) -> Dict[str, str]:
    """Return a dict of block_id -> error message for invalid form values."""
    errors = {}
    for block_id, action_id_to_value in state_values.items():
        action_id = list(action_id_to_value.keys())[0]
        value_obj = action_id_to_value[action_id]
        str_value = value_obj.get("value")
        if action_id == "title" and (str_value is None or str_value.strip() == ""):
            errors[block_id] = "The title must not be empty"
        if action_id == "number" and str_value is not None and parse_number(str_value) is None:
            errors[block_id] = "This property must be a number"
        if action_id == "url" and str_value is not None and not _is_http_url(str_value):
            errors[block_id] = "This property must be a URL"
        if action_id == "email" and str_value is not None and "@" not in str_value:
            errors[block_id] = "This property must be an email address"
    return errors


class NotionClients(NamedTuple):
    """Notion clients made once at startup and shared by all requests."""

    search: Client  # For the database picker: short timeout and no retries.
    default: Client  # For loading a data source and creating pages.


def new_notion_clients(token: str) -> NotionClients:
    def make(timeout_ms: int, retry: Union[RetryOptions, bool]) -> Client:
        return Client(
            auth=token,
            notion_version=NOTION_API_VERSION,
            timeout_ms=timeout_ms,
            retry=retry,
            logger=_SDK_LOGGER,
            log_level=logging.ERROR,
        )

    return NotionClients(search=make(SEARCH_TIMEOUT_MS, False), default=make(DEFAULT_TIMEOUT_MS, DEFAULT_RETRY))


def find_notion_data_source(notion: Client, data_source_id: str) -> dict:
    """Return the data source, which holds the title and the property schema."""
    return notion.data_sources.retrieve(data_source_id=data_source_id)


def build_notion_database_options(notion: Client, payload: dict) -> List[Dict[str, Any]]:
    """Search for data sources. Each option value is a data source ID.

    A database with more than one data source returns one search result per data source,
    so the user picks the exact data source to write to.
    """
    query = payload.get("value")
    database_filter = {"property": "object", "value": "data_source"}
    # Request one page only. Slack shows at most 100 options and waits only 3 seconds.
    page = notion.search(query=query, filter=database_filter, page_size=MAX_OPTIONS)
    # Skip data sources in the trash. Notion cannot add pages to them.
    databases = [r for r in page["results"] if not r.get("in_trash")]

    options = []
    for database in databases[:MAX_OPTIONS]:
        database_id = database["id"]
        database_name = shorten(plain_title(database.get("title")), MAX_OPTION_TEXT_LENGTH)
        options.append({"text": {"type": "plain_text", "text": database_name}, "value": database_id})
    return options


def _build_option(o: dict) -> dict:
    return {
        "text": {"type": "plain_text", "text": shorten(o["name"], MAX_OPTION_TEXT_LENGTH)},
        "value": o["id"],
    }


def _select_element(prop_type: str, options: List[dict]) -> Dict[str, Any]:
    """Return a select menu for a select or multi_select property.

    The action ID is the property type, which to_page_properties reads.
    A static menu has at most 100 options. Above that, Slack asks the app for the options.
    """
    multi = "multi_" if prop_type == "multi_select" else ""
    if len(options) <= MAX_OPTIONS:
        return {
            "type": f"{multi}static_select",
            "options": [_build_option(o) for o in options],
            "action_id": prop_type,
        }
    return {"type": f"{multi}external_select", "action_id": prop_type, "min_query_length": 0}


def build_property_options(data_source: dict, property_id: str, query: str) -> List[Dict[str, Any]]:
    """Return at most 100 options of a select or multi_select property whose names contain the query."""
    prop = next((p for p in data_source.get("properties", {}).values() if p.get("id") == property_id), None)
    if prop is None or prop.get("type") not in ("select", "multi_select"):
        return []
    query = (query or "").strip().casefold()
    matches = [o for o in prop[prop["type"]]["options"] if query in o["name"].casefold()]
    return [_build_option(o) for o in matches[:MAX_OPTIONS]]


def _property_element(prop_type: str, prop: dict) -> Optional[Dict[str, Any]]:
    """Return the form element for a property type, or None for unsupported types.

    Not supported yet: people, relation, formula, rollup and files.
    """
    if prop_type in ("select", "multi_select"):
        options = prop[prop_type]["options"]
        return _select_element(prop_type, options) if options else None
    if prop_type == "rich_text":
        return {
            "type": "plain_text_input",
            "multiline": True,
            "max_length": MAX_NOTION_TEXT_LENGTH,
            "action_id": prop_type,
        }
    if prop_type in ("number", "url", "email", "phone_number"):
        return {"type": "plain_text_input", "action_id": prop_type}
    if prop_type == "date":
        return {"type": "datepicker", "action_id": prop_type}
    if prop_type == "checkbox":
        return {
            "type": "checkboxes",
            "options": [
                {
                    "text": {"type": "plain_text", "text": "Yes"},
                    "description": {"type": "plain_text", "text": "This will be synchronized in Notion"},
                    "value": "checked",
                }
            ],
            "action_id": prop_type,
        }
    return None


def build_input_blocks(database: dict) -> List[Dict[str, Any]]:
    database_name = shorten(plain_title(database.get("title")), MAX_DATABASE_NAME_LENGTH)
    header = {
        "type": "section",
        # plain_text, so characters in the name have no mrkdwn meaning.
        "text": {"type": "plain_text", "text": f'You\'re going to send data to Notion database "{database_name}"'},
        "accessory": {
            "type": "button",
            "action_id": "go-back-to-database-select-view",
            "text": {"type": "plain_text", "text": "Select Database"},
            "value": "1",
        },
    }
    properties = database["properties"]
    title_key = next(name for name, prop in properties.items() if prop["type"] == "title")
    title_block = {
        "type": "input",
        "block_id": properties[title_key]["id"],
        "element": {"type": "plain_text_input", "max_length": MAX_NOTION_TEXT_LENGTH, "action_id": "title"},
        "label": {"type": "plain_text", "text": title_key},
        "optional": False,
    }

    property_blocks = []
    for name, prop in properties.items():
        if name == title_key:
            continue
        prop_type = prop.get("type", "text")
        element = _property_element(prop_type, prop)
        if element is not None:
            property_blocks.append(
                {
                    "type": "input",
                    "block_id": prop.get("id", name),
                    "element": element,
                    "label": {"type": "plain_text", "text": name},
                    "optional": True,
                }
            )

    # The header and the title block use 2 of the blocks in the modal.
    room = MAX_MODAL_BLOCKS - 2
    if len(property_blocks) > room:
        hidden = len(property_blocks) - (room - 1)
        note = {
            "type": "context",
            "elements": [
                {
                    "type": "plain_text",
                    "text": f"{hidden} properties are not shown. A Slack form has at most {MAX_MODAL_BLOCKS} blocks.",
                }
            ],
        }
        property_blocks = property_blocks[: room - 1] + [note]

    return [header, title_block] + property_blocks


def to_page_properties(state_values: dict) -> dict:
    properties = {}
    for block_id, v in state_values.items():
        action_id = list(v.keys())[0]
        if action_id == "title":
            properties[block_id] = {"title": [{"type": "text", "text": {"content": v[action_id]["value"]}}]}
        elif action_id == "select" and v[action_id].get("selected_option") is not None:
            properties[block_id] = {"select": {"id": v[action_id]["selected_option"]["value"]}}
        elif action_id == "multi_select" and v[action_id].get("selected_options") is not None:
            selected_options = [{"id": o["value"]} for o in v[action_id]["selected_options"]]
            properties[block_id] = {"multi_select": selected_options}
        elif action_id == "rich_text" and v[action_id].get("value") is not None:
            properties[block_id] = {"rich_text": [{"text": {"content": v[action_id]["value"]}}]}
        elif action_id == "number" and v[action_id].get("value") is not None:
            properties[block_id] = {"number": parse_number(v[action_id]["value"])}
        elif action_id == "date" and v[action_id].get("selected_date") is not None:
            properties[block_id] = {"date": {"start": v[action_id]["selected_date"]}}
        elif action_id == "checkbox" and v[action_id].get("selected_options") is not None:
            properties[block_id] = {"checkbox": len(v[action_id]["selected_options"]) > 0}
        elif action_id == "url" and v[action_id].get("value") is not None:
            properties[block_id] = {"url": v[action_id]["value"]}
        elif action_id == "email" and v[action_id].get("value") is not None:
            properties[block_id] = {"email": v[action_id]["value"]}
        elif action_id == "phone_number" and v[action_id].get("value") is not None:
            properties[block_id] = {"phone_number": v[action_id]["value"]}
    return properties


def send_to_notion(notion: Client, data_source_id: str, state_values: dict):
    new_page = notion.pages.create(
        parent={"type": "data_source_id", "data_source_id": data_source_id},
        properties=to_page_properties(state_values),
    )
    return new_page
