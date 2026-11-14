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


def start_server(data_dir, port=SERVER_PORT):
    env = dict(os.environ, PODWAVE_DATA=data_dir, PODWAVE_PORT=str(port), PODWAVE_BIND="127.0.0.1",
               PODWAVE_STATIC=os.path.join(ROOT, "public"))
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


def submit(topics=None, minutes=4, genre="nonfiction", fmt="duo", extra=None):
    body = {"topics": topics or ["Test topic"], "genre": genre, "format": fmt, "minutes": minutes,
            "style": "dry and technical"}
    body.update(extra or {})
    code, got, _ = req("POST", BASE + "/api/jobs", body)
    return code, got


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
        scenario_story_fields(tmp)
        scenario_hosts(tmp)
        scenario_chat(tmp)
        scenario_no_key(tmp)
        scenario_cancel_queued()
        scenario_restart(tmp)
        scenario_errors(tmp)
        scenario_connection_tests(tmp)
        scenario_static()
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
    check("health hides keys", "test-llm" not in json.dumps(h) and h["config"]["llms"][0]["keySet"] is True)
    code, c, _ = req("GET", BASE + "/api/config")
    flat = json.dumps(c)
    check("config masked", code == 200 and "test-llm" not in flat and "test-tts" not in flat
          and not any(k == "key" for section in c.values() for k in section), flat[:120])
    code, current, _ = req("GET", BASE + "/api/config")
    current["llms"][0]["model"] = "mock-llm-2"
    current["llms"].append({"id":"second","name":"Second","base":MOCK+"/v1","model":"mock-llm-fast"})
    code, _, _ = req("PUT", BASE + "/api/config", current)
    on_disk = json.load(open(os.path.join(tmp, "config.json")))
    check("config put keeps keys and multiple models", code == 200 and on_disk["llms"][0]["key"] == "test-llm"
          and on_disk["llms"][0]["model"] == "mock-llm-2" and len(on_disk["llms"]) == 2)
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
    last_auto = (req("GET", MOCK + "/__last")[1].get("chat") or {}).get("messages", [{}])[-1].get("content", "")
    check("auto language follows the topics", "same language as the topic" in last_auto, last_auto[:100])
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


def scenario_story_fields(tmp):
    print("\n== story brief, pacing, voice direction ==")
    control(beats=3, delay=0.0, rejectInstr=False)
    code, j = submit(genre="fiction", fmt="duo", minutes=3, extra={
        "tone": "Thriller / mystery", "characters": "Mara, a salvage pilot",
        "setting": "a derelict shipyard station", "twist": True, "language": "Dansk"})
    jid = j["id"]
    wait_for(lambda: job_state(jid)["state"] in ("done", "error"), 40)
    d = job_state(jid)
    check("fiction fields stored in brief", d["state"] == "done" and d["brief"].get("twist") is True
          and d["brief"].get("tone") == "Thriller / mystery", json.dumps(d.get("brief"))[:160])
    check("language stored in brief", d["brief"].get("language") == "Dansk", json.dumps(d.get("brief"))[:160])
    last = req("GET", MOCK + "/__last")[1]
    last_user = (last.get("chat") or {}).get("messages", [{}])[-1].get("content", "")
    check("story brief reaches the model", "Story brief" in last_user and "salvage pilot" in last_user,
          last_user[:120])
    check("language reaches the writing prompt", "Language: Dansk" in last_user, last_user[:120])
    check("voice direction sent to TTS", "mood" in (last.get("speech") or {}).get("instructions", ""),
          json.dumps(last.get("speech"))[:160])
    check("language in voice instructions", "Dansk" in (last.get("speech") or {}).get("instructions", ""),
          json.dumps(last.get("speech"))[:160])
    ep = [e for e in req("GET", BASE + "/api/episodes")[1]["episodes"] if e["id"] == d["episode_id"]][0]
    # 6 lines x 0.35 s + gaps: three quick hand-offs (0.08) + two beat breaks (0.30)
    check("beat-aware pacing in the joined audio",
          abs(ep["seconds"] - (6 * 0.35 + 3 * 0.08 + 2 * 0.30)) < 0.05, f"{ep['seconds']}s")
    with wave.open(os.path.join(tmp, "audio", str(ep["id"]) + ".wav"), "rb") as w:
        data = w.readframes(w.getnframes())
    peak = max(abs(int.from_bytes(data[i:i + 2], "little", signed=True)) for i in range(0, len(data), 2))
    check("lines loudness-matched near target", 20000 <= peak <= 32767, f"peak={peak}")

    # a provider that rejects the 'instructions' field must still finish, voicing without it
    control(rejectInstr=True)
    jid2 = submit(genre="fiction", minutes=2)[1]["id"]
    wait_for(lambda: job_state(jid2)["state"] in ("done", "error"), 40)
    check("instructions rejected -> job still finishes", job_state(jid2)["state"] == "done",
          job_state(jid2).get("error", ""))
    check("fallback voice call omits instructions",
          "instructions" not in (req("GET", MOCK + "/__last")[1].get("speech") or {}))
    control(rejectInstr=False)


def scenario_hosts(tmp):
    """Hosts get real names: typed ones are used verbatim, blanks get random ones, and nobody
    is ever called "A" or "B" in what the model sees or what the app shows."""
    print("\n== host names ==")
    control(beats=2, delay=0.0)
    jid = submit(topics=["Naming the hosts"], minutes=2,
                 extra={"hosts": ["Mads", "Freja"], "language": "Dansk"})[1]["id"]
    wait_for(lambda: job_state(jid)["state"] in ("done", "error"), 40)
    d = job_state(jid)
    check("typed names stored in brief", d["state"] == "done" and d["brief"].get("hosts") == ["Mads", "Freja"],
          json.dumps(d.get("brief"))[:180])
    prompt = (req("GET", MOCK + "/__last")[1].get("chat") or {}).get("messages", [{}])[-1].get("content", "")
    check("names reach the model", "Mads" in prompt and "Freja" in prompt, prompt[:120])
    check("model is not told 'Host A'", 'speaker "A" (lead' not in prompt, prompt[:120])
    script = req("GET", f"{BASE}/api/episodes/{d['episode_id']}/script")[1]
    check("episode script carries names", script.get("hosts") == ["Mads", "Freja"], json.dumps(script)[:150])
    req("POST", f"{BASE}/api/jobs/{jid}/retry")
    wait_for(lambda: job_state(jid)["state"] in ("done", "error"), 40)
    check("retry keeps the same names", job_state(jid)["brief"].get("hosts") == ["Mads", "Freja"],
          json.dumps(job_state(jid).get("brief", {}))[:180])

    jid2 = submit(topics=["Random names"], minutes=2)[1]["id"]
    wait_for(lambda: job_state(jid2)["state"] in ("done", "error"), 40)
    names = job_state(jid2)["brief"].get("hosts") or []
    check("blank names get two random names", len(names) == 2 and all(names) and names[0] != names[1],
          json.dumps(names))
    check("random names reach the model",
          all(n in (req("GET", MOCK + "/__last")[1].get("chat") or {}).get("messages", [{}])[-1]
              .get("content", "") for n in names))

    jid3 = submit(fmt="solo", minutes=2, topics=["Solo naming"])[1]["id"]
    wait_for(lambda: job_state(jid3)["state"] in ("done", "error"), 40)
    solo = job_state(jid3)["brief"].get("hosts") or []
    check("solo gets one name", len(solo) == 1 and bool(solo[0]), json.dumps(solo))

    jid4 = submit(topics=["Half named"], minutes=2, extra={"hosts": ["Mads", ""]})[1]["id"]
    wait_for(lambda: job_state(jid4)["state"] in ("done", "error"), 40)
    half = job_state(jid4)["brief"].get("hosts") or []
    check("blank slot is filled in place", half[0] == "Mads" and len(half) == 2 and bool(half[1]),
          json.dumps(half))

    code, r, _ = req("POST", BASE + "/api/brief/chat",
                     {"messages": [{"role": "user", "content": "Call them Freja and Mikkel"}],
                      "brief": {"topics": ["x"], "genre": "nonfiction", "format": "duo", "minutes": 5}})
    check("chat names are kept", code == 200, json.dumps(r)[:150])
    control(failLLM=False, chatReply="Noted.", chatUpdates={"hosts": ["Freja", "Mikkel"]}, chatDone=True)
    code, r, _ = req("POST", BASE + "/api/brief/chat",
                     {"messages": [{"role": "user", "content": "Call them Freja and Mikkel"}],
                      "brief": {"topics": ["x"], "genre": "nonfiction", "format": "duo", "minutes": 5}})
    check("chat name update survives cleanup", r.get("updates") == {"hosts": ["Freja", "Mikkel"]},
          json.dumps(r)[:180])
    control(chatUpdates={})


def scenario_chat(tmp):
    print("\n== story-setup chat ==")
    control(failLLM=False, chatReply="What should the main character be after?",
            chatUpdates={"tone": "Sci-fi", "minutes": 12}, chatDone=False)
    body = {"messages": [{"role": "user", "content": "A heist on a generation ship"},
                         {"role": "assistant", "content": "Ooh — two hosts or solo?"},
                         {"role": "user", "content": "Two rival thieves. Make it 12 minutes."}],
            "brief": {"topics": ["generation-ship heist"], "genre": "fiction", "format": "duo",
                      "minutes": 10, "characters": "Mara, a salvage pilot"}}
    code, r, _ = req("POST", BASE + "/api/brief/chat", body)
    check("chat answers the user", code == 200 and r.get("reply") == "What should the main character be after?",
          json.dumps(r)[:150])
    check("chat suggests field updates", r.get("updates") == {"tone": "Sci-fi", "minutes": 12}, json.dumps(r)[:200])
    check("chat not done yet", r.get("done") is False, json.dumps(r)[:100])
    msgs = (req("GET", MOCK + "/__last")[1].get("chat") or {}).get("messages", [])
    joined = json.dumps(msgs)
    check("chat sends the current brief to the model", "Mara, a salvage pilot" in joined
          and "generation-ship heist" in joined, joined[:180])
    check("chat sends the conversation", msgs and msgs[0]["role"] == "system"
          and "Two rival thieves" in joined, joined[:180])

    control(chatUpdates={"minutes": 999, "genre": "western", "topics": ["x"] * 30, "twist": "yes", "bogus": 1})
    code, r, _ = req("POST", BASE + "/api/brief/chat", {"messages": [{"role": "user", "content": "go"}]})
    u = r.get("updates", {})
    check("bad suggestions clamped or dropped", code == 200 and u.get("minutes") == 60 and "genre" not in u
          and len(u.get("topics", [])) == 8 and u.get("twist") is True and "bogus" not in u, json.dumps(u)[:220])

    control(chatDone=True, chatUpdates={})
    code, r, _ = req("POST", BASE + "/api/brief/chat", {"messages": [{"role": "user", "content": "that's enough, build it"}]})
    check("chat can end with done", code == 200 and r.get("done") is True and r.get("updates") == {},
          json.dumps(r)[:150])
    control(chatDone=False, chatReply="", chatUpdates={})

    check("chat needs a user turn", req("POST", BASE + "/api/brief/chat", {"messages": []})[0] == 400)
    control(failLLM=True)
    code, r, _ = req("POST", BASE + "/api/brief/chat", {"messages": [{"role": "user", "content": "hello?"}]})
    check("chat reports a model failure", code == 502 and "model call failed" in str(r.get("error", "")),
          json.dumps(r)[:150])
    control(failLLM=False)
    check("chat is POST only", req("GET", BASE + "/api/brief/chat")[0] == 405)
    check("brief draft endpoints unaffected", req("GET", BASE + "/api/brief")[0] == 200)
    check("server healthy after chat", req("GET", BASE + "/api/health")[1]["ok"] is True)


def scenario_no_key(tmp):
    print("\n== keyless local providers (no auth header at all) ==")
    code, config, _ = req("GET", BASE + "/api/config")
    config["llms"][0]["key"] = ""
    config["ttss"][0]["key"] = ""
    code, _, _ = req("PUT", BASE + "/api/config", config)
    on_disk = json.load(open(os.path.join(tmp, "config.json")))
    check("empty key clears stored key", code == 200 and on_disk["llms"][0]["key"] == ""
          and on_disk["ttss"][0]["key"] == "")
    code, h, _ = req("GET", BASE + "/api/health")
    check("health says keyless", h["config"]["llms"][0]["keySet"] is False
          and h["config"]["ttss"][0]["keySet"] is False)
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


def api_test(target, **over):
    """POST /api/test -> (status, body, per-provider rows)."""
    code, body, _ = req("POST", BASE + "/api/test", dict({"target": target}, **over))
    if not isinstance(body, dict):      # never happens for this endpoint; keeps the types honest
        return code, {}, {}
    return code, body, body.get("results", {})


def scenario_connection_tests(tmp):
    print("\n== settings test endpoint ==")
    control(failLLM=False, failTTS=False, requireAuth=False, beats=2, emptyReply=False, ttsForceMp3=False)
    cfg_path = os.path.join(tmp, "config.json")
    before = json.load(open(cfg_path))

    code, body, res = api_test("llm")
    check("test llm passes on a working model",
          code == 200 and body.get("ok") and res.get("llm", {}).get("state") == "ok", json.dumps(res)[:180])
    code, body, res = api_test("tts")
    tts = res.get("tts", {})
    check("test tts passes and measures the audio", code == 200 and tts.get("state") == "ok"
          and "s WAV" in tts.get("detail", "") and tts.get("ms", -1) >= 0, json.dumps(tts)[:180])
    code, body, res = api_test("search")
    check("test search reports 'off' rather than failing",
          code == 200 and res.get("search", {}).get("state") == "unset" and body.get("ok") is True,
          json.dumps(res)[:180])
    code, body, res = api_test("all")
    check("test all covers every provider", code == 200 and set(res) == {"llm", "search", "tts"}, json.dumps(res)[:180])

    control(emptyReply=True)
    code, body, res = api_test("llm")
    check("an empty model reply is a failure", res.get("llm", {}).get("state") == "fail"
          and "empty" in res.get("llm", {}).get("detail", ""), json.dumps(res)[:180])
    control(emptyReply=False, failLLM=True)
    code, body, res = api_test("llm")
    check("a model error is reported with the HTTP code", res.get("llm", {}).get("state") == "fail"
          and "HTTP 500" in res.get("llm", {}).get("detail", ""), json.dumps(res)[:180])
    control(failLLM=False)
    code, body, res = api_test("all")
    check("an all-good run reports ok", code == 200 and body.get("ok") is True, json.dumps(res)[:180])

    code, body, res = api_test("tts", tts={"prov": "elevenlabs"})
    check("elevenlabs without a key fails with a fix", res.get("tts", {}).get("state") == "fail"
          and "ElevenLabs key" in res.get("tts", {}).get("detail", ""), json.dumps(res)[:180])
    code, body, res = api_test("llm", llm={"base": "ftp://example.invalid"})
    check("a bad base URL fails without a crash", code == 200 and res.get("llm", {}).get("state") == "fail"
          and "http(s)" in res.get("llm", {}).get("detail", ""), json.dumps(res)[:180])

    control(ttsForceMp3=True)
    code, body, res = api_test("tts")
    check("audio the server cannot splice is reported", res.get("tts", {}).get("state") == "fail",
          json.dumps(res.get("tts", {}))[:180])
    control(ttsForceMp3=False)

    # values typed into Settings are used for the check but never persisted
    control(requireAuth=True)
    profile_id = req("GET", BASE + "/api/config")[1]["defaultLlm"]
    code, echo, res = api_test("llm", llm={"id": profile_id, "key": "one-shot-key"})
    check("a typed-in key is used for the test", res.get("llm", {}).get("state") == "ok", json.dumps(res)[:180])
    code, body, res = api_test("llm", llm={"id": profile_id})
    check("a keyless config fails an auth-only provider", res.get("llm", {}).get("state") == "fail",
          json.dumps(res)[:180])
    control(requireAuth=False)
    check("a test never writes config.json", json.load(open(cfg_path)) == before)
    check("a test never echoes a key back", "one-shot-key" not in json.dumps(echo))
    control(failLLM=True)
    code, body, res = api_test("all")
    check("one failing provider reddens the whole run", code == 200 and body.get("ok") is False
          and res.get("tts", {}).get("state") == "ok", json.dumps(res)[:180])
    control(failLLM=False)

    code, body, res = api_test("llm", llm={"base": "http://127.0.0.1:1/v1"})
    check("a refused connection is reported plainly", res.get("llm", {}).get("state") == "fail"
          and "Connection refused" in res.get("llm", {}).get("detail", ""), json.dumps(res)[:180])
    check("unknown target 400", req("POST", BASE + "/api/test", {"target": "nope"})[0] == 400)
    check("unknown provider 400", req("POST", BASE + "/api/test",
          {"target": "search", "search": {"prov": "bogus"}})[0] == 400)
    check("the test endpoint is POST only", req("GET", BASE + "/api/test")[0] == 405)
    check("tests leave the server healthy", req("GET", BASE + "/api/health")[1]["ok"] is True)


def scenario_static():
    print("\n== static hardening, public api ==")
    check("path traversal blocked", req("GET", BASE + "/../server/podwave.py", raw=True)[0] in (400, 404))
    check("missing file 404", req("GET", BASE + "/nope.html", raw=True)[0] == 404)
    code, h, _ = req("GET", BASE + "/api/health")
    check("health reports a public server", code == 200 and h.get("public") is True
          and "auth" not in h, json.dumps(h)[:120])


SRV_PID = [0]
PROCS = []


if __name__ == "__main__":
    sys.exit(main())
