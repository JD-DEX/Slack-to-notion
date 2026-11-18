import math
import re
from typing import Any, Dict, List, Optional, Union

from notion_client import Client

# Latest Notion API version. Since 2025-09-03, databases hold one or more data sources.
# 2026-03-11 renames "archived" to "in_trash" (and changes block APIs this app does not use).
# notion-client 3.x sends 2025-09-03 by default, so set the version explicitly here.
NOTION_API_VERSION = "2026-03-11"

UNTITLED = "Untitled"
# Slack limits: an options response has at most 100 options, and option text has at most 75 characters.
MAX_OPTIONS = 100
MAX_OPTION_TEXT_LENGTH = 75

# Form validation messages. Each one tells the user what to type.
TITLE_ERROR = "Enter a title."
NUMBER_ERROR = "Enter a number, for example 42, -3 or 0.5."
URL_ERROR = "Enter a full web address that starts with http:// or https://"
EMAIL_ERROR = "Enter an email address, for example name@example.com."

# An optional sign, then digits with an optional fraction (for example: 3, -2, 0.5, -.25, 10.).
_NUMBER_PATTERN = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)$")


def plain_title(rich_text: Optional[List[dict]]) -> str:
    """Return the plain text of a Notion title, or "Untitled" when the title is empty."""
    text = "".join(item.get("plain_text", "") for item in (rich_text or [])).strip()
    return text or UNTITLED


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


def validate_submission(state_values: dict) -> Dict[str, str]:
    """Return a dict of block_id -> error message for invalid form values."""
    errors = {}
    for block_id, action_id_to_value in state_values.items():
        action_id = list(action_id_to_value.keys())[0]
        value_obj = action_id_to_value[action_id]
        str_value = value_obj.get("value")
        if action_id == "title" and (str_value is None or str_value.strip() == ""):
            errors[block_id] = TITLE_ERROR
        if action_id == "number" and str_value is not None and parse_number(str_value) is None:
            errors[block_id] = NUMBER_ERROR
        if action_id == "url" and str_value is not None and not str_value.lower().startswith(("http://", "https://")):
            errors[block_id] = URL_ERROR
        if action_id == "email" and str_value is not None and "@" not in str_value:
            errors[block_id] = EMAIL_ERROR
    return errors


def new_notion_client(token: str) -> Client:
    return Client(auth=token, notion_version=NOTION_API_VERSION)


def find_notion_data_source(notion: Client, data_source_id: str) -> dict:
    """Return the data source, which holds the title and the property schema."""
    return notion.data_sources.retrieve(data_source_id=data_source_id)


def build_notion_database_options(notion: Client, payload: dict) -> List[Dict[str, Any]]:
    """Search for data sources. Each option value is a data source ID.

    A database with more than one data source returns one search result per data source,
    so the user picks the exact data source to write to.
    """
    databases = []
    query = payload.get("value")
    database_filter = {"property": "object", "value": "data_source"}
    cursor = None
    # Slack shows at most 100 options, so stop after 100 results.
    # This also keeps the request inside Slack's 3-second limit.
    while len(databases) < MAX_OPTIONS:
        page = notion.search(query=query, filter=database_filter, start_cursor=cursor, page_size=MAX_OPTIONS)
        # Skip data sources in the trash. Notion cannot add pages to them.
        databases = databases + [r for r in page["results"] if not r.get("in_trash")]
        cursor = page.get("next_cursor")
        if cursor is None:
            break

    options = []
    for database in databases[:MAX_OPTIONS]:
        database_id = database["id"]
        database_name = plain_title(database.get("title"))[:MAX_OPTION_TEXT_LENGTH]
        options.append({"text": {"type": "plain_text", "text": database_name}, "value": database_id})
    return options


def _build_option(o: dict) -> dict:
    return {
        "text": {"type": "plain_text", "text": o["name"]},
        "value": o["id"],
    }


def build_input_blocks(database: dict) -> List[Dict[str, Any]]:
    blocks = []
    database_name = plain_title(database.get("title"))[:24]
    blocks.append(
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f'You\'re going to send data to Notion database *"{database_name}"*',
            },
            "accessory": {
                "type": "button",
                "action_id": "go-back-to-database-select-view",
                "text": {"type": "plain_text", "text": "Select Database"},
                "value": "1",
            },
        },
    )
    title_key = list(filter(lambda p: p[1]["type"] == "title", database["properties"].items()))[0][0]
    title = database["properties"].pop(title_key)
    blocks.append(
        {
            "type": "input",
            "block_id": title["id"],
            "element": {"type": "plain_text_input", "action_id": "title"},
            "label": {"type": "plain_text", "text": title_key},
            "optional": False,
        }
    )
    for name, prop in database["properties"].items():
        prop_type = prop.get("type", "text")
        label = {"type": "plain_text", "text": name}
        block_id = prop.get("id", name)
        if prop_type == "select" and len(prop["select"]["options"]) > 0:
            blocks.append(
                {
                    "type": "input",
                    "block_id": block_id,
                    "element": {
                        "type": "static_select",
                        "options": [_build_option(o) for o in prop["select"]["options"]],
                        "action_id": prop_type,
                    },
                    "label": label,
                    "optional": True,
                }
            )
        elif prop_type == "multi_select" and len(prop["multi_select"]["options"]) > 0:
            blocks.append(
                {
                    "type": "input",
                    "block_id": block_id,
                    "element": {
                        "type": "multi_static_select",
                        "options": [_build_option(o) for o in prop["multi_select"]["options"]],
                        "action_id": prop_type,
                    },
                    "label": label,
                    "optional": True,
                }
            )
        elif prop_type == "rich_text":
            blocks.append(
                {
                    "type": "input",
                    "block_id": block_id,
                    "element": {"type": "plain_text_input", "multiline": True, "action_id": prop_type},
                    "label": label,
                    "optional": True,
                }
            )
        elif prop_type in ("number", "url", "email", "phone_number"):
            blocks.append(
                {
                    "type": "input",
                    "block_id": block_id,
                    "element": {"type": "plain_text_input", "action_id": prop_type},
                    "label": label,
                    "optional": True,
                }
            )
        elif prop_type == "date":
            blocks.append(
                {
                    "type": "input",
                    "block_id": block_id,
                    "element": {"type": "datepicker", "action_id": prop_type},
                    "label": label,
                    "optional": True,
                }
            )
        elif prop_type == "checkbox":
            blocks.append(
                {
                    "type": "input",
                    "block_id": block_id,
                    "element": {
                        "type": "checkboxes",
                        "options": [
                            {
                                "text": {"type": "plain_text", "text": " "},
                                "description": {"type": "plain_text", "text": "This will be synchronized in Notion"},
                                "value": "checked",
                            }
                        ],
                        "action_id": prop_type,
                    },
                    "label": label,
                    "optional": True,
                }
            )

        # The following ones are not yet supported
        # people
        # relation
        # formula
        # rollup
        # files

    return blocks


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
