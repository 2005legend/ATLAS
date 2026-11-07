"""Hermes benchmark: 12 tasks, 2 per category, each graded by an automatic check (never the model's own claim).

Run from the laptop; the script is streamed to VM 1 over SSH so the checks never sit on VM 1's disk
(Hermes runs as the same user there and could otherwise read them):

    ssh <vm1> "python3 - <label>" < benchmarks/bench.py > benchmarks/results/<date>-<label>.csv

Each task runs in a fresh one-shot Hermes session (`hermes chat --oneshot -Q -q ...`).
Output: CSV on stdout (one row per task); progress on stderr.
"""
import csv
import json
import os
import random
import re
import shutil
import socket
import string
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

LABEL = sys.argv[1] if len(sys.argv) > 1 else "run"
RUN = f"{time.strftime('%Y%m%d-%H%M%S')}-{LABEL}"
WORK = Path("/data/workspace/bench") / RUN
LOG = Path("/data/hermes/logs/agent.log")
ENV = {**os.environ, "HERMES_HOME": "/data/hermes", "PATH": f"{Path.home()}/.local/bin:" + os.environ.get("PATH", "")}
TIMEOUT = 600
rand = lambda n: "".join(random.choices(string.ascii_lowercase, k=n))


def hermes(prompt: str) -> tuple[str, str, float]:
    t = time.time()
    try:
        done = subprocess.run(["hermes", "chat", "--oneshot", "-Q", "-q", prompt], capture_output=True, text=True,
                              env=ENV, timeout=TIMEOUT)
        out, err = done.stdout, done.stderr
    except subprocess.TimeoutExpired as e:
        dec = lambda b: b.decode(errors="replace") if isinstance(b, bytes) else (b or "")
        out, err = dec(e.stdout), dec(e.stderr)
    # -Q prints the session id on stderr; the reply on stdout may be preceded by tool previews (box-drawing lines).
    m = re.search(r"session_id:\s*(\S+)", out + "\n" + err)
    body = re.sub(r"session_id:\s*\S+", "", out)
    lines = body.rstrip().splitlines()
    last_chrome = max((i for i, l in enumerate(lines) if l.lstrip()[:1] in "│┃╭╰├┌└─"), default=-1)
    answer = "\n".join(lines[last_chrome + 1:]).strip()
    return answer, (m.group(1) if m else ""), round(time.time() - t, 1)


def usage(session: str) -> tuple[int, int, str]:
    """API calls, input tokens and model names for one session, from Hermes's agent log."""
    if not session:
        return 0, 0, ""
    calls, tok, models = 0, 0, set()
    for line in LOG.read_text(errors="replace").splitlines():
        if session in line and "API call #" in line:
            calls += 1
            m = re.search(r"model=(\S+).*?in=(\d+)", line)
            if m:
                models.add(m.group(1)); tok += int(m.group(2))
    return calls, tok, "|".join(sorted(models))


def arxiv_entry(arxiv_id: str) -> str:
    url = f"https://export.arxiv.org/api/query?id_list={arxiv_id}"
    try:
        return urllib.request.urlopen(url, timeout=30).read().decode()
    except Exception:
        return ""


def pytest_ok(d: Path, test_src: str) -> bool:
    (d / "test_hidden_check.py").write_text(test_src)
    ok = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_hidden_check.py"],
                        cwd=d, capture_output=True, timeout=120).returncode == 0
    (d / "test_hidden_check.py").unlink(missing_ok=True)
    return ok


# ---------- tasks: (id, category, setup(dir)->ctx, prompt(dir,ctx), check(answer, dir, ctx)->bool) ----------
def first_int(a: str):
    m = re.search(r"-?\d+", a.replace(",", ""))
    return int(m.group()) if m else None


TASKS = []


def task(tid, cat):
    def deco(fn):
        TASKS.append((tid, cat, fn))
        return fn
    return deco


@task("R1", "reasoning")
def _(d):
    return ("A bat and a ball cost $1.10 in total. The bat costs $1.00 more than the ball. "
            "How much does the ball cost, in cents? Reply with only the number."), lambda a: first_int(a) == 5


@task("R2", "reasoning")
def _(d):
    return ("Five people sit in a row. Ann is left of Bo. Cy is right of Bo. Di is left of Ann. Ed is right of Cy. "
            "Who sits in the middle seat? Reply with only the name."), lambda a: a.strip().strip(".").lower().endswith("bo") or a.strip().lower() == "bo"


@task("C1", "coding")
def _(d):
    p = d / "c1"
    return (f"Create {p}/roman.py with a function to_roman(n: int) -> str for 1 <= n <= 3999 "
            "(standard subtractive notation) and test it. Reply 'done' when finished."), \
        lambda a: pytest_ok(p, "from roman import to_roman\n"
                               "def test_r():\n"
                               "    cases={1:'I',4:'IV',9:'IX',14:'XIV',40:'XL',90:'XC',400:'CD',944:'CMXLIV',1994:'MCMXCIV',2024:'MMXXIV',3999:'MMMCMXCIX'}\n"
                               "    for n,r in cases.items(): assert to_roman(n)==r, n\n") if (p / "roman.py").exists() else False


@task("C2", "coding")
def _(d):
    p = d / "c2"
    return (f"Create {p}/intervals.py with merge(intervals: list[list[int]]) -> list[list[int]] that merges overlapping "
            "or touching closed intervals and returns them sorted. Test it. Reply 'done' when finished."), \
        lambda a: pytest_ok(p, "from intervals import merge\n"
                               "def test_m():\n"
                               "    assert merge([[1,3],[2,6],[8,10],[15,18]])==[[1,6],[8,10],[15,18]]\n"
                               "    assert merge([[1,4],[4,5]])==[[1,5]]\n"
                               "    assert merge([[5,7],[1,2]])==[[1,2],[5,7]]\n"
                               "    assert merge([])==[]\n"
                               "    assert merge([[1,10],[2,3],[4,5]])==[[1,10]]\n") if (p / "intervals.py").exists() else False


@task("D1", "debugging")
def _(d):
    p = d / "d1"; p.mkdir(parents=True, exist_ok=True)
    (p / "search.py").write_text(
        "def find(xs, target):\n    \"\"\"Index of target in sorted list xs, or -1.\"\"\"\n"
        "    lo, hi = 0, len(xs) - 1\n    while lo < hi:\n        mid = (lo + hi) // 2\n"
        "        if xs[mid] == target:\n            return mid\n        if xs[mid] < target:\n            lo = mid + 1\n"
        "        else:\n            hi = mid - 1\n    return -1\n")
    (p / "test_search.py").write_text("from search import find\ndef test_find():\n    assert find([1, 3, 5, 7], 7) == 3\n    assert find([2], 2) == 0\n")
    return (f"The tests in {p}/test_search.py fail. Fix the bug in {p}/search.py without changing the test file. "
            "Run the tests. Reply 'done' when finished."), \
        lambda a: pytest_ok(p, "from search import find\nimport random\n"
                               "def test_all():\n"
                               "    for n in range(0, 30):\n"
                               "        xs = sorted(random.sample(range(1000), n))\n"
                               "        for i, x in enumerate(xs): assert find(xs, x) == i\n"
                               "        assert find(xs, 5000) == -1 and find(xs, -1) == -1\n")


@task("D2", "debugging")
def _(d):
    p = d / "d2"; p.mkdir(parents=True, exist_ok=True)
    (p / "words.py").write_text(
        "def top_word(text):\n    \"\"\"Most common word, case-insensitive, ignoring punctuation; ties -> alphabetically first.\"\"\"\n"
        "    counts = {}\n    for w in text.split():\n        counts[w] = counts.get(w, 0) + 1\n"
        "    return max(counts, key=counts.get)\n")
    return (f"{p}/words.py has a bug: top_word does not follow its docstring. Fix it so it matches the docstring exactly, "
            "and test it. Reply 'done' when finished."), \
        lambda a: pytest_ok(p, "from words import top_word\n"
                               "def test_w():\n"
                               "    assert top_word('The cat. the dog! THE end') == 'the'\n"
                               "    assert top_word('b a b a') == 'a'\n"
                               "    assert top_word('Hello, hello world') == 'hello'\n")


@task("S1", "research")
def _(d):
    return ("What is the arXiv ID of the paper 'Attention Is All You Need'? Reply with only the arXiv ID."), \
        lambda a: "1706.03762" in a


@task("S2", "research")
def _(d):
    def check(a):
        m = re.search(r"\b(2[4-6]\d{2}\.\d{4,5})\b", a)
        if not m:
            return False
        e = arxiv_entry(m.group(1)).lower()
        return "<entry>" in e and "physics-informed" in e
    return ("Find one arXiv paper first published in 2024 or later about physics-informed neural networks. "
            "Reply with only its arXiv ID (format YYMM.NNNNN). Do not guess: it must be a real paper."), check


@task("T1", "tool_use")
def _(d):
    host = socket.gethostname()
    return "What is the hostname of the machine you are running on? Check it, then reply with only the hostname.", \
        lambda a: host in a


@task("T2", "tool_use")
def _(d):
    n = len(Path("/etc/os-release").read_text().splitlines())
    return "How many lines does the file /etc/os-release have? Check it, then reply with only the number.", \
        lambda a: first_int(a) == n


@task("M1", "memory")
def _(d):
    # Long-term memory: remember, then approve the staged write (memory.write_approval is on), then recall in a new session.
    word = "zebra-" + rand(6)
    hermes(f"Please remember this for future conversations: my benchmark codeword is {word}. Reply 'ok'.")
    hermes("/memory approve all")
    return "What is my benchmark codeword? Reply with only the codeword.", lambda a: word in a


@task("M2", "memory")
def _(d):
    # Past-conversation search: nothing is approved into memory, so recall must come from session_search.
    color = random.choice(["teal", "maroon", "ochre", "violet", "amber"]); city = random.choice(["Ooty", "Shimla", "Munnar", "Coorg"])
    hermes(f"For my records: my test car is {color} and I parked it in {city}. Reply 'noted'.")
    return "In an earlier conversation I told you where I parked my test car. Which town was it? Reply with only the town name.", \
        lambda a: city.lower() in a.lower()


def cleanup_memory():
    """Drop benchmark facts so they never pollute the user's real memory."""
    hermes("/memory reject all")
    for f in Path("/data/hermes/memories").glob("*.md"):
        text = f.read_text()
        kept = [l for l in text.splitlines(keepends=True)
                if not re.search(r"benchmark codeword|test car|test fruit", l, re.I)]
        if len(kept) != len(text.splitlines(keepends=True)):
            f.write_text("".join(kept))


def main():
    WORK.mkdir(parents=True, exist_ok=True)
    w = csv.writer(sys.stdout)
    w.writerow(["run", "task", "category", "pass", "seconds", "api_calls", "input_tokens", "hermes_model", "start_utc", "end_utc", "answer"])
    passed = 0
    for tid, cat, fn in TASKS:
        d = WORK / tid; d.mkdir(parents=True, exist_ok=True)
        prompt, check = fn(d)
        start = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        answer, sid, secs = hermes(prompt)
        end = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        try:
            ok = bool(check(answer))
        except Exception:
            ok = False
        passed += ok
        calls, tok, model = usage(sid)
        w.writerow([RUN, tid, cat, int(ok), secs, calls, tok, model, start, end, answer.replace("\n", " ")[:200]])
        sys.stdout.flush()
        print(f"{tid:3s} {cat:10s} {'PASS' if ok else 'fail'}  {secs:6.1f}s  calls={calls}  ans={answer[:60]!r}", file=sys.stderr)
    print(f"TOTAL {passed}/{len(TASKS)}", file=sys.stderr)
    cleanup_memory()
    shutil.rmtree(WORK, ignore_errors=True)


if __name__ == "__main__":
    main()
