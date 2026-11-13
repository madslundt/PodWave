#!/usr/bin/env python3
"""Mock OpenAI-compatible provider for PodWave tests (LLM + speech). Not shipped."""
import json
import math
import struct
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CTRL = {"beats": 3, "segWords": 40, "delay": 0.0, "failLLM": False, "failTTS": False, "ttsFormat": "wav",
        "requireAuth": False, "ttsForceMp3": False, "emptyReply": False, "rejectInstr": False}
LAST = {}  # last request bodies seen, for tests that assert what the server actually sent


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
        self.send_header("Access-Control-Allow-Origin", "*")  # browsers call the mock directly in tests
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if self.path == "/__last":
            return self.out(200, LAST)
        self.out(404, {"error": "mock: " + self.path})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        time.sleep(CTRL["delay"])
        if self.path == "/__control":
            CTRL.update(body)
            return self.out(200, {"ok": True, **CTRL})
        if CTRL["requireAuth"] and not self.headers.get("Authorization"):
            return self.out(401, {"error": {"message": "mock: missing Authorization header"}})
        if self.path.endswith("/chat/completions"):
            LAST["chat"] = body
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
            if CTRL.get("emptyReply"):
                content = "   "
            return self.out(200, {"choices": [{"message": {"role": "assistant", "content": content}}]})
        if self.path.endswith("/audio/speech"):
            LAST["speech"] = body
            if CTRL["failTTS"]:
                return self.out(500, {"error": "mock tts down"})
            if CTRL["rejectInstr"] and "instructions" in body:
                return self.out(400, {"error": {"message": "unknown parameter: instructions"}})
            fmt = body.get("response_format", "wav")
            if CTRL.get("ttsForceMp3"):
                return self.out(200, b"\xff\xf3\x18\xc4" + b"\x00" * 512, "audio/mpeg")  # provider ignores the format
            if fmt == "wav":
                return self.out(200, wav(CTRL.get("ttsSeconds", 0.35)), "audio/wav")
            return self.out(200, b"\xff\xf3\x18\xc4" + b"\x00" * 512, "audio/mpeg")  # fake mp3
        self.out(404, {"error": "mock: " + self.path})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
    host = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1"
    ThreadingHTTPServer((host, port), H).serve_forever()
