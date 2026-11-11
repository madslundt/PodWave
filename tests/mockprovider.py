#!/usr/bin/env python3
"""Mock OpenAI-compatible provider for PodWave tests (LLM + speech). Not shipped."""
import json
import math
import struct
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CTRL = {"beats": 3, "segWords": 40, "delay": 0.0, "failLLM": False, "failTTS": False, "ttsFormat": "wav"}


def wav(seconds=0.35, rate=24000):
    n = int(seconds * rate)
    pcm = b"".join(struct.pack("<h", int(9000 * math.sin(i / 33))) for i in range(n))
    return (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt " +
            struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16) +
            b"data" + struct.pack("<I", len(pcm)) + pcm)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def out(self, code, body, ctype="application/json"):
        if isinstance(body, dict):
            body = json.dumps(body)
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        time.sleep(CTRL["delay"])
        if self.path == "/__control":
            CTRL.update(body)
            return self.out(200, {"ok": True, **CTRL})
        if self.path.endswith("/chat/completions"):
            if CTRL["failLLM"]:
                return self.out(500, {"error": "mock llm down"})
            user = body["messages"][-1]["content"]
            if '"beats"' in user:
                beats = [{"name": f"Beat {i+1}", "words": CTRL["segWords"] * 2,
                          "gist": f"Coverage point {i+1} of the episode."} for i in range(CTRL["beats"])]
                content = json.dumps({"title": "Mock Episode", "description": "A test episode.",
                                      "beats": beats})
            else:
                nseg = 2
                content = json.dumps({"segments": [
                    {"speaker": "A" if i % 2 == 0 else "B",
                     "text": "Spoken line for the test episode. " * 6} for i in range(nseg)]})
            return self.out(200, {"choices": [{"message": {"role": "assistant", "content": content}}]})
        if self.path.endswith("/audio/speech"):
            if CTRL["failTTS"]:
                return self.out(500, {"error": "mock tts down"})
            fmt = body.get("response_format", "wav")
            if fmt == "wav":
                return self.out(200, wav(CTRL.get("ttsSeconds", 0.35)), "audio/wav")
            return self.out(200, b"\xff\xf3\x18\xc4" + b"\x00" * 512, "audio/mpeg")  # fake mp3
        self.out(404, {"error": "mock: " + self.path})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
