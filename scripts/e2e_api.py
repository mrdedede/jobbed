"""End-to-end check of the deployed API (Phase 5 checkpoint).

Signs up users over HTTP, seeds postings, and checks that /analyze grades the
same postings the CLI would for identical inputs, that a user with other
keywords gets a different set, and that re-runs and paging behave. Stdlib
only; leaves the database as it found it.

    E2E_TARGET=compose|k8s   where the stack runs        (default compose)
    E2E_MODEL=...            model that grades            (default ollama-qwen2.5:0.5b)
    E2E_API_URL=...          API base URL                 (compose :8080, k8s port-forward :18080)
                             when set, no port-forward is started, e.g.
                             E2E_TARGET=k8s E2E_API_URL=$(minikube service \
                               joblister-api-nodeport --url) python scripts/e2e_api.py

The model must be in the API's ANALYSIS_MODELS. Postings and keywords carry a
per-run random token, so only the seeded rows can match: no real posting is
graded and no clean database is needed. See README, "End-to-end check".
"""

import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = os.environ.get("E2E_TARGET", "compose")
MODEL = os.environ.get("E2E_MODEL", "ollama-qwen2.5:0.5b")
EXPLICIT_API = os.environ.get("E2E_API_URL")
API = EXPLICIT_API or ("http://localhost:18080" if TARGET == "k8s"
                       else "http://localhost:8080")
PASSWORD = "correct horse"

results = []


# --- plumbing -------------------------------------------------------------

def check(name, ok, evidence=""):
    results.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" +
          (f"   [{evidence}]" if evidence and not ok else ""))

    return ok


def call(method, path, token=None, body=None):
    request = urllib.request.Request(
        API + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"content-type": "application/json",
                 **({"authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            raw, status = response.read(), response.status
    except urllib.error.HTTPError as err:
        raw, status = err.read(), err.code

    return status, (json.loads(raw) if raw else None)


def run(*cmd):
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)


def sql(statement):
    prefix = (["docker", "compose", "exec", "-T", "postgres"]
              if TARGET == "compose" else
              ["kubectl", "exec", "deploy/postgres", "--"])
    done = run(*prefix, "psql", "-U", "joblister", "-d", "joblister",
               "-tAc", statement)

    if done.returncode:
        raise RuntimeError(f"psql failed: {done.stderr.strip()}")

    return done.stdout.strip()


def cli(*args):
    prefix = (["docker", "compose", "exec", "-T", "api"]
              if TARGET == "compose" else
              ["kubectl", "exec", "deploy/joblister-api", "--"])

    return run(*prefix, "python", "-m", "ai.main_analysis", *args)


def signup(tag, name):
    status, body = call("POST", "/v1/auth/signup", body={
        "email": f"e2e-{tag}-{name}@example.com", "password": PASSWORD})

    return body["access_token"] if status == 201 else None


def configure(token, keyword, blacklist=None, cv=True):
    call("POST", "/v1/me/keywords", token, {"term": keyword})

    if blacklist:
        call("POST", "/v1/me/blacklist", token, {"term": blacklist})
    if cv:
        call("POST", "/v1/me/cvs", token,
             {"content_md": "Backend engineer. Python, Go, Postgres."})


def urls(items):
    return {item["url"] for item in items}


def analyze(token, **body):
    return call("POST", "/v1/me/analyze", token, {"model": MODEL, **body})


def analyze_settled(token, attempts=4, **body):
    """analyze, retried while the model flakes.

    A small local model sometimes errors on a posting (the gateway answers
    502 for it). A failed posting stays due, so asking again is exactly how a
    client recovers; this makes the script test the API, not the model's
    reliability. Results accumulate across attempts; due/remaining/failed
    are the last attempt's.
    """
    merged, status = {"analysed": 0, "results": []}, 0

    for _ in range(attempts):
        status, got = analyze(token, **body)

        if status == 502:
            continue
        if status != 200:
            return status, got

        merged.update(due=got["due"], remaining=got["remaining"],
                      failed=got["failed"], filtered_out=got["filtered_out"])
        merged["analysed"] += got["analysed"]
        merged["results"] += got["results"]

        if got["failed"] == 0:
            break

    return (200, merged) if "due" in merged else (status, got)


# --- the check ------------------------------------------------------------

def seed(tag, py, go):
    rows = [
        ("py1", "Python Backend", f"{py} " * 6 + "services"),
        ("py2", "Python Platform", f"{py} " * 6 + "tooling"),
        ("pysenior", "Senior Python", f"{py} " * 6 + "senior role"),
        ("go1", "Go Backend", f"{go} " * 6 + "services"),
        ("go2", "Go Platform", f"{go} " * 6 + "tooling"),
        ("none", "Baker", "bake bread and cook meals"),
    ]
    values = ",".join(
        f"('E2E','{title}','{desc}','http://e2e/{tag}/{key}','Paris')"
        for key, title, desc in rows)
    sql("INSERT INTO job_postings (company,title,description,url,place) "
        f"VALUES {values}")

    return {key: f"http://e2e/{tag}/{key}" for key, _, _ in rows}


def scenario(tag):
    py, go = f"zq{tag}py", f"zq{tag}go"
    tokens = {}
    u = seed(tag, py, go)
    want_py, want_go = {u["py1"], u["py2"]}, {u["go1"], u["go2"]}

    print("preflight")
    status, _ = call("GET", "/healthz")
    check("healthz answers 200", status == 200)
    status, _ = call("GET", "/v1/me")
    check("no token is 401", status == 401)

    for name in "abcde":
        tokens[name] = signup(tag, name)
    check("five users signed up", all(tokens.values()))

    status, _ = analyze(tokens["e"])
    check(f"model {MODEL} is allowed (409 no-CV, not 422)", status == 409,
          f"got {status}; set ANALYSIS_MODELS to include it")
    status, _ = call("POST", "/v1/me/analyze", tokens["e"], {"model": "opus"})
    check("model opus is refused (422)", status == 422, f"got {status}")

    for name in "abd":
        configure(tokens[name], py, blacklist="senior")
    configure(tokens["c"], go)

    print("due before")
    for name, want in (("a", 2), ("b", 2), ("c", 2)):
        _, body = call("GET", "/v1/me/analyses/due?window_days=1",
                       tokens[name])
        check(f"user {name} has {want} due", body and body["due"] == want,
              body)

    print("API path")
    status, body = analyze_settled(tokens["a"])
    ok = status == 200 and body is not None
    check("A analyze returns 200", ok, (status, body))
    check("A graded exactly the python postings, blacklist honoured",
          ok and urls(body["results"]) == want_py and body["failed"] == 0,
          body and urls(body["results"]))
    check("results carry no description",
          ok and all("description" not in r for r in body["results"]))
    check("results record the model",
          ok and {r["ai_model"] for r in body["results"]} == {MODEL})
    graded_a = urls(body["results"]) if ok else set()

    print("CLI path (same inputs, other user)")
    _, me = call("GET", "/v1/me", tokens["b"])
    for _ in range(3):  # same model flake as analyze_settled: ask again
        done = cli(me["id"], "--model", MODEL)
        _, left = call("GET", "/v1/me/analyses/due?window_days=1",
                       tokens["b"])

        if done.returncode == 0 and left == {"due": 0}:
            break

    check("CLI exits 0", done.returncode == 0, done.stderr[-300:])
    _, listed = call("GET", "/v1/me/analyses?limit=200", tokens["b"])
    check("CLI graded the same set as the API (the checkpoint)",
          urls(listed) == graded_a and graded_a == want_py,
          (sorted(urls(listed)), sorted(graded_a)))

    print("divergence")
    status, body = analyze_settled(tokens["c"])
    got = urls(body["results"]) if status == 200 else set()
    check("C (go keywords) gets the go postings", got == want_go, got)
    check("C's set is disjoint from A's", not (got & graded_a))

    print("idempotence")
    status, body = analyze(tokens["a"])
    check("A again grades nothing",
          status == 200 and body["analysed"] == 0 and body["due"] == 0,
          body)
    _, due = call("GET", "/v1/me/analyses/due?window_days=1", tokens["a"])
    check("due is 0 after a run", due == {"due": 0}, due)

    print("paging")
    status, first = analyze_settled(tokens["d"], limit=1)
    ok = status == 200
    check("D limit=1 grades one, one remaining",
          ok and (first["due"], first["remaining"]) == (1, 1), first)
    status, second = analyze_settled(tokens["d"], limit=1)
    ok = ok and status == 200
    check("D second call grades the last",
          ok and (second["due"], second["remaining"]) == (1, 0), second)
    check("pages together cover both postings",
          ok and urls(first["results"]) | urls(second["results"]) == want_py)

    print("isolation")
    _, a_list = call("GET", "/v1/me/analyses?limit=200", tokens["a"])
    check("A sees none of C's rows", not (urls(a_list) & want_go))
    _, cvs = call("GET", "/v1/me/cvs", tokens["a"])
    status, _ = call("PUT", f"/v1/me/cvs/{cvs[0]['id']}/activate",
                     tokens["c"])
    check("C cannot touch A's CV (404)", status == 404, status)

    return tokens


def main():
    tag = secrets.token_hex(3)
    forward = None
    tokens = {}

    if TARGET == "k8s" and not EXPLICIT_API:
        forward = subprocess.Popen(
            ["kubectl", "port-forward", "svc/joblister-api", "18080:8080"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(3)

    print(f"e2e target={TARGET} model={MODEL} api={API} tag={tag}")

    try:
        tokens = scenario(tag)
    except Exception as exc:  # report, still clean up
        check(f"scenario ran without raising ({type(exc).__name__})", False,
              exc)
    finally:
        for token in tokens.values():
            if token:
                call("DELETE", "/v1/me", token)
        try:
            sql(f"DELETE FROM job_postings WHERE url LIKE 'http://e2e/{tag}/%'")
        except Exception as exc:
            print(f"  cleanup of postings failed: {exc}")
        if forward:
            forward.terminate()

    failed = [name for name, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
