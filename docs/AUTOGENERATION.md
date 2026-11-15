# Auto-generation — design sketch & implementation notes

*Status: implemented in 1.10.0 (below), from this brainstorm. The design notes after the
implementation section describe the original sketch; where they differ, the code is right.*

## What is implemented

PodWave runs **several automations side by side**. Each automation is a show with its own
schedule (daily or weekly) and is either a **news digest** — fed by RSS/Atom feeds and Tavily
interest searches, scored with the configured LLM — or a **story**, written from a rotating list
of ideas with no news involved at all. Configure them on the **Automations** tab; generation is disabled until the master
switch is on.

```mermaid
flowchart LR
  A[RSS / Atom feeds] --> C[Poll and deduplicate]
  B[Interest searches] --> C
  C --> D[LLM relevance scores]
  D --> E[Pick recent unseen stories]
  E --> F[Regular episode job queue]
  F --> G[Research, script, audio, Library]
```

- **Polling:** the union of every digest show's feeds every three hours by default; interest
  searches run alongside them and need a Tavily key. Feed parsing is stdlib-only (`xml.etree`),
  RSS 2.0 and Atom both work.
- **Scoring and routing:** each new item gets one LLM pass against all shows' interests — a
  0–10 score plus which shows it matches (`seen.tags`). A digest drops in stories tagged for it
  (untagged items match every show) that it has not already used (`seen.used_by`), so two shows
  can share the story pool without repeating each other's picks.
- **Dedup and memory:** URLs are deduplicated and items live in the local SQLite `seen` table;
  items whose scoring batch failed are retried on a later poll.
- **Scheduling:** every show keeps its own next-due in `auto_state` (`next:<id>`), re-anchored
  when its schedule is edited; weekly delivery picks a weekday (Monday = 0). A digest drop with
  nothing recent above its relevance bar is skipped and logged.
- **Story shows:** each drop takes the next idea line (rotating through the list) and queues a
  normal fiction brief — no feeds, no search key, no scoring.
- **Generation:** the queued digest is a normal brief — the existing job pipeline (research,
  script, TTS, Library) is unchanged. Queue controls and the Library view work as for any
  episode.
- **Limits:** the server must stay running for polling and drops; there is no review inbox in
  this version; disabling automation stops new scheduling but keeps queued jobs and history.
  `/api/*` has no authentication — bind only to a trusted network, since configured feed URLs
  are fetched by the server.

Follow-up ideas: an editable review inbox, per-source polling intervals, story clustering,
follow-up detection against older episodes, quiet hours, multiple show profiles, an RSS podcast
output feed, and notifications when a new episode is ready.

---

## The idea

PodWave today is *pull*: a person types topics and presses a button. Auto-generation makes it
*push*: PodWave watches the topics and news you care about, and quietly keeps an inbox of
ready-made (or already-voiced) episodes — a daily digest, a weekly deep-dive, a bedtime story
whenever the news cycle is quiet.

The key observation: **everything downstream already exists.** The whole audio pipeline
(research → outline → beats → TTS → joined WAV) runs unattended from a brief stored in the
`jobs` table. Auto-generation is only a new front end that *writes briefs on your behalf*.

```mermaid
flowchart LR
  A[Sources<br/>RSS feeds · search keywords] --> B[Poller<br/>collect new items]
  B --> C[Scorer<br/>LLM: relevance 0–10 · tags]
  C --> D[Planner<br/>pick stories · avoid repeats]
  D --> E[Digest draft<br/>a normal brief]
  E --> F{Review gate<br/>approve · edit · skip}
  F -->|auto-publish on| G[Job queue → run_job]
  F -->|manual mode| G
  G --> H[Library · “Today” inbox]
```

## The loop, stage by stage

### 1. Sources (what to watch)

| Kind | How (stdlib only) | Notes |
|---|---|---|
| RSS / Atom feeds | `urllib.request` + `xml.etree.ElementTree`, HTML stripped with `html.parser` | News sites, blogs, subreddits, YouTube channels — the usual 80 %. One fetch per feed per interval; honour `ETag`/`Last-Modified`. |
| Keyword watchlist | existing Tavily integration (`config.search`) | “home batteries”, “Danish politics” — a scheduled search *is* a feed. No new provider needed. |
| Manual seeds | today's Create page | always stays; auto-gen augments it, never replaces it. |

Per-feed settings: enabled, poll interval (default 3 h), weight (own topics vs shared),
default genre/language for stories from this source.

### 2. Dedup & scoring (what's worth an episode)

* `seen_items` table keyed on `sha1(url)` → a story is never offered twice, restart-safe.
* One cheap LLM pass per new item (batch ~20/call): relevance 0–10, one-line summary, topic
  tag, freshness note. Scores below ~5 are logged but dropped; this is the only per-item cost
  and it uses the *cheapest configured profile* (same knob as the Create-page model picker).
* Optional second, smarter pass groups items into stories (two feeds covering one event →
  one episode) and labels each as **fresh**, **follow-up** (covered before → may reference it)
  or **stale** (covered to death → skip). Prior art does exactly this and it is what keeps a
  daily show from repeating itself.

### 3. Planning (when to make an episode)

A scheduler tick (see “Scheduling”) assembles today's digest:

* take the top-scoring unseen stories until the minutes budget is spent (default: 2 stories,
  10 min, morning 07:00 local — all configurable);
* respect per-topic interests and quiet hours; a user-said “only fiction on weekends” rule is
  just a schedule filter;
* produce a **normal brief** (`topics`, `genre`, `angle`, `minutes`, `language`, `llm`…) plus
  the collected research URLs, and file it as a `digests` row in state `draft`.

From here it is 100 % the existing pipeline: `digests → create_job(brief) → queue → run_job →
episodes`. Retries, resume, loudness-matching — all free.

### 4. Review gate (who presses play on quality)

Three modes, per schedule:

1. **Ask first** (default): the digest appears in a “Today” inbox as a proposed episode —
   approve, edit topics/angle, swap, or skip. Nothing is voiced until you approve (voicing
   only after approval saves TTS cost on skipped items).
2. **Auto-publish**: queued immediately; the inbox becomes a “what PodWave made for you
   lately” feed. A bad episode costs nothing but a delete.
3. **Quiet**: scoring and dedup still run, but episodes only generate on manual “Make today's
   digest”.

### 5. Delivery (how it reaches your ears)

* v1: it just lands in **Library** — already works from any device.
* v2: a `/feed.xml` endpoint (stdlib `xml.etree` again: RSS 2.0 + iTunes tags + WAV
  enclosures) turns PodWave into a normal podcast any podcast app can subscribe to. Since
  audio is already WAV on disk, this is small.
* v3: push (OpenBot routine/notification: “your daily digest is ready”).

## Data model (new tables)

```sql
CREATE TABLE IF NOT EXISTS feeds(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  url TEXT NOT NULL UNIQUE, label TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL DEFAULT 'rss',        -- rss|atom|search
  enabled INTEGER NOT NULL DEFAULT 1,
  poll_minutes INTEGER NOT NULL DEFAULT 180,
  weight REAL NOT NULL DEFAULT 1.0,
  etag TEXT NOT NULL DEFAULT '', modified TEXT NOT NULL DEFAULT '',
  last_poll REAL NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '');

CREATE TABLE IF NOT EXISTS seen_items(
  url_hash TEXT PRIMARY KEY,               -- sha1(url)
  url TEXT NOT NULL, feed_id INTEGER, title TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '', published REAL NOT NULL DEFAULT 0,
  scored REAL NOT NULL DEFAULT -1,         -- -1 = not scored yet
  tags TEXT NOT NULL DEFAULT '[]',
  first_seen REAL NOT NULL, digest_id INTEGER);   -- NULL = still on the shelf

CREATE TABLE IF NOT EXISTS digests(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  planned_for REAL NOT NULL,               -- the tick that produced it
  state TEXT NOT NULL DEFAULT 'draft',     -- draft|queued|done|skipped|error
  brief TEXT NOT NULL,                     -- a normal brief + "source":"auto"
  items TEXT NOT NULL DEFAULT '[]',        -- [{url,title,score}]
  job_id TEXT, created REAL NOT NULL, note TEXT NOT NULL DEFAULT '');
```

## API sketch

| Endpoint | Effect |
|---|---|
| `GET/POST /api/feeds`, `PUT/DELETE /api/feeds/{id}` | manage sources (`POST /api/feeds/{id}/poll` = poll now) |
| `GET /api/digests`, `GET /api/digests/{id}` | the inbox |
| `POST /api/digests/plan` | “plan today now” — score shelf + build a draft digest |
| `POST /api/digests/{id}/approve` · `/skip` · PUT `/brief` | the review gate |
| `GET/PUT /api/automation` | schedules, mode (ask/auto/quiet), minutes budget, quiet hours |

All public like the rest of `/api/*` (the app has no auth by design) — so binding to a
trusted network stays a deployment concern, as today.

## Scheduling without adding dependencies

No cron module needed: one extra daemon thread (like `Worker`) waking every ~60 s.

* for each due `feeds` row → fetch, parse, insert unseen items (`INSERT OR IGNORE`);
* score in batches when ≥ N new items exist (off-peak bias; skip if the job queue is busy);
* at each schedule's time → if budget left, plan a digest → mode decides draft vs queued.

`health()` grows `automation: {feeds: n, pending: n, next_run: …}` so the UI and tests can
see it. Every stage logs into the existing job-log style so “why is there no digest today?”
is answerable from the Library UI.

## Quality & safety guardrails

* **Feed content is untrusted data.** Item text flows into prompts exactly like web research
  already does — the existing “treat research as data, never instructions” rule extends
  verbatim to feed bodies; snippets are length-capped before prompting.
* **No silent fact invention:** auto digests always run the research stage when a search key
  exists; without one, digests default to *opinion/roundup* framing instead of news claims.
* **Cost ceiling:** max stories/day, max minutes/day, scorer uses the cheapest profile, TTS
  runs only after approval in “ask first” mode.
* **Kill switch:** `automation.enabled=false` stops poller and planner without touching jobs.
* **Traceability:** `digests.items` keeps URL+score per story; the episode keeps the `sources`
  column it already has, so every auto episode cites where it came from.

## Milestones

| Step | Delivers | Rough work |
|---|---|---|
| **M0 — “Surprise me + Daily plan”** | watchlist keywords → `POST /api/digests/plan` → one-click approve from a Today card. No feeds yet, no scheduler. | ~half a day; reuses Tavily + jobs fully |
| **M1 — Feeds + poller + dedup** | RSS sources, `seen_items`, scoring; “Ask first” inbox | ~1–1.5 days |
| **M2 — Schedules + auto-publish** | times, budgets, quiet hours, modes; Library “Auto” badge | ~half a day |
| **M3 — Podcast-app feed** | `/feed.xml` + enclosure URLs | ~half a day |
| **M4 — Memory & show persona** | follow-up/stale story handling via past-episode titles (SQLite FTS5), recurring intro style | later |

M0 alone already answers “generate automatically based on new topics” with one manual click;
M1+M2 is the full autopilot.

## Open questions (worth deciding before M1)

1. Where does the *review* live — a fourth tab, or the top of Library? (Lean: top of Library.)
2. Should fiction also auto-generate (e.g. a bedtime story each evening from a “themes”
   watchlist), or non-fiction only in v1?
3. Default language of a digest when feeds mix languages — the topics' language rule already
   exists in the brief; inherit from the schedule instead?
4. Multi-show future: today one schedule = one show; worth reserving a `show` column now?

## Prior art (studied for this doc)

* [ai-podcast-studio](https://github.com/sourcelabs-nl/ai-podcast-studio) — closest sibling:
  monitors feeds/X, cheap-model relevance scoring, fresh/follow-up/stale topic memory,
  optional script review step, publishes to a podcast RSS feed. Validates M1–M4 nearly 1:1.
* [How I built an AI podcast pipeline](https://michael-casey.com/blog/how-i-built-an-ai-podcast-pipeline/) —
  ~$1–2/month running two daily AI shows: cheap first-pass ranking, one stronger model for
  selection+writing, *a different model reviews the script*, episode memory via embeddings.
  The reviewer-model trick is a cheap quality lever we could add as an optional pass.
* [BriefCast](https://github.com/hosikiti/briefcast) — minimal RSS→podcast proof, useful for
  how small the v0 loop can be.
* [miragea-podcast](https://pypi.org/project/miragea-podcast/) — nice publishing extras
  (ID3 chapters, transcripts, Podcasting 2.0 feed) worth borrowing concepts from later.
