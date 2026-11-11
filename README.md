# PodWave — AI Podcast Studio

Generate a podcast episode from one or more topics, entirely in the browser. One static HTML file, no server, no build step, no dependencies — works on desktop and mobile.

## What it does

1. **Brief** — enter topics (one per line), pick genre (non-fiction / fiction), format (two hosts or solo), length in minutes, optional style notes.
2. **Research** — for non-fiction, queries Tavily (optional) and collects source links; otherwise uses the LLM's own knowledge.
3. **Script** — the LLM plans an outline of beats, then writes each beat as spoken dialogue, targeting ~150 words/minute. Output is a JSON segment list (`{speaker, text}`) rendered as an editable script.
4. **Audio** — each line is voiced via the configured TTS, decoded with Web Audio, joined with short gaps, peak-normalized and encoded as a downloadable 24 kHz mono WAV. Episodes can be saved to a local library (IndexedDB).

## Configuration (Settings tab, stored in localStorage only)

- **LLM**: any OpenAI-compatible `/chat/completions` endpoint (OpenAI, OpenRouter, Groq, local llama.cpp/Ollama with a `/v1` route). Base URL + key + model.
- **Search**: Tavily or none.
- **TTS**: OpenAI-compatible `/audio/speech` (per-speaker voices), ElevenLabs (per-speaker voice IDs), or the browser's built-in `speechSynthesis` (free, live playback, no file export).
- Settings export/import as JSON (keys excluded unless you opt in).

## Privacy

API keys live in your browser (localStorage / IndexedDB) and are only sent directly to the providers you configure. The page itself contains no analytics and no external scripts.

## Deploy with Docker

```bash
docker build -t podwave .
docker run -d -p 8080:80 --restart unless-stopped --name podwave podwave
# or
docker compose up -d --build
```

Then open http://localhost:8080. The image is `nginx:1.27-alpine` plus the static files (~5 MB); `docker/nginx.conf` adds gzip, SPA fallback and basic security headers. No environment variables are needed — provider keys are configured by each user in the app's Settings tab and never touch the server.

## Run without Docker

Open `public/index.html` directly in a browser, or serve the folder with any static server.

## Known limits / roadmap ideas

- Long episodes: audio is a WAV, so ~2.9 MB per audio-minute; a client-side MP3/AAC encoder would shrink this.
- No CORS from some providers when called from a browser — OpenAI and ElevenLabs work; some proxies block browser origins.
- Ideas: RSS export for feeds, per-beat regeneration, voice preview before full render, episode transcripts alongside audio, PWA manifest for install-on-phone.

## Notes

- The built-in "sample script" button loads **illustrative example data** so the UI can be tried without keys; it is not fact-checked.
- Research text is treated as untrusted data in the prompt (explicit anti-injection instruction).
