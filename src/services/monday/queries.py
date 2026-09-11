import json

def create_update(item_id: str, body: str) -> str:
    """Post one update on an item's Updates section.

    `body` is a plain String!, not the JSON scalar column_values is, so it takes
    a single json.dumps - the double encode _column_values_arg does would send
    the quotes through as literal text.

    Monday renders the body as HTML, so newlines have to arrive as <br> or the
    whole update collapses onto one line.
    """
    return f"""
        mutation {{
            create_update(
                item_id: {json.dumps(str(item_id))},
                body: {json.dumps(body)}
            ) {{
                id
            }}
        }}
    """


def find_item_id_by_phone(board_id: int, column_id: str, phone: str) -> str:
    """The id of the first item on `board_id` whose phone column carries `phone`.

    `contains_text` rather than `any_of`, mirroring CL's own lookup against this
    board: a 10-digit stored value still matches the 11-digit key. `phone` is
    digits only, so nothing in it needs escaping.
    """
    return f"""
        query {{
            boards(ids: [{board_id}]) {{
                items_page(
                    limit: 1
                    query_params: {{
                        rules: [
                            {{
                                column_id: {json.dumps(str(column_id))}
                                compare_value: {json.dumps(phone)}
                                operator: contains_text
                            }}
                        ]
                    }}
                ) {{
                    items {{ id }}
                }}
            }}
        }}
    """
