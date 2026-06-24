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
        existing = conn.execute("SELECT word FROM words WHERE word = ?", (body.word,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE words SET status = ?, status_updated = ? WHERE word = ?",
                (body.status, ts, body.word),
            )
        else:
            conn.execute(
                "INSERT INTO words (word, status, seen_count, first_seen, last_seen, status_updated) "
                "VALUES (?, ?, 0, ?, ?, ?)",
                (body.word, body.status, ts, ts, ts),
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
        return entries[0]["pinyin"], entries[0]["definition"], "cedict"
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


@app.delete("/api/words/{word}/custom")
def revert_word(word: str):
    """Remove a user override, reverting the word to its CC-CEDICT definition."""
    word = to_simplified(word.strip())
    with db() as conn:
        conn.execute("DELETE FROM custom_definitions WHERE word = ?", (word,))
        from dictionary import lookup as cedict_lookup
        entries = cedict_lookup(word)
        if entries:
            conn.execute(
                "UPDATE words SET pinyin = ?, definition = ? WHERE word = ?",
                (entries[0]["pinyin"], entries[0]["definition"], word),
            )
        py, defn, source = _effective_entry(conn, word)
    return {"ok": True, "word": word, "pinyin": py, "definition": defn, "source": source}


def _record_word_seen(conn, word: str, pinyin: str = "", definition: str = ""):
    ts = now()
    existing = conn.execute("SELECT word, seen_count FROM words WHERE word = ?", (word,)).fetchone()
    if existing:
        conn.execute(
            "UPDATE words SET seen_count = seen_count + 1, last_seen = ?, "
            "pinyin = COALESCE(pinyin, ?), definition = COALESCE(definition, ?) WHERE word = ?",
            (ts, pinyin, definition, word),
        )
    else:
        conn.execute(
            "INSERT INTO words (word, status, pinyin, definition, seen_count, first_seen, last_seen) "
            "VALUES (?, 'unknown', ?, ?, 1, ?, ?)",
            (word, pinyin, definition, ts, ts),
        )


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


def _tokenize_with_status(conn, content: str, record_seen: bool = True):
    """Segment text, attach each Chinese token's known/learning/unknown status,
    and (optionally) bump its seen-count. Shared by article and subtitle reading."""
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
            if record_seen:
                pinyin = tok["entries"][0]["pinyin"] if tok["entries"] else ""
                definition = tok["entries"][0]["definition"] if tok["entries"] else ""
                _record_word_seen(conn, tok["text"], pinyin, definition)
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


class YoutubeImport(BaseModel):
    url: str


@app.post("/api/subtitles/youtube")
def import_youtube(body: YoutubeImport):
    try:
        title, video_url, cues = subtitles.fetch_youtube_subtitles(body.url)
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


@app.get("/read/{text_id}")
def read_text_page(text_id: int):
    return FileResponse(FRONTEND_DIR / "reader.html")
