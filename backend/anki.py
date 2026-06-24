import requests

ANKI_CONNECT_URL = "http://127.0.0.1:8765"


class AnkiConnectError(Exception):
    pass


def _invoke(action: str, **params):
    payload = {"action": action, "version": 6, "params": params}
    resp = requests.post(ANKI_CONNECT_URL, json=payload, timeout=5)
    resp.raise_for_status()
    body = resp.json()
    if body.get("error"):
        raise AnkiConnectError(body["error"])
    return body["result"]


def is_available() -> bool:
    try:
        _invoke("version")
        return True
    except Exception:
        return False


def get_deck_names():
    return _invoke("deckNames")


def get_all_known_words(deck_name: str | None = None):
    """Pulls front-field text from all notes (optionally in one deck) to
    seed known-word status from an existing Anki collection."""
    query = f'deck:"{deck_name}"' if deck_name else "deck:*"
    note_ids = _invoke("findNotes", query=query)
    if not note_ids:
        return []
    notes_info = _invoke("notesInfo", notes=note_ids)
    words = []
    for note in notes_info:
        fields = note.get("fields", {})
        for field_name in ("Front", "Hanzi", "Chinese", "Word", "Simplified"):
            if field_name in fields:
                value = fields[field_name]["value"].strip()
                if value:
                    words.append(value)
                break
    return words


def add_note(deck_name: str, word: str, pinyin: str, definition: str, model_name: str = "Basic"):
    note = {
        "deckName": deck_name,
        "modelName": model_name,
        "fields": {
            "Front": word,
            "Back": f"{pinyin}<br>{definition}",
        },
        "options": {"allowDuplicate": False},
        "tags": ["chinese-tracker"],
    }
    return _invoke("addNote", note=note)
