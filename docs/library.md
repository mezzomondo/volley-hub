# Drill library and training sessions

Staff (and admins) can ask any connected AI about the club's drills and past training sessions:
"which reception drills with jump serves have we used?", "show me the Akbas drill", "what did we do on
Wednesdays in December?". Players have no access.

## Tools

| Tool | What it does |
|---|---|
| `list_drills(theme?)` | the library by theme, with how often and when each drill was used |
| `search_drills(text?, theme?, group?, season?)` | full-text search (English stemming) over names, summaries and notes |
| `get_drill(drill, diagrams=3)` | every use of a drill; the first diagrams come back as images the AI can see |
| `list_sessions(group?, season?, date_from?, date_to?, drill?)` | sessions with their drills in order |
| `get_session(session_id, diagrams=0)` | one session as planned, block by block |
| `draw_drill(spec)` | draws a drill on a court (PNG link, 7 days); the AI is told to look at the coach's own diagrams first and follow his style |

## Where the content comes from

The source of truth is a Markdown vault per season, kept by the coach (e.g. built from Noteful exports):

```
Season 2025-26/
  Sessions/<Weekday ...>/<YYYY-MM-DD>.md   (+ .pdf, img/*.png)
  Drills/<slug>.md
```

`scripts/library-bundle.py` turns the vault into a JSON bundle (format described in its docstring) and
`vh library` publishes it; publishing a season again replaces it, so the vault can be edited freely.

```sh
scripts/vh library "~/Documents/Coaching/Season 2025-26"              # text only
scripts/vh library "~/Documents/Coaching/Season 2025-26" --diagrams   # also the diagrams (PNG)
```

Privacy: attendance is never published. The "Players:" line becomes a head count and player pseudonyms become
"X" in the text. Diagrams and PDFs are uploaded only on request (`--diagrams`, `--pdfs`), because handwritten sketches can show names; staff-only either way.

## Adding new sessions

1. Export the session from the notes app as PDF.
2. In Claude Code, ask to add it to the vault: the AI reads the PDF (including the drawings), writes
   `Sessions/<Weekday>/<date>.md`, crops the diagrams into `img/`, and links each block to a drill page in
   `Drills/` (an existing drill, or a new page it proposes). Review and correct.
3. `scripts/vh library "<vault>"` to publish.
