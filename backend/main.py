import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

import anki
import database
import fetchers
import hsk
import subtitles
import tts
from database import db
from normalize import to_simplified
from segmentation import segment, apply_override, load_overrides

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

app = FastAPI(title="Chinese Learning Tracker")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def now():
    return datetime.now(timezone.utc).isoformat()


@app.middleware("http")
async def revalidate_static(request, call_next):
    """Tell browsers to revalidate /static assets each load so JS/CSS changes
    show up immediately instead of being served stale from cache."""
    response = await call_next(request)
    if request.url.path.startswith("/static"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.on_event("startup")
def startup():
    database.init_db()
    database.backup_db()
    with db() as conn:
        load_overrides(conn)


# ---------- Words ----------

class WordStatusUpdate(BaseModel):
    word: str
    status: str  # unknown | learning | known


@app.get("/api/words")
def list_words(
    q: str | None = None,
    status: str | None = None,
    synced: str | None = None,   # 'yes' = in Anki, 'no' = not yet
    custom: str | None = None,   # 'yes' = has a user override
    sort: str = "recent",        # recent | seen | word
    limit: int = 100,
    offset: int = 0,
):
    """Search/filter the full word list. Returns {total, words} where each word
    also carries has_custom (whether the user has overridden its definition)."""
    clauses, params = [], []
    if status in ("unknown", "learning", "known"):
        clauses.append("w.status = ?")
        params.append(status)
    if synced == "yes":
        clauses.append("w.exported_at IS NOT NULL")
    elif synced == "no":
        clauses.append("w.exported_at IS NULL")
    if custom == "yes":
        clauses.append("cd.word IS NOT NULL")
    elif custom == "no":
        clauses.append("cd.word IS NULL")
    if q:
        clauses.append("(w.word LIKE ? OR w.pinyin LIKE ? OR w.definition LIKE ?)")
        like = f"%{q}%"
        params += [like, like, like]
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    order = {
        "recent": "w.last_seen DESC",
        "seen": "w.seen_count DESC",
        "word": "w.word ASC",
    }.get(sort, "w.last_seen DESC")

    base = "FROM words w LEFT JOIN custom_definitions cd ON cd.word = w.word"
    with db() as conn:
        total = conn.execute(f"SELECT COUNT(*) {base} {where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT w.*, (cd.word IS NOT NULL) AS has_custom {base} {where} "
            f"ORDER BY {order} LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
    return {"total": total, "words": [dict(r) for r in rows]}


@app.get("/api/words/export")
def export_words():
    """Download all words as CSV — a portable backup of the data only you generated."""
    import csv
    import io
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["word", "status", "pinyin", "definition", "seen_count", "first_seen", "last_seen", "status_updated", "in_anki"])
    with db() as conn:
        rows = conn.execute(
            "SELECT word, status, pinyin, definition, seen_count, first_seen, last_seen, status_updated, "
            "CASE WHEN exported_at IS NOT NULL THEN 'yes' ELSE 'no' END AS in_anki "
            "FROM words ORDER BY status, word"
        ).fetchall()
        for r in rows:
            writer.writerow([r[k] for k in r.keys()])
    stamp = datetime.now().strftime("%Y%m%d")
    return Response(
        content=buf.getvalue().encode("utf-8-sig"),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="words_{stamp}.csv"'},
    )


@app.get("/api/words/{word}")
def get_word(word: str):
    with db() as conn:
        row = conn.execute("SELECT * FROM words WHERE word = ?", (word,)).fetchone()
        return dict(row) if row else {"word": word, "status": "unknown", "seen_count": 0}


@app.post("/api/words/status")
def set_word_status(body: WordStatusUpdate):
    if body.status not in ("unknown", "learning", "known"):
        raise HTTPException(400, "invalid status")
    ts = now()
    with db() as conn:
        existing = conn.execute(
            "SELECT word, pinyin, definition FROM words WHERE word = ?", (body.word,)
        ).fetchone()
        if existing:
            # Backfill pinyin/definition if the row is missing them (e.g. it was
            # created by an earlier status-only mark), so the Words tab and any
            # Anki sync have the data.
            if not (existing["pinyin"] and existing["definition"]):
                py, defn, _ = _effective_entry(conn, body.word)
                conn.execute(
                    "UPDATE words SET status = ?, status_updated = ?, "
                    "pinyin = CASE WHEN pinyin IS NULL OR pinyin = '' THEN ? ELSE pinyin END, "
                    "definition = CASE WHEN definition IS NULL OR definition = '' THEN ? ELSE definition END "
                    "WHERE word = ?",
                    (body.status, ts, py, defn, body.word),
                )
            else:
                conn.execute(
                    "UPDATE words SET status = ?, status_updated = ? WHERE word = ?",
                    (body.status, ts, body.word),
                )
        else:
            # New word: fetch pinyin/definition from the dictionary (or a custom
            # override) up front so it's never stored blank.
            py, defn, _ = _effective_entry(conn, body.word)
            conn.execute(
                "INSERT INTO words (word, status, pinyin, definition, seen_count, first_seen, last_seen, status_updated) "
                "VALUES (?, ?, ?, ?, 0, ?, ?, ?)",
                (body.word, body.status, py, defn, ts, ts, ts),
            )
    return {"ok": True}


def _effective_entry(conn, word: str):
    """Return (pinyin, definition, source) using the same precedence as the
    reader: a user override wins, then CC-CEDICT, then the stored words row."""
    custom = _custom_defs(conn, {word})
    if word in custom:
        return custom[word]["pinyin"], custom[word]["definition"], "custom"
    from dictionary import lookup as cedict_lookup
    entries = cedict_lookup(word)
    if entries:
        py, defn = _format_entries(entries)
        return py, defn, "cedict"
    row = conn.execute("SELECT pinyin, definition FROM words WHERE word = ?", (word,)).fetchone()
    if row:
        return row["pinyin"] or "", row["definition"] or "", "words"
    return "", "", "none"


class WordEdit(BaseModel):
    pinyin: str | None = None
    definition: str | None = None
    status: str | None = None


@app.post("/api/words/{word}")
def edit_word(word: str, body: WordEdit):
    """Edit a word's pinyin/definition/status. A pinyin or definition change is
    stored as an authoritative override (custom_definitions) so it appears
    consistently in every future text, and is mirrored onto the words row."""
    word = to_simplified(word.strip())
    if not word:
        raise HTTPException(400, "word is required")
    if body.status is not None and body.status not in ("unknown", "learning", "known"):
        raise HTTPException(400, "invalid status")
    ts = now()
    with db() as conn:
        cur_py, cur_def, _ = _effective_entry(conn, word)
        new_py = body.pinyin.strip() if body.pinyin is not None else cur_py
        new_def = body.definition.strip() if body.definition is not None else cur_def

        if body.pinyin is not None or body.definition is not None:
            if not new_def:
                raise HTTPException(400, "definition cannot be empty")
            conn.execute(
                "INSERT INTO custom_definitions (word, pinyin, definition, created_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(word) DO UPDATE SET pinyin=excluded.pinyin, definition=excluded.definition",
                (word, new_py, new_def, ts),
            )

        existing = conn.execute("SELECT word FROM words WHERE word = ?", (word,)).fetchone()
        if existing:
            if body.status is not None:
                conn.execute(
                    "UPDATE words SET pinyin = ?, definition = ?, status = ?, status_updated = ? WHERE word = ?",
                    (new_py, new_def, body.status, ts, word),
                )
            else:
                conn.execute(
                    "UPDATE words SET pinyin = ?, definition = ? WHERE word = ?",
                    (new_py, new_def, word),
                )
        else:
            conn.execute(
                "INSERT INTO words (word, status, pinyin, definition, seen_count, first_seen, last_seen, status_updated) "
                "VALUES (?, ?, ?, ?, 0, ?, ?, ?)",
                (word, body.status or "unknown", new_py, new_def, ts, ts, ts),
            )
    return {"ok": True, "word": word, "pinyin": new_py, "definition": new_def, "status": body.status}


@app.delete("/api/words/{word}")
def delete_word(word: str):
    """Remove a word entirely from the local tracker (word row, any custom
    override, and its lookup history). Does not touch an existing Anki card."""
    word = to_simplified(word.strip())
    with db() as conn:
        conn.execute("DELETE FROM words WHERE word = ?", (word,))
        conn.execute("DELETE FROM custom_definitions WHERE word = ?", (word,))
        conn.execute("DELETE FROM lookups WHERE word = ?", (word,))
    return {"ok": True, "word": word}


@app.delete("/api/words/{word}/custom")
def revert_word(word: str):
    """Remove a user override, reverting the word to its CC-CEDICT definition."""
    word = to_simplified(word.strip())
    with db() as conn:
        conn.execute("DELETE FROM custom_definitions WHERE word = ?", (word,))
        from dictionary import lookup as cedict_lookup
        entries = cedict_lookup(word)
        if entries:
            py, defn = _format_entries(entries)
            conn.execute(
                "UPDATE words SET pinyin = ?, definition = ? WHERE word = ?",
                (py, defn, word),
            )
        py, defn, source = _effective_entry(conn, word)
    return {"ok": True, "word": word, "pinyin": py, "definition": defn, "source": source}


def _format_entries(entries):
    """Combine ALL dictionary entries for a word into a single (pinyin, definition)
    pair for storage/display, mirroring the reader popup. Multiple readings/senses
    are listed one per line as 'pinyin — definition'; a single entry is kept plain."""
    if not entries:
        return "", ""
    seen, uniq = set(), []
    for e in entries:
        key = (e.get("pinyin", ""), e.get("definition", ""))
        if key not in seen:
            seen.add(key)
            uniq.append(e)
    readings = []
    for e in uniq:
        if e.get("pinyin") and e["pinyin"] not in readings:
            readings.append(e["pinyin"])
    pinyin = " / ".join(readings)
    if len(uniq) == 1:
        return pinyin, uniq[0].get("definition", "")
    definition = "\n".join(f"{e.get('pinyin', '')} — {e.get('definition', '')}" for e in uniq)
    return pinyin, definition


def _record_text_occurrences(conn, text_id: int, content: str):
    """Fold a text's word occurrences into words.seen_count ONCE, when the text
    is added. seen_count is therefore the cumulative number of times a word has
    appeared across all texts — it is NOT re-counted when a text is re-opened,
    and NOT decremented when a text is deleted (the exposure already happened).
    text_word_counts records each text's contribution so re-counting a
    re-segmented text replaces its old contribution instead of stacking."""
    from collections import Counter
    counts = Counter(t["text"] for t in segment(content) if t["is_chinese"])
    ts = now()
    for word, count in counts.items():
        prev = conn.execute(
            "SELECT count FROM text_word_counts WHERE text_id = ? AND word = ?",
            (text_id, word),
        ).fetchone()
        delta = count - (prev["count"] if prev else 0)
        if delta == 0:
            continue
        conn.execute(
            "INSERT INTO text_word_counts (text_id, word, count) VALUES (?, ?, ?) "
            "ON CONFLICT(text_id, word) DO UPDATE SET count = excluded.count",
            (text_id, word, count),
        )
        existing = conn.execute("SELECT word FROM words WHERE word = ?", (word,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE words SET seen_count = seen_count + ?, last_seen = ? WHERE word = ?",
                (delta, ts, word),
            )
        else:
            pinyin, definition, _ = _effective_entry(conn, word)
            conn.execute(
                "INSERT INTO words (word, status, pinyin, definition, seen_count, first_seen, last_seen) "
                "VALUES (?, 'unknown', ?, ?, ?, ?, ?)",
                (word, pinyin, definition, count, ts, ts),
            )


def backfill_word_entries(conn):
    """Fill in pinyin/definition for any word row that's missing them but has a
    dictionary (or custom) entry available. Fixes words that were created by a
    status-only mark before that data was fetched. Returns the count updated."""
    rows = conn.execute(
        "SELECT word FROM words WHERE pinyin IS NULL OR pinyin = '' OR definition IS NULL OR definition = ''"
    ).fetchall()
    updated = 0
    for r in rows:
        py, defn, _ = _effective_entry(conn, r["word"])
        if py or defn:
            conn.execute(
                "UPDATE words SET "
                "pinyin = CASE WHEN pinyin IS NULL OR pinyin = '' THEN ? ELSE pinyin END, "
                "definition = CASE WHEN definition IS NULL OR definition = '' THEN ? ELSE definition END "
                "WHERE word = ?",
                (py, defn, r["word"]),
            )
            updated += 1
    return updated


def rebuild_seen_counts(conn):
    """Recompute every word's seen_count from the texts currently stored. Zeroes
    the per-text contributions and seen_count, then re-counts each text. Used as
    a one-time migration off the old count-on-every-open behaviour, and safe to
    run again any time the totals look off."""
    load_overrides(conn)
    conn.execute("DELETE FROM text_word_counts")
    conn.execute("UPDATE words SET seen_count = 0")
    for r in conn.execute("SELECT id, content FROM texts").fetchall():
        _record_text_occurrences(conn, r["id"], r["content"])


# ---------- Texts / Reading ----------

class TextCreate(BaseModel):
    title: str
    content: str
    source: str | None = None


@app.post("/api/texts")
def create_text(body: TextCreate):
    title = to_simplified(body.title)
    content = to_simplified(body.content)
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO texts (title, source, content, created_at) VALUES (?, ?, ?, ?)",
            (title, body.source, content, now()),
        )
        _record_text_occurrences(conn, cur.lastrowid, content)
        return {"id": cur.lastrowid}


@app.get("/api/texts")
def list_texts():
    with db() as conn:
        rows = conn.execute(
            "SELECT id, title, source, content, created_at, last_read_at, kind, video_url, "
            "completion FROM texts ORDER BY created_at DESC"
        ).fetchall()
        result = []
        for r in rows:
            item = dict(r)
            content = item.pop("content")
            item["length"] = len(content)
            item["difficulty"] = _score_difficulty(conn, content)
            result.append(item)
        return result


class CompletionUpdate(BaseModel):
    completion: str  # unread | reading | read


@app.post("/api/texts/{text_id}/completion")
def set_text_completion(text_id: int, body: CompletionUpdate):
    if body.completion not in ("unread", "reading", "read"):
        raise HTTPException(400, "invalid completion")
    with db() as conn:
        conn.execute("UPDATE texts SET completion = ? WHERE id = ?", (body.completion, text_id))
    return {"ok": True}


def _custom_defs(conn, words):
    """Return {word: {pinyin, definition, source:'custom'}} for any words that
    have a user-supplied definition."""
    if not words:
        return {}
    placeholders = ",".join("?" * len(words))
    rows = conn.execute(
        f"SELECT word, pinyin, definition FROM custom_definitions WHERE word IN ({placeholders})",
        tuple(words),
    ).fetchall()
    return {r["word"]: {"pinyin": r["pinyin"] or "", "definition": r["definition"], "source": "custom"} for r in rows}


def _tokenize_with_status(conn, content: str):
    """Segment text and attach each Chinese token's known/learning/unknown status
    plus its definition. Shared by article and subtitle reading. Does NOT touch
    seen-counts — those are recorded once when a text is added (see
    _record_text_occurrences), so re-opening a text never inflates them."""
    tokens = segment(content)
    chinese_words = {t["text"] for t in tokens if t["is_chinese"]}
    words_status = {}
    if chinese_words:
        placeholders = ",".join("?" * len(chinese_words))
        status_rows = conn.execute(
            f"SELECT word, status FROM words WHERE word IN ({placeholders})",
            tuple(chinese_words),
        ).fetchall()
        words_status = {r["word"]: r["status"] for r in status_rows}

    # User-supplied definitions take precedence over CC-CEDICT, so a correction
    # (or a definition for a word CC-CEDICT misses) shows up consistently.
    custom = _custom_defs(conn, chinese_words)

    for tok in tokens:
        if tok["is_chinese"]:
            tok["status"] = words_status.get(tok["text"], "unknown")
            if tok["text"] in custom:
                tok["entries"] = [custom[tok["text"]]]
    return tokens


@app.get("/api/texts/{text_id}")
def get_text(text_id: int):
    with db() as conn:
        row = conn.execute("SELECT * FROM texts WHERE id = ?", (text_id,)).fetchone()
        if not row:
            raise HTTPException(404, "text not found")
        conn.execute("UPDATE texts SET last_read_at = ? WHERE id = ?", (now(), text_id))
        if row["completion"] == "unread":
            conn.execute("UPDATE texts SET completion = 'reading' WHERE id = ?", (text_id,))
        text = dict(row)

        if text.get("kind") == "subtitle":
            line_rows = conn.execute(
                "SELECT idx, start_ms, end_ms, content FROM subtitle_lines "
                "WHERE text_id = ? ORDER BY idx",
                (text_id,),
            ).fetchall()
            lines = []
            for lr in line_rows:
                lines.append({
                    "start_ms": lr["start_ms"],
                    "end_ms": lr["end_ms"],
                    "tokens": _tokenize_with_status(conn, lr["content"]),
                })
            text["lines"] = lines
        else:
            text["tokens"] = _tokenize_with_status(conn, text["content"])
        return text


@app.delete("/api/texts/{text_id}")
def delete_text(text_id: int):
    with db() as conn:
        conn.execute("DELETE FROM subtitle_lines WHERE text_id = ?", (text_id,))
        conn.execute("DELETE FROM lookups WHERE text_id = ?", (text_id,))
        conn.execute("DELETE FROM texts WHERE id = ?", (text_id,))
    return {"ok": True}


def _save_subtitle_text(conn, title, cues, source=None, video_url=None):
    """Persist a subtitle import: one texts row (kind='subtitle') plus a
    subtitle_lines row per cue. `content` holds the joined text so difficulty
    scoring and full-text features keep working unchanged."""
    title = to_simplified(title)
    joined = "\n".join(to_simplified(c["text"]) for c in cues)
    cur = conn.execute(
        "INSERT INTO texts (title, source, content, created_at, kind, video_url) "
        "VALUES (?, ?, ?, ?, 'subtitle', ?)",
        (title, source, joined, now(), video_url),
    )
    text_id = cur.lastrowid
    for i, c in enumerate(cues):
        conn.execute(
            "INSERT INTO subtitle_lines (text_id, idx, start_ms, end_ms, content) "
            "VALUES (?, ?, ?, ?, ?)",
            (text_id, i, c.get("start_ms"), c.get("end_ms"), to_simplified(c["text"])),
        )
    _record_text_occurrences(conn, text_id, joined)
    return text_id


@app.post("/api/subtitles/upload")
async def upload_subtitle(file: UploadFile = File(...), title: str = Form(None)):
    raw = (await file.read()).decode("utf-8", errors="replace")
    cues = subtitles.parse_subtitles(raw)
    if not cues:
        raise HTTPException(400, "No subtitle cues could be parsed from that file. Expected SRT or VTT format.")
    display_title = title or (file.filename or "Subtitle").rsplit(".", 1)[0]
    with db() as conn:
        text_id = _save_subtitle_text(conn, display_title, cues, source=file.filename)
    return {"id": text_id, "lines": len(cues)}


def _chinese_count(text: str) -> int:
    return sum(1 for ch in text if "一" <= ch <= "鿿")


def _parse_epub(raw: bytes, fallback_title: str):
    """Return [(chapter_title, text)] for an EPUB — one entry per document item
    that holds a meaningful amount of Chinese text (skips covers/TOC/colophon)."""
    import ebooklib
    from ebooklib import epub
    from bs4 import BeautifulSoup
    import tempfile, os

    with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as tf:
        tf.write(raw)
        path = tf.name
    try:
        book = epub.read_epub(path)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

    meta = book.get_metadata("DC", "title")
    book_title = (meta[0][0] if meta else "") or fallback_title
    chapters = []
    idx = 0
    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        soup = BeautifulSoup(item.get_content(), "lxml")
        text = soup.get_text("\n").strip()
        if _chinese_count(text) < 100:
            continue
        idx += 1
        heading = soup.find(["h1", "h2", "h3"])
        ch_title = heading.get_text().strip() if heading and heading.get_text().strip() else f"Chapter {idx}"
        chapters.append((f"{book_title} — {ch_title}", text))
    return chapters


@app.post("/api/books/upload")
async def upload_book(file: UploadFile = File(...), title: str = Form(None)):
    """Import a book as reader text(s). A .txt becomes one text; an .epub is split
    into one text per chapter so the reader stays responsive."""
    raw = await file.read()
    name = file.filename or "book"
    base_title = (title or name.rsplit(".", 1)[0]).strip()
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""

    if ext == "txt":
        chapters = [(base_title, raw.decode("utf-8", errors="replace"))]
    elif ext == "epub":
        try:
            chapters = _parse_epub(raw, base_title)
        except Exception as e:
            raise HTTPException(400, f"Couldn't read that EPUB: {e}")
    else:
        raise HTTPException(400, "Upload a .epub or .txt file.")

    created = []
    with db() as conn:
        for ch_title, content in chapters:
            content = to_simplified(content).strip()
            if _chinese_count(content) < 20:
                continue
            cur = conn.execute(
                "INSERT INTO texts (title, source, content, created_at) VALUES (?, ?, ?, ?)",
                (to_simplified(ch_title), name, content, now()),
            )
            _record_text_occurrences(conn, cur.lastrowid, content)
            created.append({"id": cur.lastrowid, "title": to_simplified(ch_title)})
    if not created:
        raise HTTPException(400, "No Chinese text found in that file.")
    return {"count": len(created), "texts": created}


class YoutubeImport(BaseModel):
    url: str


@app.post("/api/subtitles/video")
@app.post("/api/subtitles/youtube")  # backward-compatible alias
def import_video(body: YoutubeImport):
    """Import Chinese captions from a YouTube or Bilibili video."""
    try:
        title, video_url, cues = subtitles.fetch_video_subtitles(body.url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"Failed to fetch captions: {e}")
    with db() as conn:
        text_id = _save_subtitle_text(conn, title, cues, source=video_url, video_url=video_url)
    return {"id": text_id, "lines": len(cues), "title": title}


class LookupLog(BaseModel):
    word: str
    text_id: int | None = None


@app.post("/api/lookups")
def log_lookup(body: LookupLog):
    with db() as conn:
        conn.execute(
            "INSERT INTO lookups (word, text_id, looked_up_at) VALUES (?, ?, ?)",
            (body.word, body.text_id, now()),
        )
    return {"ok": True}


# ---------- Dictionary lookup & custom definitions ----------

def _suggest_pinyin(word: str) -> str:
    """Generate diacritic pinyin (nǐ hǎo) for a word jieba/CC-CEDICT can't
    define, so the user only has to fill in the meaning."""
    from pypinyin import pinyin, Style
    parts = pinyin(word, style=Style.TONE)
    return " ".join(p[0] for p in parts)


@app.get("/api/lookup")
def lookup_word(word: str):
    """Look up an arbitrary word (used when splitting/merging tokens). Returns
    CC-CEDICT entries, falling back to a custom definition, plus the word's
    current status and a suggested pinyin if undefined."""
    from dictionary import lookup as cedict_lookup
    entries = cedict_lookup(word)
    with db() as conn:
        custom = _custom_defs(conn, {word})
        if word in custom:  # a user edit overrides CC-CEDICT
            entries = [custom[word]]
        status_row = conn.execute("SELECT status FROM words WHERE word = ?", (word,)).fetchone()
    status = status_row["status"] if status_row else "unknown"
    return {
        "word": word,
        "entries": entries,
        "status": status,
        "suggested_pinyin": "" if entries else _suggest_pinyin(word),
    }


class CustomDefinition(BaseModel):
    word: str
    pinyin: str | None = None
    definition: str


@app.post("/api/custom-definition")
def set_custom_definition(body: CustomDefinition):
    word = to_simplified(body.word.strip())
    definition = body.definition.strip()
    if not word or not definition:
        raise HTTPException(400, "word and definition are required")
    pinyin = (body.pinyin or "").strip() or _suggest_pinyin(word)
    with db() as conn:
        conn.execute(
            "INSERT INTO custom_definitions (word, pinyin, definition, created_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(word) DO UPDATE SET pinyin=excluded.pinyin, definition=excluded.definition",
            (word, pinyin, definition, now()),
        )
        # Reflect onto the words row so dashboards/exports show the definition too.
        conn.execute(
            "UPDATE words SET pinyin = COALESCE(NULLIF(pinyin,''), ?), definition = ? WHERE word = ?",
            (pinyin, definition, word),
        )
    return {"ok": True, "pinyin": pinyin, "entries": [{"pinyin": pinyin, "definition": definition, "source": "custom"}]}


# ---------- Segmentation overrides (split / merge) ----------

class SegmentationOverride(BaseModel):
    action: str            # 'split' | 'merge'
    word: str              # the token being edited
    next_word: str | None = None  # for 'merge', the following token to join


@app.post("/api/segmentation/override")
def segmentation_override(body: SegmentationOverride):
    if body.action == "split":
        kind, word = "split", to_simplified(body.word.strip())
        new_word = word[0] if word else ""
    elif body.action == "merge":
        if not body.next_word:
            raise HTTPException(400, "merge requires next_word")
        kind = "merge"
        word = to_simplified(body.word.strip()) + to_simplified(body.next_word.strip())
        new_word = word
    else:
        raise HTTPException(400, "action must be 'split' or 'merge'")

    if not word:
        raise HTTPException(400, "word is required")

    apply_override(kind, word)
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO segmentation_overrides (kind, word, created_at) VALUES (?, ?, ?)",
            (kind, word, now()),
        )
    return {"ok": True, "new_word": new_word}


# ---------- Fetchers ----------

def _score_difficulty(conn, content: str):
    tokens = segment(content)
    chinese_words = [t["text"] for t in tokens if t["is_chinese"]]
    if not chinese_words:
        return {"known_pct": 0, "learning_pct": 0, "unknown_pct": 0, "total_words": 0}

    unique_words = set(chinese_words)
    placeholders = ",".join("?" * len(unique_words))
    rows = conn.execute(
        f"SELECT word, status FROM words WHERE word IN ({placeholders})", tuple(unique_words)
    ).fetchall()
    status_by_word = {r["word"]: r["status"] for r in rows}

    counts = {"known": 0, "learning": 0, "unknown": 0}
    for w in chinese_words:
        counts[status_by_word.get(w, "unknown")] += 1

    total = len(chinese_words)
    return {
        "known_pct": round(100 * counts["known"] / total),
        "learning_pct": round(100 * counts["learning"] / total),
        "unknown_pct": round(100 * counts["unknown"] / total),
        "total_words": total,
    }


@app.get("/api/fetch/sources")
def fetch_sources():
    return [{"id": s["id"], "name": s["name"], "difficulty_hint": s["difficulty_hint"]} for s in fetchers.SOURCES]


class FetchRun(BaseModel):
    source_ids: list[str]
    limit: int = 5


@app.post("/api/fetch/run")
def fetch_run(body: FetchRun):
    with db() as conn:
        existing_links = {
            r["source"] for r in conn.execute("SELECT source FROM texts WHERE source IS NOT NULL").fetchall()
        }

        results = []
        errors = []
        for source_id in body.source_ids:
            try:
                candidates = fetchers.fetch_candidates(
                    source_id, limit=body.limit, exclude_links=existing_links
                )
            except Exception as e:
                errors.append({"source_id": source_id, "error": str(e)})
                continue
            for c in candidates:
                c["title"] = to_simplified(c["title"])
                c["content"] = to_simplified(c["content"])
                difficulty = _score_difficulty(conn, c["content"])
                results.append({**c, "difficulty": difficulty})

        return {"candidates": results, "errors": errors}


# ---------- Analytics ----------

@app.get("/api/analytics/summary")
def analytics_summary():
    with db() as conn:
        counts = conn.execute(
            "SELECT status, COUNT(*) as c FROM words GROUP BY status"
        ).fetchall()
        status_counts = {r["status"]: r["c"] for r in counts}
        total_texts = conn.execute("SELECT COUNT(*) as c FROM texts").fetchone()["c"]
        total_lookups = conn.execute("SELECT COUNT(*) as c FROM lookups").fetchone()["c"]
        recent_words = conn.execute(
            "SELECT word, status, last_seen FROM words ORDER BY last_seen DESC LIMIT 20"
        ).fetchall()
        lookups_by_day = conn.execute(
            "SELECT substr(looked_up_at, 1, 10) as day, COUNT(*) as c "
            "FROM lookups GROUP BY day ORDER BY day DESC LIMIT 30"
        ).fetchall()
        return {
            "status_counts": status_counts,
            "total_texts": total_texts,
            "total_lookups": total_lookups,
            "recent_words": [dict(r) for r in recent_words],
            "lookups_by_day": [dict(r) for r in lookups_by_day],
        }


@app.get("/api/analytics/timeline")
def analytics_timeline(days: int = 371):
    """Data for the Progress dashboard: cumulative vocabulary growth, a per-day
    activity map (for a heatmap), and study streaks. All dates are UTC (matching
    how timestamps are stored)."""
    from datetime import datetime, timezone, date, timedelta

    def _cumulative(rows):
        out, total = [], 0
        for r in rows:
            if not r["d"]:
                continue
            total += r["c"]
            out.append({"date": r["d"], "value": total})
        return out

    with db() as conn:
        known_rows = conn.execute(
            "SELECT substr(status_updated,1,10) d, COUNT(*) c FROM words "
            "WHERE status='known' AND status_updated IS NOT NULL GROUP BY d ORDER BY d"
        ).fetchall()
        seen_rows = conn.execute(
            "SELECT substr(first_seen,1,10) d, COUNT(*) c FROM words "
            "WHERE first_seen IS NOT NULL GROUP BY d ORDER BY d"
        ).fetchall()
        # Activity = lookups + status changes, per day (intensity for the heatmap).
        activity = {}
        for r in conn.execute("SELECT substr(looked_up_at,1,10) d, COUNT(*) c FROM lookups GROUP BY d"):
            if r["d"]:
                activity[r["d"]] = activity.get(r["d"], 0) + r["c"]
        for r in conn.execute("SELECT substr(status_updated,1,10) d, COUNT(*) c FROM words WHERE status_updated IS NOT NULL GROUP BY d"):
            if r["d"]:
                activity[r["d"]] = activity.get(r["d"], 0) + r["c"]

    known_series = _cumulative(known_rows)
    seen_series = _cumulative(seen_rows)

    # Streaks over the set of active days.
    active = set(activity.keys())
    today = datetime.now(timezone.utc).date()
    longest = run = 0
    prev = None
    for d in sorted(active):
        cur = date.fromisoformat(d)
        run = run + 1 if (prev and (cur - prev).days == 1) else 1
        longest = max(longest, run)
        prev = cur
    anchor = today if today.isoformat() in active else today - timedelta(days=1)
    current = 0
    d = anchor
    while d.isoformat() in active:
        current += 1
        d -= timedelta(days=1)

    # Trim the activity map to the requested window for the heatmap.
    cutoff = (today - timedelta(days=days - 1)).isoformat()
    activity_window = {d: c for d, c in activity.items() if d >= cutoff}

    return {
        "today": today.isoformat(),
        "known_series": known_series,
        "seen_series": seen_series,
        "activity": activity_window,
        "streak": {
            "current": current,
            "longest": longest,
            "active_today": today.isoformat() in active,
            "total_active_days": len(active),
        },
    }


# ---------- Goals / targets ----------

# Each goal kind, with how to label it and whether it's a rolling-weekly target
# or a cumulative total. Progress is computed live in get_goals.
GOAL_DEFS = {
    "known_total":         {"label": "Total known words",  "unit": "words", "period": "total"},
    "known_per_week":      {"label": "New known words",    "unit": "words", "period": "week"},
    "texts_per_week":      {"label": "Texts added",        "unit": "texts", "period": "week"},
    "study_days_per_week": {"label": "Study days",         "unit": "days",  "period": "week"},
}


def _goal_current(conn, kind: str) -> int:
    from datetime import datetime, timezone, timedelta
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    if kind == "known_total":
        return conn.execute("SELECT COUNT(*) c FROM words WHERE status='known'").fetchone()["c"]
    if kind == "known_per_week":
        return conn.execute(
            "SELECT COUNT(*) c FROM words WHERE status='known' AND status_updated >= ?", (since,)
        ).fetchone()["c"]
    if kind == "texts_per_week":
        return conn.execute("SELECT COUNT(*) c FROM texts WHERE created_at >= ?", (since,)).fetchone()["c"]
    if kind == "study_days_per_week":
        days = set()
        for r in conn.execute("SELECT DISTINCT substr(looked_up_at,1,10) d FROM lookups WHERE looked_up_at >= ?", (since,)):
            if r["d"]:
                days.add(r["d"])
        for r in conn.execute("SELECT DISTINCT substr(status_updated,1,10) d FROM words WHERE status_updated >= ?", (since,)):
            if r["d"]:
                days.add(r["d"])
        return len(days)
    return 0


@app.get("/api/goals")
def get_goals():
    """Every goal kind with its target (if set) and live progress."""
    with db() as conn:
        set_rows = {r["kind"]: r["target"] for r in conn.execute("SELECT kind, target FROM goals").fetchall()}
        out = []
        for kind, meta in GOAL_DEFS.items():
            target = set_rows.get(kind)
            current = _goal_current(conn, kind)
            out.append({
                "kind": kind,
                "label": meta["label"],
                "unit": meta["unit"],
                "period": meta["period"],
                "target": target,
                "current": current,
                "pct": (min(current, target) / target * 100) if target else 0,
                "met": bool(target) and current >= target,
            })
    return {"goals": out}


class GoalSet(BaseModel):
    target: int


@app.put("/api/goals/{kind}")
def set_goal(kind: str, body: GoalSet):
    if kind not in GOAL_DEFS:
        raise HTTPException(404, "unknown goal kind")
    if body.target <= 0:
        raise HTTPException(400, "target must be positive")
    with db() as conn:
        conn.execute(
            "INSERT INTO goals (kind, target, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(kind) DO UPDATE SET target=excluded.target",
            (kind, body.target, now()),
        )
    return {"ok": True, "kind": kind, "target": body.target}


@app.delete("/api/goals/{kind}")
def clear_goal(kind: str):
    with db() as conn:
        conn.execute("DELETE FROM goals WHERE kind = ?", (kind,))
    return {"ok": True, "kind": kind}


# ---------- Learn next (frequency-ranked study list) ----------

def _coverage(conn):
    """Corpus coverage: what fraction of all word-instances across your texts you
    already know. T = total instances (sum of seen_count); known/learning count
    toward coverage. Frames how much comprehension each unknown word would add."""
    row = conn.execute(
        "SELECT COALESCE(SUM(seen_count),0) AS total, "
        "COALESCE(SUM(CASE WHEN status IN ('known','learning') THEN seen_count ELSE 0 END),0) AS known "
        "FROM words"
    ).fetchone()
    total, known = row["total"], row["known"]
    return {"total": total, "known_instances": known, "pct": (known / total * 100) if total else 0}


@app.get("/api/analytics/coverage")
def coverage():
    with db() as conn:
        return _coverage(conn)


@app.get("/api/learn/next")
def learn_next(scope: str = "all", text_id: int | None = None, limit: int = 25):
    """Highest-leverage words to study next, ranked by how often they appear.
    scope='all' ranks unknown words across every text by seen_count; scope='text'
    ranks a single text's unknown words by their in-text count, so you can prep
    before reading it. Each word carries its share of running text ('share')."""
    with db() as conn:
        if scope == "text":
            if text_id is None:
                raise HTTPException(400, "text_id is required for scope='text'")
            tot = conn.execute(
                "SELECT COALESCE(SUM(count),0) AS t FROM text_word_counts WHERE text_id = ?",
                (text_id,),
            ).fetchone()["t"]
            known = conn.execute(
                "SELECT COALESCE(SUM(twc.count),0) AS k FROM text_word_counts twc "
                "JOIN words w ON w.word = twc.word "
                "WHERE twc.text_id = ? AND w.status IN ('known','learning')",
                (text_id,),
            ).fetchone()["k"]
            rows = conn.execute(
                "SELECT twc.word AS word, twc.count AS freq FROM text_word_counts twc "
                "LEFT JOIN words w ON w.word = twc.word "
                "WHERE twc.text_id = ? AND COALESCE(w.status,'unknown') = 'unknown' "
                "ORDER BY twc.count DESC LIMIT ?",
                (text_id, limit),
            ).fetchall()
            denom = tot
            covered = known
        else:
            denom = _coverage(conn)["total"]
            covered = _coverage(conn)["known_instances"]
            rows = conn.execute(
                "SELECT word, seen_count AS freq FROM words "
                "WHERE status = 'unknown' AND seen_count > 0 "
                "ORDER BY seen_count DESC LIMIT ?",
                (limit,),
            ).fetchall()

        words = []
        for r in rows:
            py, defn, _ = _effective_entry(conn, r["word"])
            words.append({
                "word": r["word"],
                "freq": r["freq"],
                "pinyin": py,
                "definition": defn,
                "hsk": hsk.band(r["word"]),
                "share": (r["freq"] / denom * 100) if denom else 0,
            })
    return {
        "scope": scope,
        "total_instances": denom,
        "known_instances": covered,
        "coverage_pct": (covered / denom * 100) if denom else 0,
        "words": words,
    }


# ---------- HSK 3.0 milestones ----------

@app.get("/api/hsk/progress")
def hsk_progress():
    """Per-band completion: how many words in each HSK 3.0 level you've marked
    known (and learning), as a curriculum-based counterpart to corpus coverage."""
    sets = hsk.band_word_sets()
    with db() as conn:
        known = {r["word"] for r in conn.execute("SELECT word FROM words WHERE status = 'known'").fetchall()}
        learning = {r["word"] for r in conn.execute("SELECT word FROM words WHERE status = 'learning'").fetchall()}
    bands = []
    for b in hsk.BANDS:
        words = sets.get(b, set())
        total = len(words)
        k = len(words & known)
        bands.append({
            "band": b,
            "label": hsk.BAND_LABELS[b],
            "total": total,
            "known": k,
            "learning": len(words & learning),
            "pct": (k / total * 100) if total else 0,
        })
    total_all = sum(b["total"] for b in bands)
    known_all = sum(b["known"] for b in bands)
    return {"bands": bands, "total": total_all, "known": known_all,
            "pct": (known_all / total_all * 100) if total_all else 0}


@app.get("/api/hsk/missing")
def hsk_missing(band: int, limit: int = 100):
    """Words in a band you haven't marked known yet, frequency-ordered (by
    seen_count) so the most useful ones to learn surface first."""
    words = hsk.band_word_sets().get(band, set())
    with db() as conn:
        rows = {r["word"]: r for r in conn.execute("SELECT word, status, seen_count FROM words").fetchall()}
        missing = []
        for w in words:
            r = rows.get(w)
            status = r["status"] if r else "unknown"
            if status == "known":
                continue
            missing.append({"word": w, "status": status, "seen_count": r["seen_count"] if r else 0})
        total_missing = len(missing)
        missing.sort(key=lambda x: (-x["seen_count"], x["word"]))
        missing = missing[:limit]
        for m in missing:  # attach definitions only for the page-sized slice
            m["pinyin"], m["definition"], _ = _effective_entry(conn, m["word"])
    return {
        "band": band,
        "label": hsk.BAND_LABELS.get(band, str(band)),
        "missing": missing,
        "total_missing": total_missing,
    }


# ---------- Comprehension questions (multiple choice) ----------

class MCQuestion(BaseModel):
    question: str         # in simplified Chinese
    choices: list[str]    # 2+ answer options
    answer: int           # 0-based index of the correct choice


class MCQuestions(BaseModel):
    questions: list[MCQuestion]


def _text_plain_content(conn, text_id: int):
    """Return (title, plain_text) for a text — article body or joined subtitle lines."""
    row = conn.execute("SELECT title, content, kind FROM texts WHERE id = ?", (text_id,)).fetchone()
    if not row:
        return None, None
    if row["kind"] == "subtitle":
        lines = conn.execute(
            "SELECT content FROM subtitle_lines WHERE text_id = ? ORDER BY idx", (text_id,)
        ).fetchall()
        content = "\n".join(r["content"] for r in lines) or row["content"]
    else:
        content = row["content"]
    return row["title"], content


def _stored_questions(conn, text_id: int):
    """Return the saved multiple-choice questions for a text, ordered."""
    rows = conn.execute(
        "SELECT id, question, choices, answer FROM comprehension_questions "
        "WHERE text_id = ? ORDER BY idx",
        (text_id,),
    ).fetchall()
    import json
    return [
        {"id": r["id"], "question": r["question"], "choices": json.loads(r["choices"]), "answer": r["answer"]}
        for r in rows
    ]


def _save_questions(conn, text_id: int, questions):
    """Replace the stored questions for a text with `questions` (list of dicts
    with question/choices/answer). Validates shape and the answer index."""
    import json
    clean = []
    for q in questions:
        question = (q.get("question") or "").strip()
        choices = [str(c).strip() for c in (q.get("choices") or []) if str(c).strip()]
        answer = q.get("answer")
        if not question or len(choices) < 2:
            raise HTTPException(400, "each question needs a 'question' and at least 2 'choices'")
        if not isinstance(answer, int) or not (0 <= answer < len(choices)):
            raise HTTPException(400, f"'answer' must be a 0-based index into choices (0–{len(choices)-1})")
        clean.append({"question": question, "choices": choices, "answer": answer})
    ts = now()
    conn.execute("DELETE FROM comprehension_questions WHERE text_id = ?", (text_id,))
    for i, q in enumerate(clean):
        conn.execute(
            "INSERT INTO comprehension_questions (text_id, idx, question, choices, answer, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (text_id, i, q["question"], json.dumps(q["choices"], ensure_ascii=False), q["answer"], ts),
        )
    return clean


class QuestionsPayload(BaseModel):
    questions: list[dict]


@app.get("/api/texts/{text_id}/questions")
def get_questions(text_id: int):
    """Fetch the saved multiple-choice questions for a text."""
    with db() as conn:
        return {"questions": _stored_questions(conn, text_id)}


@app.put("/api/texts/{text_id}/questions")
def put_questions(text_id: int, body: QuestionsPayload):
    """Save (replace) the multiple-choice questions for a text — used by the
    paste-in flow. Expects a list of {question, choices, answer}."""
    with db() as conn:
        row = conn.execute("SELECT id FROM texts WHERE id = ?", (text_id,)).fetchone()
        if not row:
            raise HTTPException(404, "text not found")
        saved = _save_questions(conn, text_id, body.questions)
    return {"questions": saved}


@app.delete("/api/texts/{text_id}/questions")
def clear_questions(text_id: int):
    """Remove all saved questions for a text."""
    with db() as conn:
        conn.execute("DELETE FROM comprehension_questions WHERE text_id = ?", (text_id,))
    return {"ok": True}


@app.post("/api/texts/{text_id}/questions/generate")
def generate_questions(text_id: int):
    """Generate multiple-choice questions for a text via Claude and save them.
    Optional path — requires ANTHROPIC_API_KEY. The paste-in flow (PUT above)
    needs no key."""
    import os
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(503, "ANTHROPIC_API_KEY is not set. You can paste questions in instead — no key needed.")
    with db() as conn:
        title, content = _text_plain_content(conn, text_id)
    if content is None:
        raise HTTPException(404, "text not found")
    content = content.strip()
    if not content:
        raise HTTPException(400, "text has no content")

    import anthropic
    client = anthropic.Anthropic()
    system = (
        "You are a Mandarin Chinese reading-comprehension tutor. Given a Chinese text, "
        "write 4 multiple-choice questions in simplified Chinese that check whether a learner "
        "understood it — mixing literal recall and light inference. Each question has exactly 4 "
        "choices in simplified Chinese, with one correct. 'answer' is the 0-based index of the "
        "correct choice. Keep questions and choices short. Do not add pinyin or translations."
    )
    try:
        response = client.messages.parse(
            model="claude-opus-4-8",
            max_tokens=4000,
            system=system,
            messages=[{"role": "user", "content": f"Text title: {title}\n\nText:\n{content}"}],
            output_format=MCQuestions,
        )
    except anthropic.APIStatusError as e:
        raise HTTPException(502, f"Claude API error: {e.message}")
    parsed = response.parsed_output
    with db() as conn:
        saved = _save_questions(conn, text_id, [q.model_dump() for q in parsed.questions])
    return {"questions": saved}


# ---------- Anki integration ----------

@app.get("/api/anki/status")
def anki_status():
    available = anki.is_available()
    decks = anki.get_deck_names() if available else []
    return {"available": available, "decks": decks}


class AnkiImport(BaseModel):
    deck_name: str | None = None


@app.post("/api/anki/import-known")
def anki_import_known(body: AnkiImport):
    if not anki.is_available():
        raise HTTPException(503, "AnkiConnect not reachable. Is Anki running with the AnkiConnect add-on?")
    words = anki.get_all_known_words(body.deck_name)
    ts = now()
    imported = 0
    with db() as conn:
        for word in words:
            existing = conn.execute("SELECT word FROM words WHERE word = ?", (word,)).fetchone()
            if existing:
                conn.execute(
                    "UPDATE words SET status = 'known', status_updated = ? WHERE word = ?",
                    (ts, word),
                )
            else:
                conn.execute(
                    "INSERT INTO words (word, status, seen_count, first_seen, last_seen, status_updated) "
                    "VALUES (?, 'known', 0, ?, ?, ?)",
                    (word, ts, ts, ts),
                )
            imported += 1
    return {"imported": imported}


# A card whose Anki interval has grown to at least this many days is treated as
# genuinely learned, and its word is promoted to 'known' on the site.
MATURE_INTERVAL_DAYS = 21

# Sentence boundaries for pulling an example sentence out of a stored text.
_SENT_SPLIT = re.compile(r"[。！？!?\n；;]")


def _extract_sentence(content: str, word: str) -> str | None:
    """Find a sentence in `content` containing `word` and return it with the
    word bolded. Skips sentences that are too long to be a useful flashcard."""
    for sentence in _SENT_SPLIT.split(content):
        s = sentence.strip()
        if word in s and 2 <= len(s) <= 60:
            return s.replace(word, f"<b>{word}</b>")
    return None


def _word_context(conn, word: str):
    """Return (example_sentence, source_title) for where the user last met
    `word`, searching their articles first, then subtitle lines. Either value
    may be None if nothing suitable is found."""
    like = f"%{word}%"
    rows = conn.execute(
        "SELECT title, content FROM texts WHERE content LIKE ? "
        "ORDER BY COALESCE(last_read_at, created_at) DESC LIMIT 5",
        (like,),
    ).fetchall()
    for r in rows:
        s = _extract_sentence(r["content"], word)
        if s:
            return s, r["title"]
    row = conn.execute(
        "SELECT sl.content AS content, t.title AS title FROM subtitle_lines sl "
        "JOIN texts t ON t.id = sl.text_id WHERE sl.content LIKE ? LIMIT 1",
        (like,),
    ).fetchone()
    if row:
        return _extract_sentence(row["content"], word), row["title"]
    return None, None


def _slug_tag(prefix: str, value: str) -> str:
    """Turn a free-text title into a safe single-token Anki tag."""
    slug = re.sub(r"[^0-9A-Za-z一-鿿]+", "_", value).strip("_")[:30]
    return f"{prefix}:{slug}" if slug else ""


def _push_word(conn, deck_name: str, row, include_examples: bool, include_audio: bool):
    """Create or update a single Anki card for `row`. Returns 'added',
    'updated', or raises. Marks the word as exported in the local DB."""
    word = row["word"]
    example, title = (None, None)
    if include_examples:
        example, title = _word_context(conn, word)

    # Generate pronunciation audio and store it in Anki's media. Soft-fails
    # (e.g. offline) so a card is still created without audio.
    audio_tag = None
    if include_audio:
        try:
            fname = tts.media_filename(word)
            anki.store_media(fname, tts.synth_mp3(word))
            audio_tag = f"[sound:{fname}]"
        except Exception:
            audio_tag = None

    tags = [f"added:{datetime.now().strftime('%Y-%m')}"]
    if title:
        src = _slug_tag("src", title)
        if src:
            tags.append(src)

    pinyin, definition = row["pinyin"] or "", row["definition"] or ""
    existing_id = anki.find_tracker_note_id(word)
    if existing_id:
        anki.update_note(existing_id, pinyin, definition, example, audio_tag, tags)
        note_id, result = existing_id, "updated"
    else:
        note_id = anki.add_note(deck_name, word, pinyin, definition, example, audio_tag, tags)
        result = "added"

    conn.execute(
        "UPDATE words SET exported_at = ?, anki_note_id = ? WHERE word = ?",
        (now(), note_id, word),
    )
    return result


class AnkiSync(BaseModel):
    deck_name: str
    include_examples: bool = True
    include_audio: bool = True
    words: list[str] | None = None  # explicit subset; otherwise new 'learning' words


@app.post("/api/anki/sync")
def anki_sync(body: AnkiSync):
    """One-click sync: pull matured cards back to 'known', push new learning
    words as cards (with example sentences, no duplicates), then sync AnkiWeb."""
    if not anki.is_available():
        raise HTTPException(503, "AnkiConnect not reachable. Is Anki running with the AnkiConnect add-on?")

    promoted = []
    added, updated, errors = [], [], []

    with db() as conn:
        # 1. Maturity write-back: Anki says these are learned -> mark known here.
        try:
            for card in anki.get_tracker_maturity():
                if card["interval"] >= MATURE_INTERVAL_DAYS:
                    r = conn.execute(
                        "SELECT status FROM words WHERE word = ?", (card["word"],)
                    ).fetchone()
                    if r and r["status"] != "known":
                        conn.execute(
                            "UPDATE words SET status = 'known', status_updated = ? WHERE word = ?",
                            (now(), card["word"]),
                        )
                        promoted.append(card["word"])
        except Exception as e:
            errors.append({"stage": "maturity", "error": str(e)})

        # 2. Push: explicit subset, or new learning words not yet in Anki.
        if body.words:
            placeholders = ",".join("?" * len(body.words))
            rows = conn.execute(
                f"SELECT word, pinyin, definition FROM words WHERE word IN ({placeholders})",
                tuple(body.words),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT word, pinyin, definition FROM words "
                "WHERE status = 'learning' AND exported_at IS NULL"
            ).fetchall()

        for r in rows:
            try:
                result = _push_word(conn, body.deck_name, r, body.include_examples, body.include_audio)
                (added if result == "added" else updated).append(r["word"])
            except Exception as e:
                errors.append({"word": r["word"], "error": str(e)})

    # 3. Trigger AnkiWeb sync so the new cards reach the phone.
    synced = False
    try:
        anki.sync()
        synced = True
    except Exception as e:
        errors.append({"stage": "ankiweb-sync", "error": str(e)})

    return {
        "added": added,
        "updated": updated,
        "promoted_to_known": promoted,
        "synced_to_ankiweb": synced,
        "errors": errors,
    }


class AnkiRefresh(BaseModel):
    deck_name: str = ""
    include_examples: bool = True
    include_audio: bool = True


@app.post("/api/anki/refresh-existing")
def anki_refresh_existing(body: AnkiRefresh):
    """Re-render every existing chinese-tracker card with the current formatting
    (diacritic pinyin, example sentence, pronunciation audio). Updates cards in
    place — no duplicates — then syncs to AnkiWeb. Handy after improving the card
    template or fixing definitions."""
    if not anki.is_available():
        raise HTTPException(503, "AnkiConnect not reachable. Is Anki running with the AnkiConnect add-on?")

    note_ids = anki._invoke("findNotes", query=f"tag:{anki.TRACKER_TAG}")
    notes = anki._invoke("notesInfo", notes=note_ids) if note_ids else []

    updated, skipped, errors = [], [], []
    with db() as conn:
        for note in notes:
            word = anki._note_hanzi(note.get("fields", {}))
            if not word:
                continue
            row = conn.execute(
                "SELECT word, pinyin, definition FROM words WHERE word = ?", (word,)
            ).fetchone()
            if not row:
                skipped.append(word)  # card exists in Anki but not in our word list
                continue
            try:
                _push_word(conn, body.deck_name, row, body.include_examples, body.include_audio)
                updated.append(word)
            except Exception as e:
                errors.append({"word": word, "error": str(e)})

    synced = False
    try:
        anki.sync()
        synced = True
    except Exception as e:
        errors.append({"stage": "ankiweb-sync", "error": str(e)})

    return {
        "updated": updated,
        "skipped": skipped,
        "synced_to_ankiweb": synced,
        "errors": errors,
    }


# ---------- Sentence mining ----------

def _default_mine_deck() -> str:
    """Pick a target deck when the caller doesn't specify one — the first real
    deck, falling back to Anki's 'Default'."""
    try:
        decks = anki.get_deck_names()
    except Exception:
        decks = []
    for d in decks:
        if d != "Default":
            return d
    return decks[0] if decks else "Default"


def _make_cloze(sentence: str, word: str, pinyin: str) -> str | None:
    """Wrap the first occurrence of `word` in the sentence as an Anki cloze,
    using the pinyin as the hint: {{c1::word::pinyin}}."""
    idx = sentence.find(word)
    if idx < 0:
        return None
    hint = f"::{pinyin}" if pinyin else ""
    return f"{sentence[:idx]}{{{{c1::{word}{hint}}}}}{sentence[idx + len(word):]}"


class MineRequest(BaseModel):
    text_id: int | None = None
    word: str
    sentence: str | None = None
    deck_name: str = ""
    include_audio: bool = True


@app.post("/api/anki/mine")
def mine_sentence(body: MineRequest):
    """Create a cloze Anki card from the sentence a word appears in. The reader
    sends the exact sentence it's showing; otherwise we fall back to an example
    sentence pulled from the user's texts."""
    if not anki.is_available():
        raise HTTPException(503, "AnkiConnect not reachable. Open Anki desktop with the AnkiConnect add-on to mine cards.")
    word = to_simplified(body.word.strip())
    if not word:
        raise HTTPException(400, "word is required")

    with db() as conn:
        pinyin, definition, _ = _effective_entry(conn, word)
        sentence = (body.sentence or "").strip()
        title = None
        if not sentence or word not in sentence:
            sentence, title = _word_context(conn, word)  # returns word bolded
            if sentence:
                sentence = sentence.replace("<b>", "").replace("</b>", "")
        elif body.text_id is not None:
            row = conn.execute("SELECT title FROM texts WHERE id = ?", (body.text_id,)).fetchone()
            title = row["title"] if row else None

    if not sentence:
        raise HTTPException(404, "Couldn't find a sentence containing that word.")

    cloze = _make_cloze(sentence, word, pinyin)
    if not cloze:
        raise HTTPException(400, "The word isn't present in the sentence.")

    audio_tag = None
    if body.include_audio:
        try:
            fname = tts.media_filename(sentence)
            anki.store_media(fname, tts.synth_mp3(sentence))
            audio_tag = f"[sound:{fname}]"
        except Exception:
            audio_tag = None

    extra = f"<b>{word}</b> {pinyin}".strip()
    if definition:
        extra += f"<br>{definition.replace(chr(10), '<br>')}"

    tags = [f"added:{datetime.now().strftime('%Y-%m')}"]
    if title:
        src = _slug_tag("src", title)
        if src:
            tags.append(src)

    deck = body.deck_name or _default_mine_deck()
    try:
        note_id = anki.add_cloze_note(deck, cloze, extra, audio_tag, tags)
    except anki.AnkiConnectError as e:
        msg = str(e)
        if "duplicate" in msg.lower():
            raise HTTPException(409, "You've already mined this sentence.")
        raise HTTPException(502, f"Anki error: {msg}")

    return {
        "ok": True,
        "deck": deck,
        "note_id": note_id,
        "message": f"Mined into “{deck}” ✓ — sync from the dashboard to send it to your phone.",
    }


# ---------- Frontend static files ----------

app.mount("/static", StaticFiles(directory=FRONTEND_DIR / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/read")
def read_page():
    return FileResponse(FRONTEND_DIR / "read.html")


@app.get("/words")
def words_page():
    return FileResponse(FRONTEND_DIR / "words.html")


@app.get("/progress")
def progress_page():
    return FileResponse(FRONTEND_DIR / "progress.html")


@app.get("/read/{text_id}")
def read_text_page(text_id: int):
    return FileResponse(FRONTEND_DIR / "reader.html")
