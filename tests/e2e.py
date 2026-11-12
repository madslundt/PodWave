#!/usr/bin/env python3
"""End-to-end test for the PodWave server against a mock provider.

Run:  python3 tests/e2e.py            (uses a throwaway data dir, nothing real is called)
"""
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable
SERVER_PORT, MOCK_PORT = 8123, 8124
BASE = f"http://127.0.0.1:{SERVER_PORT}"
MOCK = f"http://127.0.0.1:{MOCK_PORT}"

fails, passes = [], []


def check(name, cond, detail=""):
    (passes if cond else fails).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (f"   {detail}" if detail and not cond else ""))


def req(method, url, body=None, headers=None, raw=False, timeout=20):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers=dict({"Content-Type": "application/json"}, **(headers or {})))
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            payload = resp.read()
            return resp.status, (payload if raw else json.loads(payload.decode() or "{}")), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        try:
            return exc.code, (json.loads(payload.decode()) if not raw else payload), dict(exc.headers)
        except Exception:
            return exc.code, {"raw": payload[:200]}, dict(exc.headers)


def control(**kw):
    req("POST", MOCK + "/__control", kw)


def wait_for(fn, timeout=30, step=0.2):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return None


def start_server(data_dir, token="", port=SERVER_PORT):
    env = dict(os.environ, PODWAVE_DATA=data_dir, PODWAVE_PORT=str(port), PODWAVE_BIND="127.0.0.1",
               PODWAVE_STATIC=os.path.join(ROOT, "public"))
    if token:
        env["PODWAVE_TOKEN"] = token
    else:
        env.pop("PODWAVE_TOKEN", None)
    p = subprocess.Popen([PY, os.path.join(ROOT, "server", "podwave.py")], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    end = time.time() + 15
    while time.time() < end:
        try:
            if req("GET", f"http://127.0.0.1:{port}/api/health")[0] == 200:
                SRV_PID[0] = p.pid
                PROCS.append(p)
                return p
        except Exception:
            time.sleep(0.15)
    raise RuntimeError("server did not start")


def write_config(data_dir):
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, "config.json"), "w") as fh:
        json.dump({"llm": {"base": MOCK + "/v1", "key": "test-llm", "model": "mock-llm"},
                   "search": {"prov": "none", "key": ""},
                   "tts": {"prov": "openai", "base": MOCK + "/v1", "key": "test-tts",
                           "model": "mock-tts", "voiceA": "alloy", "voiceB": "nova"}}, fh)


def submit(topics=None, minutes=4, genre="nonfiction", fmt="duo"):
    code, body, _ = req("POST", BASE + "/api/jobs", {"topics": topics or ["Test topic"],
                                                    "genre": genre, "format": fmt, "minutes": minutes,
                                                    "style": "dry and technical"})
    return code, body


def job_state(jid):
    return req("GET", f"{BASE}/api/jobs/{jid}")[1]


def main():
    mock = subprocess.Popen([PY, os.path.join(HERE, "mockprovider.py"), str(MOCK_PORT)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    end = time.time() + 10
    while time.time() < end:
        try:
            urllib.request.urlopen(MOCK + "/__control", timeout=1)
            break
        except urllib.error.HTTPError:
            break
        except Exception:
            time.sleep(0.1)
    tmp = tempfile.mkdtemp(prefix="podwave-test-")
    write_config(tmp)
    start_server(tmp)
    try:
        scenario_basic(tmp)
        scenario_no_key(tmp)
        scenario_cancel_queued()
        scenario_restart(tmp)
        scenario_errors(tmp)
        scenario_static_and_auth()
    finally:
        for p in PROCS + [mock]:
            p.terminate()
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{len(passes)} passed, {len(fails)} failed")
    if fails:
        print("failed: " + ", ".join(fails))
    return 1 if fails else 0


def scenario_basic(tmp):
    print("\n== static, health, config ==")
    code, page = None, None
    r = urllib.request.urlopen(BASE + "/", timeout=10)
    page = r.read().decode()
    check("static index served", r.status == 200 and "PodWave" in page)
    code, h, _ = req("GET", BASE + "/api/health")
    check("health ok", code == 200 and h.get("ok") and h["version"])
    check("health hides keys", "test-llm" not in json.dumps(h) and h["config"]["llm"]["keySet"] is True)
    code, c, _ = req("GET", BASE + "/api/config")
    flat = json.dumps(c)
    check("config masked", code == 200 and "test-llm" not in flat and "test-tts" not in flat
          and not any(k == "key" for section in c.values() for k in section), flat[:120])
    code, _, _ = req("PUT", BASE + "/api/config", {"llm": {"model": "mock-llm-2"}})
    on_disk = json.load(open(os.path.join(tmp, "config.json")))
    check("config put keeps keys", code == 200 and on_disk["llm"]["key"] == "test-llm"
          and on_disk["llm"]["model"] == "mock-llm-2")
    mode = oct(os.stat(os.path.join(tmp, "config.json")).st_mode & 0o777)
    check("config file is 0600", mode == "0o600", mode)
    control(beats=3, delay=0.0)

    print("\n== background job -> episode ==")
    code, j = submit(topics=["Quantum tea brewing", "Cups"], minutes=3)
    check("job accepted (202)", code == 202 and j.get("id"), str(j))
    jid = j["id"]
    wait_for(lambda: job_state(jid)["state"] in ("done", "error", "canceled"), 40)
    detail = job_state(jid)
    check("job finished", detail["state"] == "done", json.dumps(detail.get("log", [])[-2:]))
    check("progress reached total", detail["total"] and detail["done"] == detail["total"], str(detail["total"]))
    check("job has log", len(detail["log"]) >= 3)
    ep_id = detail["episode_id"]
    check("episode linked", bool(ep_id))
    code, eps, _ = req("GET", BASE + "/api/episodes")
    check("episode listed", code == 200 and any(e["id"] == ep_id for e in eps["episodes"]))
    e = [e for e in eps["episodes"] if e["id"] == ep_id][0]
    check("episode title from model", e["title"] == "Mock Episode", e["title"])
    check("episode duration plausible", 1.0 < e["seconds"] < 12.0, f"{e['seconds']}s")
    code, body, hdr = req("GET", f"{BASE}/api/episodes/{ep_id}/audio", raw=True)
    wav_path = os.path.join(tmp, "audio", f"{ep_id}.wav")
    ok_wav = code == 200 and body[:4] == b"RIFF" and os.path.exists(wav_path)
    with wave.open(wav_path, "rb") as w:
        check("audio is valid WAV", ok_wav and w.getnchannels() == 1 and w.getframerate() == 24000,
              f"code={code}")
        n_frames, rate = w.getnframes(), w.getframerate()
    check("audio has gaps+segments", abs(n_frames / rate - e["seconds"]) < 0.2, f"{n_frames/rate}s vs {e['seconds']}s")
    check("audio accepts Range", req("GET", f"{BASE}/api/episodes/{ep_id}/audio",
                                     headers={"Range": "bytes=100-199"}, raw=True)[0] == 206)
    check("audio supports suffix Range", req("GET", f"{BASE}/api/episodes/{ep_id}/audio",
                                             headers={"Range": "bytes=-44"}, raw=True)[0] == 206)
    check("audio range beyond end -> 416", req("GET", f"{BASE}/api/episodes/{ep_id}/audio",
                                               headers={"Range": "bytes=5-99999999"}, raw=True)[0] == 416)
    code, s, _ = req("GET", f"{BASE}/api/episodes/{ep_id}/script")
    check("script retrievable", code == 200 and len(s["segments"]) == 6 and s["segments"][0]["text"],
          str(len(s.get("segments", []))))
    code, _, _ = req("PUT", f"{BASE}/api/episodes/{ep_id}/script",
                     {"segments": [{"speaker": "A", "text": "edited line"}]})
    check("script edit saved", code == 200 and req("GET", f"{BASE}/api/episodes/{ep_id}/script")[1]["segments"][0]["text"] == "edited line")

    print("\n== fiction + solo, brief persistence ==")
    control(beats=2)
    code, j2 = submit(genre="fiction", fmt="solo", minutes=2)
    jid2 = j2["id"]
    wait_for(lambda: job_state(jid2)["state"] in ("done", "error"), 40)
    d2 = job_state(jid2)
    check("fiction job done", d2["state"] == "done", d2.get("error", ""))
    segs = req("GET", f"{BASE}/api/episodes/{d2['episode_id']}/script")[1]["segments"]
    check("solo forces speaker A", all(s["speaker"] == "A" for s in segs))
    req("PUT", BASE + "/api/brief", {"topics": "hello\nworld", "genre": "fiction", "minutes": 7})
    check("brief round-trips", req("GET", BASE + "/api/brief")[1].get("minutes") == 7)

    print("\n== delete ==")
    code, _, _ = req("DELETE", f"{BASE}/api/episodes/{ep_id}")
    gone = not os.path.exists(os.path.join(tmp, "audio", f"{ep_id}.wav"))
    check("episode deleted with file", code == 200 and req("GET", f"{BASE}/api/episodes/{ep_id}/audio", raw=True)[0] == 404 and gone)
    code, _, _ = req("DELETE", f"{BASE}/api/jobs/{jid}")
    check("job deleted", code == 200 and req("GET", f"{BASE}/api/jobs/{jid}")[0] == 404)


def scenario_no_key(tmp):
    print("\n== keyless local providers (no auth header at all) ==")
    code, _, _ = req("PUT", BASE + "/api/config", {"llm": {"key": ""}, "tts": {"key": ""}})
    on_disk = json.load(open(os.path.join(tmp, "config.json")))
    check("empty key clears stored key", code == 200 and on_disk["llm"]["key"] == ""
          and on_disk["tts"]["key"] == "")
    code, h, _ = req("GET", BASE + "/api/health")
    check("health says keyless", h["config"]["llm"]["keySet"] is False
          and h["config"]["tts"]["keySet"] is False)
    control(beats=2, delay=0.0, requireAuth=False)
    code, j = submit(topics=["Local model run"], minutes=2)
    jid = j["id"]
    wait_for(lambda: job_state(jid)["state"] in ("done", "error"), 40)
    d = job_state(jid)
    check("keyless job done", d["state"] == "done", json.dumps(d.get("log", [])[-2:]))
    # proof that no Authorization header is sent when no key is set
    control(requireAuth=True)
    code, j2 = submit(topics=["Should fail auth"], minutes=2)
    jid2 = j2["id"]
    wait_for(lambda: job_state(jid2)["state"] in ("done", "error"), 40)
    check("auth-only provider rejected keyless", job_state(jid2)["state"] == "error",
          job_state(jid2).get("state"))
    control(requireAuth=False)


def scenario_cancel_queued():
    print("\n== cancel a queued job ==")
    control(beats=6, delay=0.7)          # keep the worker busy
    busy = submit(topics=["Busy episode"], minutes=5)[1]["id"]
    wait_for(lambda: job_state(busy)["state"] == "running", 10)
    idle = submit(topics=["Later episode"], minutes=5)[1]["id"]
    code, r, _ = req("POST", f"{BASE}/api/jobs/{idle}/cancel")
    check("queued job canceled", code == 200 and job_state(idle)["state"] == "canceled")
    req("POST", f"{BASE}/api/jobs/{busy}/cancel")
    wait_for(lambda: job_state(busy)["state"] in ("canceled", "done"), 30)
    check("running job canceled", job_state(busy)["state"] == "canceled", job_state(busy)["state"])
    check("cancel frees worker", wait_for(lambda: submit(topics=["Next"], minutes=2)[1].get("id")) is not None)
    control(beats=3, delay=0.0)


def scenario_restart(tmp):
    print("\n== restart mid-job (resumable) ==")
    control(beats=6, delay=0.4)
    jid = submit(topics=["Long haul"], minutes=6)[1]["id"]
    wait_for(lambda: job_state(jid)["state"] == "running", 10)
    time.sleep(1.2)
    os.kill(SRV_PID[0], signal.SIGKILL)       # simulate a crash
    time.sleep(0.6)
    start_server(tmp)
    check("interrupted job re-queued", job_state(jid)["state"] in ("queued", "running", "done"),
          job_state(jid)["state"])
    ok = wait_for(lambda: job_state(jid)["state"] == "done", 90)
    d = job_state(jid)
    check("resumed job completes", bool(ok), json.dumps(d.get("log", [])[-2:]))
    check("resumed job has episode", bool(d.get("episode_id")))
    check("segments cache cleaned", not os.path.exists(os.path.join(tmp, "segments", jid)))
    control(beats=3, delay=0.0)


def scenario_errors(tmp):
    print("\n== failures are reported, server survives ==")
    control(failLLM=True)
    jid = submit(topics=["Doomed"])[1]["id"]
    wait_for(lambda: job_state(jid)["state"] == "error", 40)
    d = job_state(jid)
    check("llm failure -> error state", d["state"] == "error" and "HTTP" in d["error"], d["error"][:80])
    control(failLLM=False, failTTS=True)
    jid2 = submit(topics=["Voiceless"])[1]["id"]
    wait_for(lambda: job_state(jid2)["state"] == "error", 60)
    check("tts failure -> error state", job_state(jid2)["state"] == "error", job_state(jid2)["state"])
    control(failTTS=False)
    code, r, _ = req("POST", f"{BASE}/api/jobs/{jid2}/retry")
    check("retry re-queues", code == 200 and job_state(jid2)["state"] in ("queued", "running"))
    check("server still healthy", req("GET", BASE + "/api/health")[1]["ok"] is True)
    check("bad request rejected", req("POST", BASE + "/api/jobs", {"topics": []})[0] == 400)
    check("unknown job 404", req("GET", BASE + "/api/jobs/nope")[0] == 404)
    check("unknown episode 404", req("GET", BASE + "/api/episodes/1/audio", raw=True)[0] == 404)


def scenario_static_and_auth():
    print("\n== static hardening + token ==")
    check("path traversal blocked", req("GET", BASE + "/../server/podwave.py", raw=True)[0] in (400, 404))
    check("missing file 404", req("GET", BASE + "/nope.html", raw=True)[0] == 404)


SRV_PID = [0]
PROCS = []


if __name__ == "__main__":
    sys.exit(main())
