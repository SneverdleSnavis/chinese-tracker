import re
from pathlib import Path
from functools import lru_cache

from pinyin_tones import numbered_to_diacritic

CEDICT_PATH = Path(__file__).parent / "data" / "cedict.txt"

LINE_RE = re.compile(r"^(\S+)\s+(\S+)\s+\[(.*?)\]\s+/(.*)/$")


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
            entry = {"pinyin": numbered_to_diacritic(pinyin), "definition": "; ".join(defs)}
            entries.setdefault(simplified, []).append(entry)
            if traditional != simplified:
                entries.setdefault(traditional, []).append(entry)
    return entries


def lookup(word: str):
    d = load_dictionary()
    return d.get(word, [])
