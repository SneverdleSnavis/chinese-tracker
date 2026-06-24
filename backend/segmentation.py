import jieba
from dictionary import lookup


def is_chinese(token: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in token)


def apply_override(kind: str, word: str):
    """Adjust jieba's dictionary so a user correction sticks globally.
    'merge' keeps `word` as a single token everywhere; 'split' forces `word`
    to break into its individual characters."""
    if kind == "merge":
        jieba.add_word(word)
    elif kind == "split":
        # Removing the word from jieba's dictionary makes it segment into its
        # individual characters instead, without perturbing neighbouring words.
        jieba.del_word(word)


def load_overrides(conn):
    """Apply all stored segmentation overrides to jieba (call at startup)."""
    rows = conn.execute("SELECT kind, word FROM segmentation_overrides").fetchall()
    for r in rows:
        apply_override(r["kind"], r["word"])


def segment(text: str):
    """Segments text into tokens, returning a list of dicts for each token
    with its surface form, whether it's Chinese, and dictionary entries if so."""
    tokens = []
    for tok in jieba.cut(text):
        if not tok.strip():
            tokens.append({"text": tok, "is_chinese": False, "entries": []})
            continue
        chinese = is_chinese(tok)
        entries = lookup(tok) if chinese else []
        tokens.append({"text": tok, "is_chinese": chinese, "entries": entries})
    return tokens
