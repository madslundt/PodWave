# PodWave — AI Podcast Studio

Write a podcast episode from a few topics: research, script, spoken audio. Runs as one
small Python server, reachable from desktop and phone, and keeps generating after you
close the tab.

## What it does

1. **Brief** — topics (one per line), genre (non-fiction / fiction), format (two hosts or
   solo), target length in minutes, optional style notes.
2. **Research** — for non-fiction, Tavily (optional) collects sources; otherwise the model's
   own knowledge is used.
3. **Script** — the model plans 4–10 beats, then writes each beat as spoken dialogue
   (~150 words/minute), returned as segments `{speaker, text}` and shown as an editable script.
4. **Audio** — each line is voiced by the configured TTS, joined with short gaps, and stored
   as a 24 kHz mono WAV you can play or download.

Two ways to run a job:

| | In this browser | On the server (background) |
|---|---|---|
| Tab must stay open | ✓ | — |
| Works from any device | ✓ | ✓ |
| Keys live in | browser `localStorage` | server `data/config.json` |
| Stored audio | browser IndexedDB | server `data/` + Library |

Queue it, close the laptop, come back to **Library** and press play. Interrupted jobs
re-queue after a restart, and voiced segments are reused, so a crash does not start over.

## Run it

```bash
docker compose up -d --build     # then open http://localhost:8080
```

or without Docker (Python 3.10+, no dependencies):

```bash
python3 server/podwave.py        # http://127.0.0.1:8080
```

Environment: `PODWAVE_DATA` (default `./data`), `PODWAVE_STATIC`, `PODWAVE_PORT` (8080),
`PODWAVE_BIND` (`127.0.0.1`; set `0.0.0.0` to expose), `PODWAVE_TOKEN` (if set, every
`/api/*` call needs `Authorization: Bearer <token>`). Keys can also be seeded with
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
- **Search**: Tavily or none.
- **TTS**: OpenAI-compatible `/audio/speech` (WAV output) or ElevenLabs (`output_format=wav`).
  Browser speech stays available for live playback only. If a provider returns MP3 and
  `ffmpeg` is installed, the server converts it; otherwise it asks for WAV output.

## API

| | |
|---|---|
| `GET /api/health` | version, masked config, queue and library counts |
| `GET/PUT /api/config` | server config, keys masked (`keySet: true`) |
| `GET/PUT /api/brief` | last brief, so a reload or another device restores it |
| `POST /api/jobs` | `{topics[], genre, format, minutes, style}` → `202 {id}` |
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
cannot steer the writer. If you expose the port, set `PODWAVE_TOKEN` or put it behind auth:
an open server is an open cost meter and anyone on it can read the library.

## Tests

```bash
python3 tests/e2e.py
```

Starts the server with a mock OpenAI-compatible provider in a temp dir and checks 42
things: job lifecycle, cancel and retry, resume after `SIGKILL`, WAV validity and
duration, `Range` requests, masked config, path traversal, error paths. Nothing real is
called.

## Notes and limits

- Long episodes: WAV is ~2.9 MB/audio-minute; a client-side MP3 encoder would shrink that.
- CORS: browsers block some providers from a browser origin (OpenAI and ElevenLabs are fine);
  background jobs avoid the issue entirely.
- Ideas: RSS feed, per-beat regeneration, voice preview before a full render, an MP3
  encoder, PWA manifest for install-on-phone.
- The built-in "sample script" is **example data**, not fact-checked.
