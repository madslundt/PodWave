#!/usr/bin/env python3
"""End-to-end test for the PodWave server against a mock provider.

Run:  python3 tests/e2e.py            (uses a throwaway data dir, nothing real is called)
"""
import io
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
from pathlib import Path

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
    env = dict(test_env(), PODWAVE_DATA=data_dir, PODWAVE_PORT=str(port), PODWAVE_BIND="127.0.0.1",
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


def test_env():
    return {k: v for k, v in os.environ.items() if not k.startswith("PODWAVE_")}


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


def quality_cases():
    path = Path(ROOT) / "docs" / "quality-briefs.json"
    return json.loads(path.read_text(encoding="utf-8"))


def check_writer_input(case_id, topics, language, hosts, done):
    calls = req("GET", MOCK + "/__last")[1].get("chats", [])
    prompts = [call["messages"][-1]["content"] for call in calls]
    check(f"{case_id}: fixture reaches outline and beat writer", done and len(prompts) >= 2
          and '"beats"' in prompts[0] and '"segments"' in prompts[1]
          and all(all(t in p for t in topics) and f"Language: {language}" in p
                  and all(host in p for host in hosts) for p in prompts))


def scenario_quality_briefs():
    print("\n== listening fixtures: mock input contracts only ==")
    cases = quality_cases()
    manual = [case for case in cases if "brief" in case]
    check("listening set covers two languages, both genres and formats, and scheduled digests",
          len(cases) == len({case["id"] for case in cases}) == 6
          and {(c["brief"]["genre"], c["brief"]["format"], c["brief"]["language"]) for c in manual}
          == {("fiction", "solo", "English"), ("fiction", "duo", "Dansk"),
              ("nonfiction", "duo", "English"), ("nonfiction", "solo", "Dansk")}
          and {c["language"] for c in cases if "show" in c} == {"English", "Dansk"})
    for case in manual:
        control(beats=2)
        brief = case["brief"]
        code, job, _ = req("POST", BASE + "/api/jobs", brief)
        jid = job.get("id")
        settled = code == 202 and jid and wait_for(lambda: job_state(jid)["state"] in ("done", "error"), 30)
        check_writer_input(case["id"], brief["topics"], brief["language"], brief["hosts"],
                           settled and job_state(jid)["state"] == "done")
    control(beats=3)


def scenario_quality_digests():
    """Run a due drop and its writer from scored fixtures, without fetching a real feed."""
    with tempfile.TemporaryDirectory(prefix="podwave-quality-digest-") as tmp:
        os.environ["PODWAVE_DATA"] = tmp
        sys.path.insert(0, os.path.join(ROOT, "server"))
        import podwave as pw

        write_config(tmp)
        for case in (c for c in quality_cases() if "show" in c):
            control(beats=2)
            show, language = case["show"], case["language"]
            cfg = pw.load_config()
            cfg["auto"] = pw.clean_auto({"enabled": True, "language": language, "shows": [show]})
            pw.save_config(cfg)
            now = time.time()
            pw._st_set("last_poll", now)
            ready = pw.load_config()["auto"]["shows"][0]
            sid = show["id"]
            pw._st_set("sched:" + sid, json.dumps([ready[k] for k in ("kind", "cadence", "hour", "minute", "weekday")]))
            pw._st_set("next:" + sid, now - 1)
            for i, title in enumerate(case["headlines"]):
                url = f"https://example.invalid/{sid}/{i}"
                pw.q("INSERT INTO seen(url_hash,url,title,source,first,last,score,tags,used_by) VALUES(?,?,?,?,?,?,?,?,?)",
                     (url, url, title, "quality fixture", now, now, 9, json.dumps([sid]), "[]"))
            pw.auto_tick(now)
            series_id = pw._st_get("series:" + sid, "")
            jobs = pw.rows("SELECT id FROM jobs WHERE series_id=? AND state='queued'", (series_id,))
            check(f"{case['id']}: due show queues one local fixture job", bool(series_id) and len(jobs) == 1)
            if not series_id or len(jobs) != 1:
                continue
            jid = jobs[0]["id"]
            pw.run_job(jid)
            job = pw.one("SELECT state,episode_id FROM jobs WHERE id=?", (jid,))
            check_writer_input(case["id"], case["headlines"], language, show["hosts"],
                               job["state"] == "done" and bool(job["episode_id"]))
        pw.db().close()
    print(f"\n{len(passes)} scheduled fixture checks passed, {len(fails)} failed")
    return bool(fails)


def scenario_auto():
    """Scheduler internals run in-process (threads only start in __main__), against their own
    throwaway data dir — deterministic `now` values instead of clock racing."""
    data = tempfile.mkdtemp(prefix="podwave-auto-test-")
    old_data = os.environ.get("PODWAVE_DATA")
    os.environ["PODWAVE_DATA"] = data
    os.makedirs(data, exist_ok=True)
    sys.path.insert(0, os.path.join(ROOT, "server"))
    try:
        import podwave as pw
        pw.DATA = data
        pw.DB_PATH = os.path.join(data, "podwave.db")
        pw._db = None
        two_plans = [pw.manual_episode_plan(i, 2) for i in range(1, 3)]
        four_plans = [pw.manual_episode_plan(i, 4) for i in range(1, 5)]
        check("two-episode plan reserves payoff for actual finale",
              "Establish" in two_plans[0] and "satisfying payoff" in two_plans[1]
              and "payoff" not in two_plans[0])
        check("four-episode plan gives distinct middle roles and final payoff",
              len(set(four_plans)) == 4 and "Middle episode 2" in four_plans[1]
              and "Middle episode 3" in four_plans[2] and "satisfying payoff" in four_plans[3]
              and "satisfying payoff" not in " ".join(four_plans[1:3]))
        a = pw.clean_auto({"enabled": True, "feeds": ["https://example.org/feed"],
                           "watch": ["AI"], "cadence": "weekly", "weekday": 4,
                           "hour": 7, "minute": 30, "stories": 3, "minutes": 12})
        s0 = a["shows"][0]
        check("legacy auto config migrates to a show",
              s0["kind"] == "digest" and s0["cadence"] == "weekly" and s0["weekday"] == 4
              and s0["stories"] == 3 and s0["feeds"] == ["https://example.org/feed"] and bool(s0["id"]),
              json.dumps(s0)[:160])
        a = pw.clean_auto({"enabled": True, "maxAgeHours": 48, "pollMinutes": 180, "shows": [
            {"id": "news", "name": "News", "kind": "digest", "watch": ["AI"], "hour": 7, "stories": 4},
            {"id": "bed", "name": "Bedtime", "kind": "story", "prompt": "One\nTwo",
             "cadence": "weekly", "weekday": 6, "minutes": 5}]})
        cfg = pw.load_config()
        cfg["auto"] = a
        with open(os.path.join(data, "config.json"), "w") as fh:
            json.dump({"auto": a}, fh)
        now = time.time()
        j1, _ = pw.plan_show(a["shows"][1], a, cfg, now)
        j2, _ = pw.plan_show(a["shows"][1], a, cfg, now)
        b1 = json.loads(pw.one("SELECT brief FROM jobs WHERE id=?", (j1,))["brief"])
        b2 = json.loads(pw.one("SELECT brief FROM jobs WHERE id=?", (j2,))["brief"])
        r1 = pw.one("SELECT series_id, ep_no FROM jobs WHERE id=?", (j1,))
        r2 = pw.one("SELECT series_id, ep_no FROM jobs WHERE id=?", (j2,))
        ser = pw.one("SELECT * FROM series WHERE id=?", (r1["series_id"],))
        check("automation drops land in one open-ended series",
              r1["series_id"] == r2["series_id"] and (r1["ep_no"], r2["ep_no"]) == (1, 2)
              and ser["total"] == 0 and ser["source"] == "auto" and ser["title"] == "Bedtime",
              json.dumps(dict(ser))[:160])
        check("story automation queues a fiction brief",
              b1["genre"] == "fiction" and b2["genre"] == "fiction", json.dumps(b1)[:140])
        check("story ideas rotate per drop",
              b1["topics"] == ["One"] and b2["topics"] == ["Two"], str((b1["topics"], b2["topics"])))
        check("automation hosts remain stable across drops and show rename",
              b1.get("hosts") == b2.get("hosts") and len(b1.get("hosts") or []) == 2
              and pw.clean_auto({"enabled": True, "shows": [dict(a["shows"][1], name="Renamed") ]})
                  ["shows"][0]["hosts"] == b1["hosts"])
        renamed = pw.clean_auto({"enabled": True, "shows": [dict(a["shows"][1], name="Renamed")]})
        pw.save_config({**cfg, "auto": renamed})
        check("saved show rename preserves persisted host identity",
              pw.load_config()["auto"]["shows"][0]["hosts"] == b1["hosts"])
        pw.save_config({**cfg, "auto": a})
        for h, tags, used in [("a", "News", "[]"), ("b", "Other", "[]"),
                              ("c", "News", '["news"]'), ("d", "", "[]")]:
            pw.q("INSERT INTO seen(url_hash,url,title,first,last,score,tags,used_by) VALUES(?,?,?,?,?,?,?,?)",
                 (h, "https://s/" + h, "t-" + h, now, now, 8, tags, used))
        got = sorted(c["url_hash"] for c in pw.pick_for_show(a["shows"][0], a, now))
        check("digest picks tagged + untagged, skips other shows' and used",
              got == ["a", "d"], str(got))
        # New scores store stable show IDs, so punctuation in names cannot corrupt routing.
        comma_show = pw.clean_auto({"enabled": True, "shows": [
            {"id": "tech", "name": "Tech, Media", "kind": "digest", "watch": ["AI"]},
            {"id": "sports", "name": "Sports", "kind": "digest", "watch": ["Football"]}]})
        comma_news, sports = comma_show["shows"]
        pw.q("INSERT INTO seen(url_hash,url,title,source,first,last,score,tags,used_by) VALUES(?,?,?,?,?,?,?,?,?)",
             ("tech-hit", "https://s/tech", "AI story", "feed", now, now, 9,
              json.dumps(["tech"]), "[]"))
        pw.q("INSERT INTO seen(url_hash,url,title,source,first,last,score,tags,used_by) VALUES(?,?,?,?,?,?,?,?,?)",
             ("sports-hit", "https://s/sports", "Football story", "feed", now, now, 9,
              json.dumps(["sports"]), "[]"))
        check("stable show IDs route correctly despite comma in display name",
              "tech-hit" in [c["url_hash"] for c in pw.pick_for_show(comma_news, comma_show, now)]
              and "sports-hit" not in [c["url_hash"] for c in pw.pick_for_show(comma_news, comma_show, now)]
              and "sports-hit" in [c["url_hash"] for c in pw.pick_for_show(sports, comma_show, now)]
              and "tech-hit" not in [c["url_hash"] for c in pw.pick_for_show(sports, comma_show, now)])
        collision = pw.clean_auto({"enabled": True, "shows": [
            {"id": "local-sports", "name": "sports", "kind": "digest", "watch": ["Local"]},
            {"id": "sports", "name": "Sports", "kind": "digest", "watch": ["Football"]}]})
        local_sports, sports_id = collision["shows"]
        pw.q("INSERT INTO seen(url_hash,url,title,source,first,last,score,tags,used_by) VALUES(?,?,?,?,?,?,?,?,?)",
             ("json-collision", "https://s/json-collision", "Tagged for other show", "feed",
              now, now, 9, json.dumps(["sports"]), "[]"))
        check("JSON tags match IDs only when ID collides with another display name",
              "json-collision" not in [c["url_hash"] for c in
                                       pw.pick_for_show(local_sports, collision, now)]
              and "json-collision" in [c["url_hash"] for c in
                                       pw.pick_for_show(sports_id, collision, now)])
        pw.q("INSERT INTO seen(url_hash,url,title,source,first,last,score,tags,used_by) VALUES(?,?,?,?,?,?,?,?,?)",
             ("legacy-collision", "https://s/legacy-collision", "Legacy tagged story", "feed",
              now, now, 9, "sports", "[]"))
        check("legacy name tags route by display name only",
              "legacy-collision" in [c["url_hash"] for c in
                                      pw.pick_for_show(local_sports, collision, now)]
              and "legacy-collision" not in [c["url_hash"] for c in
                                              pw.pick_for_show(sports_id, collision, now)])
        scored_show = pw.clean_auto({"enabled": True, "shows": [
            {"id": "local-sports", "name": "sports", "kind": "digest", "watch": ["Local"]},
            {"id": "sports", "name": "Sports", "kind": "digest", "watch": ["Football"]}]})
        score_hash = "scored-id-contract"
        pw.q("INSERT INTO seen(url_hash,url,title,source,first,last,score,tags,used_by) VALUES(?,?,?,?,?,?,?,?,?)",
             (score_hash, "https://s/scored", "Local story", "feed", now, now, -1, "", "[]"))
        old_resolve, old_call = pw.resolve_llm, pw.llm_call
        pw.resolve_llm = lambda *_: {"id": "mock"}
        pw.llm_call = lambda *_args, **_kwargs: json.dumps({"scores": [
            {"i": 0, "score": 8, "shows": ["sports"]}]})
        try:
            pw.score_items([{"hash": score_hash, "title": "Local story", "source": "feed"}],
                           scored_show, cfg)
        finally:
            pw.resolve_llm, pw.llm_call = old_resolve, old_call
        check("mocked scoring prefers a colliding show ID over another show's name",
              pw.one("SELECT score,tags FROM seen WHERE url_hash=?", (score_hash,))["tags"]
              == json.dumps(["sports"]))
        pw.q("INSERT INTO seen(url_hash,url,title,source,first,last,score,tags,used_by) VALUES(?,?,?,?,?,?,?,?,?)",
             ("stale", "https://s/stale", "Old item", "feed", now - 49 * 3600, now, 10,
              json.dumps(["news"]), "[]"))
        check("digest excludes stale items beyond maxAgeHours",
              "stale" not in [c["url_hash"] for c in pw.pick_for_show(a["shows"][0], a, now)])
        pw._st_set("last_poll", now)                    # poll already "happened" — no network
        for s in a["shows"]:
            pw._st_set("sched:" + s["id"],
                       json.dumps([s["kind"], s["cadence"], s["hour"], s["minute"], s["weekday"]]))
            pw._st_set("next:" + s["id"], now - 1)      # both due right now
        n0 = pw.one("SELECT count(*) n FROM jobs")["n"]
        pw.auto_tick(now)
        check("tick fires every due automation once",
              pw.one("SELECT count(*) n FROM jobs")["n"] == n0 + 2,
              str(pw.one("SELECT count(*) n FROM jobs")["n"] - n0))
        check("each automation show keeps its own series",
              sorted(x["title"] for x in pw.rows("SELECT title FROM series")) == ["Bedtime", "News"],
              json.dumps(pw.rows("SELECT title FROM series"))[:160])
        pw.auto_tick(now + 5)
        check("no double fire before next due",
              pw.one("SELECT count(*) n FROM jobs")["n"] == n0 + 2)
        # No-match drops advance their schedule and log a skip without queuing work.
        pw.q("UPDATE seen SET used_by='[\"news\"]'")
        pw._st_set("next:news", now + 5)
        jobs_before_skip = pw.one("SELECT count(*) n FROM jobs")["n"]
        pw.auto_tick(now + 6)
        news_next = pw._st_get("next:news", 0)
        with open(pw.LOG_PATH, encoding="utf-8") as log_file:
            auto_log = log_file.read()
        check("no-match digest logs skip, advances drop, and does not queue",
              pw.one("SELECT count(*) n FROM jobs")["n"] == jobs_before_skip
              and news_next > now + 6
              and "News" in auto_log and "skipped this drop" in auto_log,
              f"jobs={pw.one('SELECT count(*) n FROM jobs')['n'] - jobs_before_skip}, next={news_next}")
        a2 = pw.clean_auto({"enabled": True, "shows": [dict(a["shows"][0], enabled=False), a["shows"][1]]})
        with open(os.path.join(data, "config.json"), "w") as fh:
            json.dump({"auto": a2}, fh)
        for s in a2["shows"]:
            pw._st_set("next:" + s["id"], now + 900)
        pw._st_set("next:bed", now + 30)
        pw._st_set("next:news", now + 30)
        pw.auto_tick(now + 31)                          # only Bedtime due, News disabled anyway
        check("disabled automations never fire",
              pw.one("SELECT count(*) n FROM jobs")["n"] == n0 + 3
              and json.loads(pw.rows("SELECT brief FROM jobs ORDER BY rowid DESC LIMIT 1")[0]["brief"])["genre"] == "fiction",
              str(pw.one("SELECT count(*) n FROM jobs")["n"] - n0))
        sid = r1["series_id"]
        last = pw.one("SELECT MAX(ep_no) n FROM jobs WHERE series_id=?", (sid,))["n"]
        pw.q("DELETE FROM jobs WHERE series_id=?", (sid,))
        j3, _ = pw.plan_show(a["shows"][1], a, cfg, now)
        r3 = pw.one("SELECT series_id, ep_no FROM jobs WHERE id=?", (j3,))
        check("automation numbering survives removal of all old jobs",
              r3["series_id"] == sid and r3["ep_no"] == last + 1)
        # More than the library's recent-episode limit, deliberately completed out of order.
        for ep_no in range(301, 0, -1):
            pw.q("INSERT INTO episodes(id,title,created,script,series_id,ep_no) VALUES(?,?,?,'[]',?,?)",
                 (302 - ep_no, "Episode", now, sid, ep_no))
        stored = next(x for x in pw.series_rows() if x["id"] == sid)
        check("series history includes every episode in episode order",
              stored["count"] == 301 and [e["series"]["ep"] for e in stored["episodes"]] == list(range(1, 302)))
    finally:
        if old_data is None:
            os.environ.pop("PODWAVE_DATA", None)
        else:
            os.environ["PODWAVE_DATA"] = old_data
        shutil.rmtree(data, ignore_errors=True)


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
        scenario_brief_matrix(tmp)
        scenario_quality_briefs()
        scenario_series(tmp)
        scenario_chat(tmp)
        scenario_no_key(tmp)
        scenario_cancel_queued()
        scenario_restart(tmp)
        scenario_errors(tmp)
        scenario_connection_tests(tmp)
        scenario_static()
        scenario_auto()
        result = subprocess.run([PY, __file__, "--quality-digests"], env=test_env())
        check("scheduled listening fixtures pass", result.returncode == 0)
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
    joined = os.path.join(tmp, "audio", str(ep["id"]) + ".wav")
    with wave.open(joined, "rb") as w:
        payload = w.readframes(w.getnframes())
    peak, max_zero_run = pcm_stats(payload)
    check("synthetic audio has no unintended long silent gap",
          max_zero_run <= int(0.31 * 24000), f"{max_zero_run / 24000:.3f}s")
    check("lines loudness-matched near target", 20000 <= peak <= 32767, f"peak={peak}")
    check("joined waveform does not hit either full-scale rail", peak < 32767)
    check("test catches clipping at either PCM rail",
          all(pcm_stats(rail * 16)[0] >= 32767 for rail in (b"\xff\x7f", b"\x00\x80")))
    check("test catches an unexpected half-second of silent PCM",
          pcm_stats(payload + b"\0\0" * 12000)[1] > int(0.31 * 24000))

    # a provider that rejects the 'instructions' field must still finish, voicing without it
    control(rejectInstr=True)
    jid2 = submit(genre="fiction", minutes=2)[1]["id"]
    wait_for(lambda: job_state(jid2)["state"] in ("done", "error"), 40)
    check("instructions rejected -> job still finishes", job_state(jid2)["state"] == "done",
          job_state(jid2).get("error", ""))
    check("fallback voice call omits instructions",
          "instructions" not in (req("GET", MOCK + "/__last")[1].get("speech") or {}))
    control(rejectInstr=False)


def pcm_stats(payload):
    peak = zero_run = max_zero_run = 0
    for i in range(0, len(payload), 2):
        sample = int.from_bytes(payload[i:i + 2], "little", signed=True)
        peak = max(peak, abs(sample))
        zero_run = zero_run + 1 if sample == 0 else 0
        max_zero_run = max(max_zero_run, zero_run)
    return peak, max_zero_run


def scenario_brief_matrix(tmp):
    """End-to-end regression for 'an episode from a brief': the brief's five dimensions
    (multiple topics, genre, solo/duo, language, requested length) reaching the writer and the
    voice direction, and the finished background episode being listed in the Library as
    playable + downloadable and surviving a server restart.

    What the mock validates: every brief field arrives in the model prompts and the TTS
    instructions, the format/voice mapping is right, and the *length instruction* (words =
    minutes x 150) reaches the outline. Wall-clock duration against the +/-20%-or-1-minute
    target is NOT mockable — the mock voices every line at a fixed 0.35 s — and is validated
    only by real-provider listening; see README 'Tests'."""
    print("\n== brief matrix: nonfiction/duo (Deutsch) + fiction/solo (Dansk) ==")
    control(beats=2, delay=0.0, failLLM=False, failTTS=False, rejectInstr=False, requireAuth=False)

    def prompts():
        return [(c.get("messages") or [{}])[-1].get("content", "")
                for c in (req("GET", MOCK + "/__last")[1].get("chats") or [])]

    def wait_done(jid):
        ok = wait_for(lambda: job_state(jid)["state"] in ("done", "error"), 40)
        return bool(ok) and job_state(jid)["state"] == "done"

    # --- A: non-fiction, two hosts, three topics, an angle, German, 4 minutes
    topics_a = ["Windkraft in der Nordsee", "Der Berliner Flughafen", "Ausbau der Bahn"]
    control(beats=2, delay=0.0)
    code, j = submit(topics=topics_a, minutes=4, extra={"angle": "Warum diese Projekte jetzt neu gedacht werden",
                                                        "language": "Deutsch"})
    jid_a = j["id"]
    check("nonfiction/duo German job done", wait_done(jid_a), job_state(jid_a).get("error", ""))
    d = job_state(jid_a)
    check("brief stores topics, genre, format, language, minutes",
          d["brief"]["topics"] == topics_a and d["brief"]["genre"] == "nonfiction"
          and d["brief"]["format"] == "duo" and d["brief"]["language"] == "Deutsch"
          and d["brief"]["minutes"] == 4, json.dumps(d.get("brief"))[:200])
    pr = prompts()
    outline, beats_p = (pr[0], "\n".join(pr[1:])) if pr else ("", "")
    check("every topic reaches the outline and the beat prompts",
          all(t in outline and t in beats_p for t in topics_a), (outline[:120], beats_p[:120]))
    check("nonfiction genre reaches the writer",
          "non-fiction episode" in outline and "Genre: nonfiction" in beats_p, outline[:120])
    check("two-host format reaches the writer",
          "two-host conversation" in outline and "Two named hosts" in outline
          and "Two named hosts" in beats_p, outline[:140])
    check("angle reaches the writer",
          "Angle to follow: Warum diese Projekte jetzt neu gedacht werden" in outline
          and "Angle to follow" in beats_p, outline[:140])
    check("requested length reaches the outline as words", "about 600 spoken words" in outline,
          outline[outline.find("Target") - 60:outline.find("Target") + 60] if "Target" in outline else outline[:120])
    check("per-beat word budget reaches the beat prompt", "~80 spoken words" in beats_p, beats_p[:140])
    script = req("GET", f"{BASE}/api/episodes/{d['episode_id']}/script")[1]
    check("duo script uses both speaker slots", {s["speaker"] for s in script["segments"]} == {"A", "B"},
          json.dumps(script["segments"])[:150])
    check("both host names reach the prompts",
          len(script["hosts"]) == 2 and all(n and n in outline + beats_p for n in script["hosts"]),
          json.dumps(script["hosts"]))
    check("duo lines use both configured TTS voices",
          req("GET", MOCK + "/__last")[1].get("voices") == ["alloy", "nova"],
          str(req("GET", MOCK + "/__last")[1].get("voices")))
    sp = (req("GET", MOCK + "/__last")[1].get("speech") or {})
    check("style reaches the voice direction", "dry and technical" in sp.get("instructions", ""),
          json.dumps(sp)[:160])
    check("language reaches the voice direction", "deliver the lines in Deutsch" in sp.get("instructions", ""),
          json.dumps(sp)[:160])

    # --- B: fiction, solo narrator, Danish, story fields, 3 minutes
    control(beats=2, delay=0.0)
    code, j = submit(topics=["En sommer paa fyret"], genre="fiction", fmt="solo", minutes=3, extra={
        "tone": "Uhyggelig stemning", "characters": "Kaptajn Holm og hendes barnebarn",
        "setting": "en fyrturnsstation i Vesterhavet", "twist": True, "language": "Dansk"})
    jid_b = j["id"]
    check("fiction/solo Danish job done", wait_done(jid_b), job_state(jid_b).get("error", ""))
    d2 = job_state(jid_b)
    check("brief stores fiction/solo/Dansk/story fields",
          d2["brief"]["genre"] == "fiction" and d2["brief"]["format"] == "solo"
          and d2["brief"]["language"] == "Dansk" and d2["brief"]["twist"] is True,
          json.dumps(d2.get("brief"))[:200])
    pr2 = prompts()
    outline2, beats2 = (pr2[0], "\n".join(pr2[1:])) if pr2 else ("", "")
    check("fiction genre and story arc reach the writer",
          "fictional story with named characters and a plot" in outline2
          and "cold-open hook" in outline2 and "Genre: fiction" in beats2, outline2[:140])
    check("story brief reaches the writer",
          "Uhyggelig stemning" in outline2 and "Kaptajn Holm" in outline2
          and "Vesterhavet" in outline2 and "surprise twist" in outline2, outline2[:160])
    check("solo format reaches the writer",
          "solo narration" in outline2 and "One host" in outline2 + beats2, outline2[:140])
    check("language reaches the writing prompt (solo)",
          "Language: Dansk" in outline2 and "Language: Dansk" in beats2, outline2[:120])
    check("requested length reaches the outline as words (3 min)", "about 450 spoken words" in outline2,
          outline2[:160])
    script2 = req("GET", f"{BASE}/api/episodes/{d2['episode_id']}/script")[1]
    check("solo episode: one narrator, one host name",
          all(s["speaker"] == "A" for s in script2["segments"])
          and len(script2["hosts"]) == 1 and bool(script2["hosts"][0]),
          json.dumps({"segs": script2["segments"][:2], "hosts": script2["hosts"]})[:180])
    check("solo lines use the single A voice only",
          req("GET", MOCK + "/__last")[1].get("voices") == ["alloy"],
          str(req("GET", MOCK + "/__last")[1].get("voices")))

    # --- C: the finished background episode in the Library — playable, downloadable, restart-proof.
    # A server (background) job never needs the tab, so 'survives closing the tab' = lives on the
    # server; the restart below is the stronger, testable form of that.
    eps = req("GET", BASE + "/api/episodes")[1]["episodes"]
    ep_a = [e for e in eps if e["id"] == d["episode_id"]]
    check("background episode appears in the Library, series-less",
          bool(ep_a) and ep_a[0]["seconds"] > 0 and ep_a[0].get("series") is None,
          json.dumps(ep_a)[:160])
    code, wav_body, hdr = req("GET", f"{BASE}/api/episodes/{d['episode_id']}/audio", raw=True)
    ctype = {k.lower(): v for k, v in hdr.items()}.get("content-type", "")
    check("Library audio serves as a WAV file for playback and download",
          code == 200 and wav_body[:4] == b"RIFF" and "audio/wav" in ctype
          and "episode-" in {k.lower(): v for k, v in hdr.items()}.get("content-disposition", ""),
          f"code={code} type={ctype}")
    check("Library audio seeks (Range -> 206)",
          req("GET", f"{BASE}/api/episodes/{d['episode_id']}/audio",
              headers={"Range": "bytes=0-99"}, raw=True)[0] == 206)
    os.kill(SRV_PID[0], signal.SIGKILL)          # stronger than a closed tab: the server dies
    time.sleep(0.5)
    start_server(tmp)
    eps2 = req("GET", BASE + "/api/episodes")[1]["episodes"]
    check("episode survives a server restart, audio intact",
          any(e["id"] == d["episode_id"] for e in eps2)
          and req("GET", f"{BASE}/api/episodes/{d['episode_id']}/audio", raw=True)[0] == 200)

    # --- D: requested-length and topic-count clamps at the API door
    code, j3 = submit(minutes=1)
    lo = j3["id"]
    wait_done(lo)
    check("minutes clamped up to 2", code == 202 and job_state(lo)["brief"]["minutes"] == 2,
          json.dumps(job_state(lo).get("brief", {}))[:140])
    code, j5 = submit(minutes=999)
    hi = j5["id"]
    wait_done(hi)
    check("minutes clamped down to 60", code == 202 and job_state(hi)["brief"]["minutes"] == 60,
          json.dumps(job_state(hi).get("brief", {}))[:140])
    check("garbage minutes rejected with 400, server alive",
          req("POST", BASE + "/api/jobs", {"topics": ["x"], "minutes": "abc"})[0] == 400
          and req("GET", BASE + "/api/health")[1]["ok"] is True)
    nine = [f"topic {i}" for i in range(1, 10)]
    control(beats=2, delay=0.0)
    code, j4 = submit(topics=nine, minutes=2)
    jid4 = j4["id"]
    wait_done(jid4)
    pr4 = prompts()
    check("topics clamped to eight before the model sees them",
          job_state(jid4)["brief"]["topics"] == nine[:8] and pr4
          and all(t in pr4[0] for t in nine[:8]) and "topic 9" not in pr4[0],
          json.dumps(job_state(jid4)["brief"]["topics"])[:140])

    for jid in (jid_a, jid_b, lo, hi, jid4):
        d_ = job_state(jid)
        if d_.get("episode_id"):
            req("DELETE", f"{BASE}/api/episodes/{d_['episode_id']}")
        req("DELETE", f"{BASE}/api/jobs/{jid}")
    control(beats=3, delay=0.0, failLLM=False, failTTS=False, requireAuth=False, rejectInstr=False)


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


def scenario_series(tmp):
    """`episodes: N` on the create call fans the brief out into a series of N ordered jobs;
    automation shows get one open-ended series each (covered in scenario_auto)."""
    print("\n== series: several episodes in one job ==")
    control(beats=2, delay=0.0, failLLM=False, failTTS=False)
    db = sqlite3.connect(os.path.join(tmp, "podwave.db"))
    db.execute("INSERT INTO series(id,title,created,total,source,show_id) VALUES('foreign','Foreign',?,0,'manual','')",
               (time.time(),))
    db.execute("INSERT INTO episodes(id,title,created,script,series_id,ep_no) VALUES(999999,'FOREIGN CANARY',?,'[]','foreign',1)",
               (time.time(),))
    db.commit()
    db.close()
    code, j = submit(topics=["The lighthouse keeper"], minutes=2,
                     extra={"episodes": 3, "series": "Signal & Static"})
    ser = j.get("series") or {}
    check("series accepted with one job per episode",
          code == 202 and ser.get("total") == 3 and len(ser.get("jobs") or []) == 3
          and j.get("id") == ser["jobs"][0], json.dumps(j)[:160])
    jids = ser["jobs"]
    detail = job_state(jids[0])
    check("job detail carries series metadata", (detail.get("series") or {}).get("id") == ser["id"])
    for jid in jids:
        wait_for(lambda x=jid: job_state(x)["state"] in ("done", "error", "canceled"), 40)
    ds = [job_state(x) for x in jids]
    check("every episode of the series finished", all(d["state"] == "done" for d in ds),
          json.dumps([d.get("error", "") for d in ds])[:150])
    jobs = req("GET", BASE + "/api/jobs")[1]["jobs"]
    jrows = {x["id"]: x for x in jobs if x["id"] in jids}
    check("job rows carry the series pointer",
          all(x["series"] and x["series"]["id"] == ser["id"] and x["series"]["title"] == "Signal & Static"
              for x in jrows.values()) and [jrows[k]["series"]["ep"] for k in jids] == [1, 2, 3],
          json.dumps([x.get("series") for x in jrows.values()])[:180])
    captured = req("GET", MOCK + "/__last")[1].get("chats") or []
    prompts = [str((call.get("messages") or [{}])[-1].get("content", "")) for call in captured]
    check("series positions reach the model in order",
          all(f"episode {i} of 3" in "\n".join(prompts) for i in range(1, 4)))
    briefs = [job_state(jid)["brief"] for jid in jids]
    check("manual episodes persist distinct episode plans",
          len({b.get("episode_plan") for b in briefs}) == 3
          and all(b.get("episode_plan") for b in briefs))
    check("episode plan contracts reach the model",
          all(any(b["episode_plan"] in p for p in prompts) for b in briefs))
    check("later manual episode receives bounded earlier same-series context",
          any("Prior completed episodes in this series" in p and "Mock Episode" in p for p in prompts)
          and all("FOREIGN CANARY" not in p for p in prompts)
          and all(len(str(job_state(jid)["brief"].get("continuity_context", ""))) <= 1400 for jid in jids))
    check("continuity notes are frozen in job brief for restart/retry",
          all(job_state(jid)["brief"].get("continuity_frozen") is True for jid in jids)
          and any("untrusted continuity notes" in str(job_state(jid)["brief"].get("continuity_context", ""))
                  for jid in jids))
    # Fail only the later outline call, then retry with the provider restored. The
    # stored context must remain the original snapshot even after another attempt.
    control(beats=2, delay=0.0, failLLM=False, failTTS=False, failContinuity=True)
    code_retry, retry_series = submit(topics=["Frozen context retry"], minutes=2,
                                      extra={"episodes": 2, "series": "Retry Arc"})
    retry_ids = retry_series.get("series", {}).get("jobs", [])
    wait_for(lambda: len(retry_ids) == 2 and job_state(retry_ids[0])["state"] == "done"
             and job_state(retry_ids[1])["state"] == "error", 40)
    failed_brief = job_state(retry_ids[1])["brief"] if len(retry_ids) == 2 else {}
    frozen_before_retry = failed_brief.get("continuity_context")
    check("failed outline attempt freezes same-series context before provider failure",
          code_retry == 202 and len(retry_ids) == 2 and failed_brief.get("continuity_frozen") is True
          and "Prior completed episodes in this series" in str(frozen_before_retry))
    control(failContinuity=False)
    if len(retry_ids) == 2:
        req("POST", f"{BASE}/api/jobs/{retry_ids[1]}/retry")
        wait_for(lambda: job_state(retry_ids[1])["state"] in ("done", "error"), 40)
        retried_brief = job_state(retry_ids[1])["brief"]
        check("retry reuses frozen context byte-for-byte",
              job_state(retry_ids[1])["state"] == "done"
              and retried_brief.get("continuity_context") == frozen_before_retry)
        control(beats=2, delay=0.0, failLLM=False, failTTS=False)
        req("DELETE", BASE + "/api/series/" + retry_series["series"]["id"])
    check("first episode and standalone job do not receive series history",
          not briefs[0].get("continuity_context")
          and all("Prior completed episodes in this series" not in p for p in prompts
                  if "No series here" in p))
    code, sl, _ = req("GET", BASE + "/api/series")
    s = [x for x in sl.get("series", []) if x["id"] == ser["id"]]
    check("series lists its episodes in order",
          code == 200 and s and s[0]["count"] == 3 and s[0]["source"] == "manual"
          and [e["id"] for e in s[0]["episodes"]] == sorted(d["episode_id"] for d in ds),
          json.dumps(s)[:200])
    eps = req("GET", BASE + "/api/episodes")[1]["episodes"]
    mine = {e["id"]: e for e in eps if (e.get("series") or {}).get("id") == ser["id"]}
    check("episodes carry series position + total",
          len(mine) == 3 and {e["series"]["ep"] for e in mine.values()} == {1, 2, 3}
          and all(e["series"]["total"] == 3 and e["series"]["title"] == "Signal & Static"
                  for e in mine.values()), json.dumps(list(mine.values()))[:200])
    others = [e for e in eps if e["id"] not in mine and e["id"] != 999999]
    check("plain episodes stay series-less", bool(others) and all(e.get("series") is None for e in others),
          str(len(others)))
    code2, j1 = submit(topics=["No series here"], minutes=2)
    ok1 = wait_for(lambda: job_state(j1["id"])["state"] in ("done", "error"), 40)
    check("one-episode create makes no series",
          code2 == 202 and "series" not in j1 and bool(ok1) and job_state(j1["id"])["state"] == "done",
          str(j1)[:120])
    code, r, _ = req("DELETE", BASE + "/api/series/" + ser["id"])
    files_gone = all(not os.path.exists(os.path.join(tmp, "audio", str(d["episode_id"]) + ".wav")) for d in ds)
    check("deleting a series removes its episodes and jobs",
          code == 200 and r.get("ok") and files_gone
          and req("GET", BASE + f"/api/episodes/{ds[0]['episode_id']}/audio", raw=True)[0] == 404
          and req("GET", BASE + f"/api/jobs/{jids[0]}")[0] == 404
          and not any(x["id"] == ser["id"] for x in req("GET", BASE + "/api/series")[1]["series"]), str(r))
    check("unknown series 404", req("GET", BASE + "/api/series/nope")[0] == 404)

    # the count is clamped to 12; cancel straight away so the clamp test stays cheap
    control(delay=0.3)
    code, j2 = submit(topics=["Count clamped"], minutes=2, extra={"episodes": 99})
    ids = j2["series"]["jobs"]
    check("active series cannot be deleted", req("DELETE", BASE + "/api/series/" + j2["series"]["id"])[0] == 409)
    check("episode count clamped to 12", code == 202 and len(ids) == 12, str(len(ids)))
    for x in ids:
        req("POST", BASE + f"/api/jobs/{x}/cancel")
    ok = wait_for(lambda: all(job_state(x)["state"] in ("done", "error", "canceled", "canceling")
                              and job_state(x)["state"] != "canceling"
                              for x in ids), 40)
    check("clamped series jobs all reached a final state", bool(ok),
          json.dumps([job_state(x)["state"] for x in ids])[:180])
    req("DELETE", BASE + "/api/series/" + j2["series"]["id"])
    control(delay=0.0)


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
    control(failTTSLine="Spoken line for the test episode.")
    partial = submit(topics=["Synthetic line failure"])[1]["id"]
    wait_for(lambda: job_state(partial)["state"] in ("done", "error"), 40)
    failed = job_state(partial)
    check("speech-line failure cannot complete a partial episode",
          failed["state"] == "error" and not failed.get("episode_id")
          and not any(e.get("id") == failed.get("episode_id") for e in req("GET", BASE + "/api/episodes")[1]["episodes"]),
          json.dumps({"state": failed["state"], "episode_id": failed.get("episode_id")})[:150])
    control(failTTSLine="")
    req("POST", f"{BASE}/api/jobs/{partial}/retry")
    wait_for(lambda: job_state(partial)["state"] in ("done", "error"), 40)
    retried = job_state(partial)
    code, audio, headers = req("GET", BASE + f"/api/episodes/{retried.get('episode_id')}/audio", raw=True)
    frames = 0
    if code == 200:
        with wave.open(io.BytesIO(audio), "rb") as wav:
            frames = wav.getnframes()
    check("successful speech retry yields playable downloadable WAV",
          retried["state"] == "done" and code == 200 and frames > 0
          and headers.get("Content-Type", "").startswith("audio/wav"),
          f"state={retried['state']}, status={code}, header={headers.get('Content-Type')}")
    control(ttsTruncatedWav=True)
    badwav = submit(topics=["Synthetic truncated PCM WAV"])[1]["id"]
    wait_for(lambda: job_state(badwav)["state"] in ("done", "error"), 40)
    bad_state = job_state(badwav)
    check("truncated PCM response cannot publish a partial episode",
          bad_state["state"] == "error" and not bad_state.get("episode_id"),
          json.dumps({"state": bad_state["state"], "error": bad_state.get("error", "")})[:150])
    # A damaged cache file from an earlier attempt is discarded and fetched again on retry.
    cached = os.path.join(tmp, "segments", badwav, "0000.wav")
    os.makedirs(os.path.dirname(cached), exist_ok=True)
    with open(cached, "wb") as fh:
        fh.write(b"RIFF" + b"\x00" * 1200)
    control(ttsTruncatedWav=False)
    calls_before = req("GET", MOCK + "/__last")[1].get("ttsCalls", 0)
    req("POST", f"{BASE}/api/jobs/{badwav}/retry")
    wait_for(lambda: job_state(badwav)["state"] in ("done", "error"), 40)
    recovered = job_state(badwav)
    calls_after = req("GET", MOCK + "/__last")[1].get("ttsCalls", 0)
    code, audio, headers = req("GET", BASE + f"/api/episodes/{recovered.get('episode_id')}/audio", raw=True)
    check("retry replaces invalid cached WAV and returns playable audio",
          recovered["state"] == "done" and calls_after > calls_before and code == 200
          and audio[:4] == b"RIFF" and headers.get("Content-Type", "").startswith("audio/wav"),
          f"state={recovered['state']}, calls={calls_before}->{calls_after}, status={code}")
    control(ttsZeroFrames=True)
    emptywav = submit(topics=["Synthetic zero-frame WAV"])[1]["id"]
    wait_for(lambda: job_state(emptywav)["state"] in ("done", "error"), 40)
    empty_state = job_state(emptywav)
    check("zero-frame speech WAV cannot publish an episode",
          empty_state["state"] == "error" and not empty_state.get("episode_id"),
          json.dumps({"state": empty_state["state"], "error": empty_state.get("error", "")})[:150])
    control(ttsZeroFrames=False)
    req("POST", f"{BASE}/api/jobs/{emptywav}/retry")
    wait_for(lambda: job_state(emptywav)["state"] in ("done", "error"), 40)
    recovered_empty = job_state(emptywav)
    code, audio, headers = req("GET", BASE + f"/api/episodes/{recovered_empty.get('episode_id')}/audio", raw=True)
    check("retry recovers zero-frame speech response with playable audio",
          recovered_empty["state"] == "done" and code == 200 and audio[:4] == b"RIFF"
          and headers.get("Content-Type", "").startswith("audio/wav"),
          f"state={recovered_empty['state']}, status={code}")
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
    sys.exit(scenario_quality_digests() if sys.argv[1:] == ["--quality-digests"] else main())
