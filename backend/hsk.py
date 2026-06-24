"""New HSK 3.0 (2021) vocabulary, loaded from data/hsk30.txt (word<TAB>band).
Bands 1–6 are the standard levels; band 7 represents the combined 7–9 "advanced"
tier. Source: tonghuikang/HSK-3.0-words-list (per-level lists), ~11,091 words."""
from pathlib import Path
from functools import lru_cache

HSK_PATH = Path(__file__).parent / "data" / "hsk30.txt"
BANDS = [1, 2, 3, 4, 5, 6, 7]
BAND_LABELS = {1: "1", 2: "2", 3: "3", 4: "4", 5: "5", 6: "6", 7: "7–9"}


@lru_cache(maxsize=1)
def _load():
    """Return (word -> band, band -> set(words)). If a word appears in more than
    one level, it's attributed to the lowest (earliest) band."""
    word_band = {}
    band_words = {b: set() for b in BANDS}
    if HSK_PATH.exists():
        with open(HSK_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n").rstrip("\r")
                if not line or "\t" not in line:
                    continue
                word, raw_band = line.split("\t", 1)
                try:
                    band = int(raw_band)
                except ValueError:
                    continue
                if band not in band_words:
                    continue
                prev = word_band.get(word)
                if prev is None:
                    word_band[word] = band
                    band_words[band].add(word)
                elif band < prev:
                    band_words[prev].discard(word)
                    word_band[word] = band
                    band_words[band].add(word)
    return word_band, band_words


def band(word: str):
    """The HSK band of a word, or None if it isn't on the list."""
    return _load()[0].get(word)


def band_word_sets():
    """{band: set(words)} for progress calculations."""
    return _load()[1]
