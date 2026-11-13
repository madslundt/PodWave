# PodWave — AI Podcast Studio

Write a podcast episode from a few topics: research, script, spoken audio. Runs as one
small Python server, reachable from desktop and phone, and keeps generating after you
close the tab.

## What it does

1. **Brief** — topics (one per line), genre (non-fiction / fiction), format (two hosts or
   solo), target length in minutes, the language to write and speak the episode in
   (default: the language of your topics) and optional style notes. Fiction adds story tone,
   characters, setting and an optional surprise-twist flag; non-fiction adds an optional
   angle/focus — all fed into the outline and beat prompts. With a server, a **setup chat**
   sits under the brief: describe the episode in your own words and the model fills the
   fields live while you talk, asking only about the few details that would most improve
   the episode. You can stop, edit anything, or generate at any time — the chat never
   blocks the brief.
2. **Research** — for non-fiction, Tavily (optional) collects sources; otherwise the model's
   own knowledge is used.
3. **Script** — the model plans 4–10 beats, then writes each beat as spoken dialogue
   (~150 words/minute), returned as segments `{speaker, text}` and shown as an editable script.
   Fiction follows a story arc — cold-open hook, escalating conflict, midpoint turn, climax,
   payoff — with every beat ending on a hook; non-fiction is told as a story with concrete
   scenes and named sources.
4. **Audio** — each line is voiced by the configured TTS (with delivery instructions derived
   from the tone/style, for providers that support them), loudness-matched to one level, joined
   with pacing-aware pauses (quick dialogue hand-offs ~0.08 s, beat breaks ~0.30 s) and 6 ms
   edge fades so joins never click, and stored as a 24 kHz mono WAV you can play or download.

Two ways to run a job:

| | In this browser | On the server (background) |
|---|---|---|
| Tab must stay open | ✓ | — |
| Works from any device | ✓ | ✓ |
| Keys live in | browser `localStorage` | server `data/config.json` |
| Stored audio | browser IndexedDB | server `data/` + Library |

Queue it, close the laptop, come back to **Library** and press play. A queued story appears
under **Being created** (with progress, cancel and retry) and moves to **Created** when done —
the Create page resets to a blank brief the moment you queue. Interrupted jobs re-queue after
a restart, and voiced segments are reused, so a crash does not start over.

The page you are on is kept in the URL (`#/create`, `#/library`, `#/settings`), so a refresh
lands on the same page. Tab switches keep your half-written brief; a reload clears the input
fields (the browser warns before you leave a page with something typed).

## Run it

```bash
docker compose up -d --build     # then open http://localhost:8080
```

or without Docker, with [uv](https://docs.astral.sh/uv/) (no third-party packages at all):

```bash
uv run server/podwave.py         # http://127.0.0.1:8080, uv picks Python >= 3.14
```

or with a plain interpreter — any Python 3.10–3.15 works, the server only uses the standard
library:

```bash
python3 server/podwave.py        # http://127.0.0.1:8080
```

The image is `ghcr.io/astral-sh/uv:python3.15-trixie-slim` (uv + CPython 3.15), pinned by
`.python-version`; `pyproject.toml` asks for `>= 3.14` and the suite passes on 3.14 and 3.15.

Environment: `PODWAVE_DATA` (default `./data`), `PODWAVE_STATIC`, `PODWAVE_PORT` (8080),
`PODWAVE_BIND` (`127.0.0.1`; set `0.0.0.0` to expose). There is no login and no access
token — every `/api/*` endpoint is public. Keys can also be seeded with
`PODWAVE_LLM_KEY`, `PODWAVE_TTS_KEY`, `PODWAVE_SEARCH_KEY`, `PODWAVE_LLM_MODEL`,
`PODWAVE_LLM_BASE`, `PODWAVE_TTS_BASE`, `PODWAVE_TTS_MODEL`.

## Configuration

- **Server settings** (Settings tab, "This server"): used for background jobs. Written to
  `data/config.json` with mode `0600`; the API only ever reports whether a key is set.
- **Browser settings** (Settings tab): used when you click *Generate script* /
  *Generate audio* on this device. Stored in `localStorage`, sent only to the providers
  you configure.
- **LLM**: any OpenAI-compatible `/chat/completions` (OpenAI, OpenRouter, Groq, a local
  llama.cpp/Ollama `/v1`).
- **Keys are optional**: for local/self-hosted LLM and TTS that need no auth, leave the key
  fields empty (tick *remove stored key* on the server panel). No `Authorization` header is
  sent at all when no key is stored — required only for hosted providers and ElevenLabs.
- **Search**: Tavily or none.
- **Test buttons** (Settings, both panels) call one provider once and report latency plus what came
  back, so a wrong URL, key, or model name is found in a second instead of after a 10-minute episode.
  They use the values in the fields as typed — nothing is saved, and a failed test changes nothing.
  On the server panel a blank key field means "use the stored key". `curl -X POST localhost:8080/api/test
  -d '{"target":"all"}'` does the same three checks from the command line.
- **TTS**: OpenAI-compatible `/audio/speech` (WAV output) or ElevenLabs (`output_format=wav`).
  Background jobs send a short `instructions` line (story tone + style notes) for expressive
  delivery; providers that reject the field are detected on the first line and skipped for the
  rest of the job. Browser speech stays available for live playback only — long lines are split
  into sentence chunks with a keep-alive so browsers that stall long speech don't cut off.
  If a provider returns MP3 and `ffmpeg` is installed, the server converts it; otherwise it
  asks for WAV output.

## API

| | |
|---|---|
| `GET /api/health` | version, masked config, queue and library counts |
| `GET/PUT /api/config` | server config, keys masked (`keySet: true`) |
| `GET/PUT /api/brief` | last brief stored on the server (the page keeps the draft locally and never restores it into the form) |
| `POST /api/brief/chat` | `{messages:[{role,content}], brief}` → `{reply, updates, done}` — one setup-chat turn; `updates` are validated against the real brief fields, `done` means the model considers the brief ready (and always obeys a stop) |
| `POST /api/test` | `{target: llm\|search\|tts\|all, llm?, search?, tts?}` → per-provider `ok`/`fail`/`unset` with detail; overrides are used in memory only |
| `POST /api/jobs` | `{topics[], genre, format, minutes, style, language?, tone?, characters?, setting?, twist?, angle?}` → `202 {id}` |
| `GET /api/jobs` / `GET /api/jobs/{id}` | list / detail with stage, progress, log |
| `POST /api/jobs/{id}/cancel` \| `/retry` | stop, or re-queue a failed job |
| `DELETE /api/jobs/{id}` | forget the job (audio already stored is kept) |
| `GET /api/episodes` | stored episodes |
| `GET /api/episodes/{id}/audio` | WAV, with `Range` support for seeking |
| `GET/PUT /api/episodes/{id}/script` | segments (edit text without re-voicing) |
| `DELETE /api/episodes/{id}` | delete episode + audio file |

Job states: `queued → running → done`, plus `error`, `canceled`; `canceling` while a running
job notices the request. Stages: `research`, `outline`, `writing`, `voicing`, `encoding`.

## Privacy

Browser keys never leave the browser except in direct calls to the providers you set.
Server keys stay in `data/config.json` (0600) and are never returned by the API. The page
loads no third-party scripts. Research text is labelled untrusted in the prompt, so a page
cannot steer the writer. The server is public on purpose — no login, no token: anyone who
can reach the address can run jobs, spend the configured provider's budget, and listen to
the library. Keep it on a private network or behind a reverse proxy you control if that
matters to you.

## Tests

```bash
uv run tests/e2e.py            # or: python3 tests/e2e.py
```

Starts the server with a mock OpenAI-compatible provider in a temp dir and checks 90
things: job lifecycle, cancel and retry, resume after `SIGKILL`, WAV validity and
duration, beat-aware pacing and loudness matching of the joined audio, the story-brief
and language fields reaching the model and the voice instructions, setup-chat turns
(brief + history sent, suggested updates clamped to real fields, the stop path, model
errors), `instructions` sent and cleanly skipped when rejected,
`Range` requests, masked config, path traversal, error paths, and every
`/api/test` outcome — passing, model error, empty reply, refused connection, provider
switched off, and a typed-in key that must never reach `config.json`. Nothing real is
called. Passing on CPython 3.14.9 and 3.15.2.

## Notes and limits

- Long episodes: WAV is ~2.9 MB/audio-minute; a client-side MP3 encoder would shrink that.
- CORS: browsers block some providers from a browser origin (OpenAI and ElevenLabs are fine);
  background jobs on the server avoid the issue entirely. A browser-side Test that says "the browser
  could not reach it" is usually this, not a bad key — the same check on the server panel will pass.
- Ideas: RSS feed, per-beat regeneration, an MP3 encoder, PWA manifest for install-on-phone.
- The built-in "sample script" is **example data**, not fact-checked.
