# Chinese Learning Tracker

Local-first Mandarin study tool: word-status tracking (unknown/learning/known),
a reader with click-to-lookup (jieba segmentation + CC-CEDICT), an analytics
dashboard, and two-way Anki sync via AnkiConnect.

## Getting started (setting up on a new computer)

These steps get the tracker running from scratch. Windows is the main path (with
macOS/Linux notes inline). You only do steps 1–4 once; after that, launching is one command.

### 0. Install prerequisites

- **Python 3.10 or newer.** Get it from [python.org](https://www.python.org/downloads/).
  On the Windows installer, tick **"Add Python to PATH"**.
  - On Windows the reliable command is the launcher **`py`** (plain `python` may open the
    Microsoft Store instead — if it does, use `py` everywhere below). Check with `py --version`.
  - On macOS/Linux use `python3` in place of `py`.
- **Git**, to clone the repo — [git-scm.com](https://git-scm.com/downloads).
- **(Optional) Anki desktop** — only needed for the Anki sync features. See the *Anki sync*
  section further down.

### 1. Clone the repo

```
git clone https://github.com/SneverdleSnavis/chinese-tracker.git
cd chinese-tracker
```

### 2. Create a virtual environment and install dependencies

The env **must** be named `venv` (the `start_server.bat` launcher expects that folder).

Windows (PowerShell or Command Prompt):

```
py -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

macOS/Linux:

```
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

> If Windows PowerShell blocks `activate` with a script-execution error, run once:
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, then try again.

### 3. Download the CC-CEDICT dictionary (required — not included in the repo)

The dictionary file is too large / license-tracked, so it isn't committed. Without it,
word lookups won't work. Get it once:

1. Go to https://www.mdbg.net/chinese/dictionary?page=cc-cedict
2. Download the **CC-CEDICT** file in **UTF-8** format (`cedict_1_0_ts_utf-8_mdbg.txt.gz`).
3. Unzip it, rename the extracted text file to **`cedict.txt`**, and put it at:
   **`backend/data/cedict.txt`**

The HSK 3.0 word list (`backend/data/hsk30.txt`) *is* included, so nothing to do there.

### 4. Run it

Windows — easiest: double-click **`start_server.bat`** (it activates the env, starts the
server, and opens your browser).

Or from a terminal, with the env activated (step 2):

```
uvicorn main:app --app-dir backend --port 8000
```

Then open **http://localhost:8000**. Your data (words, texts, progress) is stored locally in
`backend/data/app.db`, which is created automatically on first run and stays on your machine.

### Every time after that

Windows: double-click `start_server.bat`. Otherwise: activate the env
(`venv\Scripts\activate` / `source venv/bin/activate`) and run the `uvicorn` command above.

## Updating & backing up your data

**All your progress lives in one file: `backend/data/app.db`** (a SQLite
database). It holds everything — word statuses, custom definitions, imported
texts/subtitles/books, seen counts, lookup history, goals, and HSK progress. It's
created automatically on first run and **never leaves your machine** — it's listed
in `.gitignore`, so it is never committed or uploaded to GitHub.

### Updating to a newer version

From inside your existing project folder, just pull the latest code:

```
git pull
pip install -r requirements.txt   # in case dependencies changed (activate the venv first)
```

Your `app.db` is untouched by `git pull`, so **all progress is preserved.** This is
the recommended way to update.

> ⚠️ **Don't update by deleting the folder and re-cloning.** A fresh clone does
> *not* include `app.db` (it was never on GitHub), so you'd start from an empty
> tracker. If you must re-clone into a new folder, first copy
> `backend/data/app.db` out of the old folder and into the same path in the new
> one **before** running it.

### Backing up / moving to another computer

`backend/data/app.db` *is* your backup — copy it somewhere safe periodically. To
move your progress to a different machine, set the app up there (see *Getting
started*), then drop your copied `app.db` into `backend/data/` before launching.
Copy it while the server is stopped so the write-ahead log is flushed.

## Anki sync

Requires Anki desktop running with the [AnkiConnect](https://ankiweb.net/shared/info/2055492159)
add-on installed. The dashboard's Anki Sync panel lets you:
- **Import known words**: pull words from a deck and mark them "known" here — but only
  the ones whose card has matured (spacing interval ≥ `MATURE_INTERVAL_DAYS`, 21 days).
  New and still-learning cards are skipped, so importing doesn't blanket-mark the deck.
- **Sync to Anki** (one button) does three things:
  1. **Pushes new "learning" words** as cards. Each card's back gets the pinyin
     (diacritic tones, e.g. `nǐ hǎo`), definition, and — if enabled — an
     **example sentence pulled from a text you actually read** (word bolded) and
     **pronunciation audio** generated by a free neural voice (`edge-tts`,
     `backend/tts.py`), stored in Anki's media so it syncs to your phone. Cards
     are tagged with the source text and the month added.
  2. **Skips duplicates / updates in place.** The site remembers what it has
     pushed (`words.exported_at` / `anki_note_id`) and matches existing cards by
     hanzi, so re-syncing never creates duplicates — it updates the card instead.
  3. **Promotes matured cards to "known".** Any `chinese-tracker` card whose Anki
     interval has reached `MATURE_INTERVAL_DAYS` (21) is treated as learned, and
     its word is marked "known" here.
- Finally it triggers Anki's **AnkiWeb sync**, so the new cards reach your phone
  (AnkiDroid / AnkiMobile) without hosting anything yourself.

You never need a permanent server: AnkiWeb is the "study anywhere" layer. The
site only needs Anki desktop open (for AnkiConnect) when you press Sync.

## How it's organized

- `backend/` — FastAPI app, SQLite storage (`backend/data/app.db`), jieba segmentation,
  CC-CEDICT lookup, AnkiConnect client.
- `frontend/` — plain HTML/CSS/JS: dashboard (`index.html`), text list (`read.html`),
  reader with click-to-lookup (`reader.html`).

## Status colors in the reader

Underline color reflects per-word status: gray = unknown, yellow = learning, green = known.
Click any Chinese word to see pinyin/definition and change its status. Pinyin is shown
with diacritic tone marks (`nǐ hǎo`); CC-CEDICT's numbered pinyin is converted on load by
`backend/pinyin_tones.py`.

## Fetching new articles

The Reading page has a "Fetch new articles" panel pulling from:
- **BBC Chinese (News)** — full article text scraped from the linked page (RSS only gives a teaser).
- **Mandarin Bean (Graded Readers)** — learner-leveled lessons, extracted to simplified-only text
  (the source's pinyin/traditional annotations are stripped out).
- **DW 中文**, **RFI 中文**, **RFA 普通话**, **中央社 CNA** — native news feeds scraped with the generic
  `<p>`-tag extractor (CNA is Traditional and gets converted to Simplified on import).

Each candidate shows a difficulty bar (% known/learning/unknown words, based on your current
word-status data) before you commit to adding it. Already-imported articles (matched by source
URL) are filtered out automatically. Source list lives in `backend/fetchers.py` — add an entry
to `SOURCES` and an extractor function to add more feeds.

All text — pasted, or fetched from any source — is converted to Simplified Chinese via OpenCC
(`backend/normalize.py`) before it's stored or scored. This keeps word tracking consistent even
if a source (like BBC Chinese) returns Traditional characters.

## Reader extras

- **Embedded video** — subtitle texts imported from YouTube show an inline player
  (sticky at the top) for side-by-side watching. Clicking a line's timestamp seeks
  the embedded player to that moment. The player has keyboard control disabled
  (`disablekb`), so pressing 1/2/3 to mark words never accidentally jumps the video.
- **Comprehension questions (multiple choice)** — each text can hold a set of
  saved multiple-choice questions, rendered as clickable options that show
  ✓/✗ instantly. Two ways to add them, both feeding the same store:
  - **Paste in (no API key)** — "Add / paste questions" opens an editor. "Copy a
    prompt…" puts a ready-made prompt (with the text embedded) on your clipboard;
    paste it into any chatbot, then paste the JSON it returns back in and Save.
    JSON shape: `[{"question":"…","choices":["…","…","…","…"],"answer":0}]` where
    `answer` is the 0-based index of the correct choice.
  Questions are stored per text in the `comprehension_questions` table and
  served by `GET`/`PUT`/`DELETE /api/texts/{id}/questions`. An optional LLM path
  (`POST /api/texts/{id}/questions/generate`, requires `ANTHROPIC_API_KEY`) can
  generate and save questions too, but there's no UI button for it right now —
  the paste flow is the supported path.
- **Edits keep your place** — splitting/merging a word re-segments without yanking
  the page: it preserves your scroll position and re-selects the exact instance you
  edited (matched by character offset), not the first occurrence in the text.

## Video / subtitle study

The Reading page has an "Import subtitles" panel:
- **YouTube or Bilibili URL** — auto-fetches the video's Chinese captions via yt-dlp (prefers
  manual simplified subs, falls back to auto-generated). `subtitles.fetch_video_subtitles` routes
  by URL; Bilibili captions (incl. AI tracks) are parsed from yt-dlp's JSON/bcc format. Bilibili is
  best-effort — many videos have no CC, need a login, or are geo-restricted, and you'll get a clear
  error in those cases. Only YouTube gets the embedded split-view player; Bilibili lines link out.
- **File upload** — drop in any `.srt` or `.vtt` file (downloaded shows, movies, subs you grabbed
  manually).

Imported subtitles open in the reader as timestamped lines with the same click-to-lookup and
word tracking as articles. All text is normalized to simplified on import (`backend/normalize.py`).

Parsing/fetching lives in `backend/subtitles.py`; lines are stored in the `subtitle_lines` table.

## Importing books (EPUB / TXT)

The Reading page's "Import a book" panel takes a `.epub` or `.txt` file
(`POST /api/books/upload`):
- A **.txt** becomes a single text.
- An **.epub** is split into **one text per chapter** (each document item with enough Chinese,
  titled `Book — Chapter`), so the reader stays responsive on long books. Parsed with `EbookLib`
  + BeautifulSoup (`_parse_epub` in `backend/main.py`); covers/TOC/colophon are skipped.

Everything is converted to Simplified on import and runs through the normal segmentation,
status tracking, and seen-count pipeline.

## Learn next (what to study for the most benefit)

Every unknown word costs you comprehension in proportion to how often it appears
in texts you actually read — and that frequency is exactly what `seen_count`
tracks. Two surfaces use it:

- **Dashboard "Learn next" card** — your most frequent unknown words across all
  texts, ranked, each showing its definition, how many times you've seen it, and
  the running **coverage** you'd reach by learning down to that row. *Coverage* =
  the share of all word-instances in your texts you already know
  (`SUM(seen_count)` over known/learning ÷ total). Mark a word *Learning*/*Known*
  inline and it drops off.
- **Reader "Prep — what to learn first"** — the same idea scoped to the open
  text (`text_word_counts`): "you know 69% of this text; learning these 20 gets
  you to ~79%." Great for softening a hard article before you read it. Marking a
  word here also recolours every occurrence in the reader.

Served by `GET /api/learn/next?scope=all|text&text_id=&limit=` and
`GET /api/analytics/coverage` (see `_coverage`/`learn_next` in `backend/main.py`).

## Goals

The **Progress** tab has a Goals card where you set targets and watch live
progress bars. Four goal kinds: **total known words** (cumulative) and three
rolling weekly targets — **new known words**, **texts added**, and **study
days** (each measured over the last 7 days). Set or change a target inline; a bar
turns green with a ✓ when met. Stored in the `goals` table (one row per kind);
progress is computed live by `GET /api/goals`, set via `PUT /api/goals/{kind}`,
cleared via `DELETE /api/goals/{kind}` (kinds defined in `GOAL_DEFS`).

## Progress dashboard (streaks, activity, growth)

The top of the **Progress** tab summarises your study habit:
- **Streak cards** — current day streak (🔥 when you've studied today), longest
  streak, total active days, and your known-word count.
- **Activity heatmap** — a GitHub-style 26-week calendar; each day is shaded by
  how many lookups and status changes you made (hover for the exact count).
- **Vocabulary growth** — a line chart of cumulative *known words* and *words
  encountered* over time.

A "study day" / activity is any day with a word lookup or a status change. The
data comes from `GET /api/analytics/timeline` (`known_series`/`seen_series` are
cumulative by `status_updated`/`first_seen`; `activity` is per-day counts;
`streak` is computed over active days, all in UTC). Charts are inline SVG — no
chart library.

## HSK 3.0 progress

The lower **Progress** section tracks the New HSK 3.0 (2021) vocabulary — bands 1–6 plus
the combined 7–9 advanced tier, ~11k words (`backend/data/hsk30.txt`, loaded by
`backend/hsk.py`; a word that spans levels is attributed to its lowest band).
Per-band completion bars show how many words you've marked *known*; tapping a
band lists the words you're still missing, **frequency-ordered** (by `seen_count`)
so the most useful ones to learn surface first — with inline *Learning*/*Known*.

Coverage and HSK progress are complementary: coverage measures comprehension of
**your own corpus**, while HSK progress measures a **curriculum** independent of
what you've read. Endpoints: `GET /api/hsk/progress`, `GET /api/hsk/missing?band=`.

## Sentence mining → Anki

In the reader, selecting a word reveals **✚ Mine sentence → Anki** in the lookup
panel. It builds an Anki **cloze** card from the exact sentence the word is in:
- For subtitles the sentence is the line; for articles it's the run of text
  around the word bounded by sentence punctuation (`。！？；`).
- The target word becomes the cloze with its pinyin as the hint
  (`{{c1::word::pīnyīn}}`); the back ("Back Extra") holds the pinyin + definition
  and **audio of the whole sentence** (free `edge-tts`, stored in Anki media).
- Cards are tagged `chinese-tracker mined` (plus the source text and month) and
  go to the chosen deck (defaults to your first real deck). Re-mining the same
  sentence is rejected as a duplicate.

It needs Anki desktop running (AnkiConnect); the card reaches your phone on the
next dashboard **Sync to Anki**. Endpoint: `POST /api/anki/mine` (`add_cloze_note`
in `backend/anki.py`, `mine_sentence` in `backend/main.py`).

## Words page (browse & edit your vocabulary)

The **Words** tab is the home for your full word list:
- **Search** across hanzi, pinyin, and definition; **filter** by status, Anki-sync
  state, or whether you've edited the word; **sort** by recency / frequency / A–Z.
- **Edit** any word's pinyin and definition inline. An edit is stored as an
  authoritative override (`custom_definitions`) that **takes precedence over
  CC-CEDICT everywhere** — so a corrected definition shows up consistently in
  every future text, the reader, exports, and Anki cards. A word you've edited is
  marked "edited"; **Revert** removes the override and restores the dictionary entry.
- Change a word's status (known/learning/unknown) straight from the table.
- **Revert** an edited word to its dictionary definition, or **delete** a word
  from the tracker entirely (✕ — removes the word row, any override, and lookup
  history; an existing Anki card is left untouched).

Words with multiple dictionary entries (e.g. several readings/senses) show **all**
of them — one `pinyin — definition` line per entry, matching the reader popup —
in the word list, exports, and on Anki cards. This combined form is built by
`_format_entries` in `backend/main.py`.

Low-value senses are filtered out systematically: entries beginning with
`surname`, `variant of`, or `old/archaic/ancient variant of` are dropped before
display (kept only if a word has nothing else). The patterns live in
`_NOISE_ENTRY_RE` / `filter_entries` in `backend/dictionary.py` — add to that
regex to exclude more. The filter runs at the lookup layer, so it applies to the
reader, word list, and Anki cards alike; a user's own edited definition always
overrides it.

The list is served by `GET /api/words` (with `q`/`status`/`synced`/`custom`/`sort`/
`limit`/`offset`), edits by `POST /api/words/{word}`, and reverts by
`DELETE /api/words/{word}/custom`.

### Seen counts

`seen_count` is the cumulative number of times a word has appeared across all
texts. It's tallied **once, when a text is added** (`_record_text_occurrences` in
`backend/main.py`, backed by the `text_word_counts` table) — not on every reader
open, so re-reading a text never inflates the figure. Deleting a text does **not**
decrement it: the exposure already happened, so the count persists. To recompute
the totals from the texts currently stored (e.g. after a bulk import), call
`rebuild_seen_counts(conn)`.

## Fixing segmentation & missing words

In the reader, selecting a word reveals edit controls in the lookup panel:
- **✂ Split into characters** — when jieba wrongly glues characters together, this breaks the
  word apart. Stored as a global override (`jieba.del_word`) so it stays fixed across all texts.
- **Merge with «next» →** — joins the selected word with the following one into a single token
  (`jieba.add_word`), also global and persistent.
- **✎ Define** — for words CC-CEDICT doesn't cover, add your own definition. Pinyin is
  auto-suggested via `pypinyin`, so you usually only type the meaning. Custom definitions then
  appear wherever that word shows up, and are included in the CSV export.

Both kinds of correction persist in the `segmentation_overrides` and `custom_definitions` tables
and are re-applied to jieba on startup. After an edit the reader re-segments and auto-looks-up the
affected word.

## Next steps (not yet built)

- Bilibili subtitle import (different caption API than YouTube)
- Spaced-repetition review built into the site itself, as an alternative to Anki
