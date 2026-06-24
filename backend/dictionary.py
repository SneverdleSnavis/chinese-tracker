import re
from pathlib import Path
from functools import lru_cache

from pinyin_tones import numbered_to_diacritic, convert_bracketed

CEDICT_PATH = Path(__file__).parent / "data" / "cedict.txt"

LINE_RE = re.compile(r"^(\S+)\s+(\S+)\s+\[(.*?)\]\s+/(.*)/$")

# Entries whose definition begins with one of these are "low value" senses the
# user doesn't want cluttering lookups: surnames, and variant/old-variant
# cross-references to another character. Filtered out unless they're all a word
# has. Add patterns here to exclude more systematically.
_NOISE_ENTRY_RE = re.compile(
    r"^\s*(surname\s"
    r"|(?:old|archaic|ancient)\s+variant\s+of\b"
    r"|variant\s+of\b)",
    re.IGNORECASE,
)


def _is_noise_entry(definition: str) -> bool:
    return bool(_NOISE_ENTRY_RE.match(definition or ""))


def filter_entries(entries):
    """Drop surname/variant noise entries, but keep them if that's all there is
    (so a word that's *only* a surname still shows something)."""
    kept = [e for e in entries if not _is_noise_entry(e.get("definition", ""))]
    return kept if kept else entries


@lru_cache(maxsize=1)
def load_dictionary():
    """Returns dict keyed by simplified word -> list of {pinyin, definition} entries."""
    entries = {}
    with open(CEDICT_PATH, encoding="utf-8") as f:
        for line in f:
            if line.startswith("#") or not line.strip():
                continue
            m = LINE_RE.match(line.strip())
            if not m:
                continue
            traditional, simplified, pinyin, definition = m.groups()
            defs = [d for d in definition.split("/") if d]
            entry = {
                "pinyin": numbered_to_diacritic(pinyin),
                "definition": convert_bracketed("; ".join(defs)),
            }
            entries.setdefault(simplified, []).append(entry)
            if traditional != simplified:
                entries.setdefault(traditional, []).append(entry)
    return entries


def lookup(word: str):
    d = load_dictionary()
    return filter_entries(d.get(word, []))
