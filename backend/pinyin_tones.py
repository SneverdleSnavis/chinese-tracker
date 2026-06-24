"""Convert CC-CEDICT / numbered pinyin (e.g. "ni3 hao3", "lu:3") into the
diacritic form learners read ("nǐ hǎo", "lǚ"). Idempotent: input that already
lacks tone numbers is returned unchanged."""
import re

# tone 1..5 (5 = neutral, no mark) glyphs for each base vowel.
_TONE_MARKS = {
    "a": "āáǎàa",
    "e": "ēéěèe",
    "i": "īíǐìi",
    "o": "ōóǒòo",
    "u": "ūúǔùu",
    "ü": "ǖǘǚǜü",
}

_SYLLABLE_RE = re.compile(r"^[A-Za-zü:]+[1-5]?$")


def _place_tone(body: str, tone: int) -> str:
    """Apply the tone mark to the correct vowel per standard pinyin rules:
    a/e win; in 'ou' the o takes it; otherwise the last vowel."""
    if "a" in body:
        idx = body.index("a")
    elif "e" in body:
        idx = body.index("e")
    elif "ou" in body:
        idx = body.index("o")
    else:
        idx = next((i for i in range(len(body) - 1, -1, -1) if body[i] in "aeiouü"), None)
    if idx is None:
        return body
    vowel = body[idx]
    marked = _TONE_MARKS.get(vowel, vowel * 5)[tone - 1]
    return body[:idx] + marked + body[idx + 1:]


def _convert_syllable(syl: str) -> str:
    if not _SYLLABLE_RE.match(syl):
        return syl  # punctuation, middle dots, anything unexpected: leave as-is
    upper = syl[0].isupper()
    s = syl.lower().replace("u:", "ü").replace("v", "ü")
    m = re.match(r"^([a-zü]+?)([1-5]?)$", s)
    if not m:
        return syl
    body, tone = m.group(1), m.group(2)
    result = body if not tone or tone == "5" else _place_tone(body, int(tone))
    if upper and result:
        result = result[0].upper() + result[1:]
    return result


def numbered_to_diacritic(pinyin: str) -> str:
    if not pinyin:
        return pinyin
    return " ".join(_convert_syllable(tok) for tok in pinyin.split(" "))
