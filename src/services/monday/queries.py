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


def add_file_to_update(update_id: str) -> str:
    """Hang one file off an update that has already been posted.

    The bytes are not in this string: they travel as a separate form part, and
    `$file` is the placeholder MondayModel._execute's `map` points that part at.
    That map is hardcoded {"image": "variables.file"}, so the variable here has
    to be named `file` and the caller's files dict has to be keyed "image" -
    rename either side and monday answers "Variable $file is not defined".

    `update_id` is an ID!, which monday takes as a quoted string, so it gets the
    single json.dumps create_update's item_id gets.
    """
    return f"""
        mutation ($file: File!) {{
            add_file_to_update(
                update_id: {json.dumps(str(update_id))},
                file: $file
            ) {{
                id
            }}
        }}
    """


def write_long_text_column(board_id: int, item_id: str, column_id: str, text: str) -> str:
    """Overwrite one long_text column with `text`, replacing whatever it held.

    `value` is a JSON! rather than the String! `body` is, so it takes the double
    encode create_update's body does not: once to build the {"text": ...} the
    column type wants, again to land that object as a GraphQL string literal.

    The board id is required here and nowhere else in this file - monday resolves
    a column against its board, where an update hangs off the item alone.
    """
    value = json.dumps({"text": text})
    return f"""
        mutation {{
            change_column_value(
                board_id: {json.dumps(str(board_id))},
                item_id: {json.dumps(str(item_id))},
                column_id: {json.dumps(str(column_id))},
                value: {json.dumps(value)}
            ) {{
                id
            }}
        }}
    """


def item_column_text(item_id: str, column_id: str) -> str:
    """The text of one column on one item.

    The lookup find_item_id_by_phone cannot serve: that one searches a board for a
    phone, where the create_update webhook arrives naming an item and needing the
    person behind it. A root `items(ids:)` read rather than a board search,
    because the id is already known - searching for it would spend complexity
    finding what was handed over.

    `column_values(ids:)` is filtered rather than read whole: an item on either
    board carries dozens of columns, and the response is the only thing this pays
    for. Both ids are quoted scalars, so each takes the single json.dumps
    create_update's item_id takes, not the double encode
    write_long_text_column's value takes.
    """
    return f"""
        query {{
            items(ids: [{json.dumps(str(item_id))}]) {{
                column_values(ids: [{json.dumps(str(column_id))}]) {{
                    text
                }}
            }}
        }}
    """
