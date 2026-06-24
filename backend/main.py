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
def list_words(status: str | None = None):
    with db() as conn:
        if status:
            rows = conn.execute("SELECT * FROM words WHERE status = ? ORDER BY last_seen DESC", (status,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM words ORDER BY last_seen DESC").fetchall()
        return [dict(r) for r in rows]


@app.get("/api/words/export")
def export_words():
    """Download all words as CSV — a portable backup of the data only you generated."""
    import csv
    import io
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["word", "status", "pinyin", "definition", "seen_count", "first_seen", "last_seen", "status_updated"])
    with db() as conn:
        rows = conn.execute(
            "SELECT word, status, pinyin, definition, seen_count, first_seen, last_seen, status_updated "
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

    # Fill in user-supplied definitions for words CC-CEDICT doesn't cover.
    missing = {t["text"] for t in tokens if t["is_chinese"] and not t["entries"]}
    custom = _custom_defs(conn, missing)

    for tok in tokens:
        if tok["is_chinese"]:
            tok["status"] = words_status.get(tok["text"], "unknown")
            if not tok["entries"] and tok["text"] in custom:
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
    """Generate numbered pinyin (CC-CEDICT style) for a word jieba/CC-CEDICT
    can't define, so the user only has to fill in the meaning."""
    from pypinyin import pinyin, Style
    parts = pinyin(word, style=Style.TONE3, neutral_tone_with_five=True)
    return " ".join(p[0] for p in parts)


@app.get("/api/lookup")
def lookup_word(word: str):
    """Look up an arbitrary word (used when splitting/merging tokens). Returns
    CC-CEDICT entries, falling back to a custom definition, plus the word's
    current status and a suggested pinyin if undefined."""
    from dictionary import lookup as cedict_lookup
    entries = cedict_lookup(word)
    with db() as conn:
        if not entries:
            custom = _custom_defs(conn, {word})
            if word in custom:
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


class AnkiExport(BaseModel):
    deck_name: str
    words: list[str] | None = None  # if omitted, export all 'learning' words


@app.post("/api/anki/export")
def anki_export(body: AnkiExport):
    if not anki.is_available():
        raise HTTPException(503, "AnkiConnect not reachable. Is Anki running with the AnkiConnect add-on?")
    with db() as conn:
        if body.words:
            placeholders = ",".join("?" * len(body.words))
            rows = conn.execute(
                f"SELECT word, pinyin, definition FROM words WHERE word IN ({placeholders})",
                tuple(body.words),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT word, pinyin, definition FROM words WHERE status = 'learning'"
            ).fetchall()

    added = []
    errors = []
    for r in rows:
        try:
            anki.add_note(body.deck_name, r["word"], r["pinyin"] or "", r["definition"] or "")
            added.append(r["word"])
        except Exception as e:
            errors.append({"word": r["word"], "error": str(e)})
    return {"added": added, "errors": errors}


# ---------- Frontend static files ----------

app.mount("/static", StaticFiles(directory=FRONTEND_DIR / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/read")
def read_page():
    return FileResponse(FRONTEND_DIR / "read.html")


@app.get("/read/{text_id}")
def read_text_page(text_id: int):
    return FileResponse(FRONTEND_DIR / "reader.html")
