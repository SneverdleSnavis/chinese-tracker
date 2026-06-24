import base64
import requests

ANKI_CONNECT_URL = "http://127.0.0.1:8765"
TRACKER_TAG = "chinese-tracker"


class AnkiConnectError(Exception):
    pass


def _invoke(action: str, **params):
    payload = {"action": action, "version": 6, "params": params}
    resp = requests.post(ANKI_CONNECT_URL, json=payload, timeout=10)
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


def sync():
    """Trigger desktop Anki to sync with AnkiWeb, so new cards reach the phone."""
    return _invoke("sync")


# ---------- Field helpers ----------

# Field names we treat as "the hanzi" when reading notes from arbitrary decks.
_HANZI_FIELDS = ("Front", "Hanzi", "Chinese", "Word", "Simplified")


def _note_hanzi(fields: dict) -> str:
    for name in _HANZI_FIELDS:
        if name in fields:
            return fields[name]["value"].strip()
    return ""


def store_media(filename: str, data: bytes):
    """Save audio (or any media) into Anki's collection.media so it syncs to
    AnkiWeb / the phone. Idempotent for a given filename (overwrites)."""
    return _invoke("storeMediaFile", filename=filename, data=base64.b64encode(data).decode())


def _build_back(pinyin: str, definition: str, example: str | None, audio: str | None = None) -> str:
    # Definitions may span multiple readings/senses (newline-separated); render
    # each on its own line in the card.
    definition = (definition or "").replace("\n", "<br>")
    back = f"{pinyin}<br>{definition}".strip()
    if audio:
        back += f"<br>{audio}"  # [sound:...] tag — plays when the answer is shown
    if example:
        back += (
            '<hr><div style="color:#888;font-size:0.85em;margin-top:6px">'
            f"{example}</div>"
        )
    return back


def get_all_known_words(deck_name: str | None = None):
    """Pulls hanzi text from all notes (optionally in one deck) to seed
    known-word status from an existing Anki collection."""
    query = f'deck:"{deck_name}"' if deck_name else "deck:*"
    note_ids = _invoke("findNotes", query=query)
    if not note_ids:
        return []
    notes_info = _invoke("notesInfo", notes=note_ids)
    words = []
    for note in notes_info:
        value = _note_hanzi(note.get("fields", {}))
        if value:
            words.append(value)
    return words


def find_tracker_note_id(word: str):
    """Return the note id of an existing chinese-tracker card for `word`, or None.
    Matches on the Front field so re-syncing updates instead of duplicating."""
    query = f'tag:{TRACKER_TAG} Front:"{word}"'
    ids = _invoke("findNotes", query=query)
    return ids[0] if ids else None


def add_note(deck_name: str, word: str, pinyin: str, definition: str,
             example: str | None = None, audio: str | None = None,
             tags=None, model_name: str = "Basic"):
    note = {
        "deckName": deck_name,
        "modelName": model_name,
        "fields": {
            "Front": word,
            "Back": _build_back(pinyin, definition, example, audio),
        },
        "options": {"allowDuplicate": False},
        "tags": [TRACKER_TAG] + list(tags or []),
    }
    return _invoke("addNote", note=note)


def add_cloze_note(deck_name: str, text: str, extra: str = "",
                   audio: str | None = None, tags=None):
    """Create a Cloze note for sentence mining. `text` is the sentence with the
    target word wrapped as {{c1::...}}; `extra` (pinyin/definition) and any audio
    go in the 'Back Extra' field. Uses Anki's built-in Cloze note type."""
    back_extra = extra or ""
    if audio:
        back_extra = f"{back_extra}<br>{audio}".strip("<br>") if back_extra else audio
    note = {
        "deckName": deck_name,
        "modelName": "Cloze",
        "fields": {"Text": text, "Back Extra": back_extra},
        "options": {"allowDuplicate": False},
        "tags": [TRACKER_TAG, "mined"] + list(tags or []),
    }
    return _invoke("addNote", note=note)


def update_note(note_id: int, pinyin: str, definition: str,
                example: str | None = None, audio: str | None = None, tags=None):
    """Update an existing tracker note's Back field (and add any new tags)."""
    _invoke("updateNoteFields", note={
        "id": note_id,
        "fields": {"Back": _build_back(pinyin, definition, example, audio)},
    })
    if tags:
        _invoke("addTags", notes=[note_id], tags=" ".join(tags))


def get_tracker_maturity():
    """Return [{word, interval}] for every chinese-tracker card, where interval
    is the current spacing in days. Used to promote matured words to 'known'."""
    card_ids = _invoke("findCards", query=f"tag:{TRACKER_TAG}")
    if not card_ids:
        return []
    cards = _invoke("cardsInfo", cards=card_ids)
    out = []
    for c in cards:
        word = _note_hanzi(c.get("fields", {}))
        if word:
            out.append({"word": word, "interval": c.get("interval", 0)})
    return out
