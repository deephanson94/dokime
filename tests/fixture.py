#!/usr/bin/env python3
"""Build a synthetic dokime repo and replay `check` at every session start.

    fixture.py a DEST                 incident (a): condition met at session 2, work to 6
    fixture.py b DEST                 incident (b): four sessions, no unit named
    fixture.py handoffs SRC DEST      replay a real repo's handoffs/ dir (and units/ if
                                      SRC/../units exists) as synthetic sessions

A session is {"date": "YYYY-MM-DD", "unit": name-or-None, "files": {path: text},
"open": [{"name", "condition", "ceiling"}]}. Replay per session: tick the clock and
run the check (session start), open units, commit work with a `Unit:` trailer when
the session names one, commit a handoff. `handoffs SRC DEST` reads a real repo's
handoff files and takes a `unit:` line from each as that session's unit.
"""
import json, os, re, subprocess, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dokime as dk  # noqa: E402

DOKIME = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dokime.py")
INTENT = "# intent\n\n## Goals\n- g\n\n## Non-goals\n- n\n\n## Acceptance criteria\n- a\n\n## Invariants\n- i\n"


def sh(args, cwd, when=None, stdin=None):
    env = dict(os.environ, GIT_AUTHOR_DATE=when or "", GIT_COMMITTER_DATE=when or "")
    if when:
        env["DOKIME_NOW"] = when
    p = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, input=stdin)
    if p.returncode not in (0, 1, 2):
        raise RuntimeError("%s: %s" % (args, p.stderr))
    return p


def dokime(dest, when, *args):
    """Run a dokime command. Only check and session-start may exit non-zero (a flag); anything else raising is a broken replay."""
    payload = '{"source": "startup"}' if args[0] == "session-start" else None
    p = sh([sys.executable, DOKIME, *args], dest, when, stdin=payload)
    if p.returncode and args[0] not in ("check", "session-start"):
        raise RuntimeError("%s: %s" % (" ".join(args[:2]), p.stderr.strip() or p.stdout.strip()))
    return p


def commit(dest, when, msg, unit=None):
    sh(["git", "add", "-A"], dest)
    sh(["git", "commit", "-q", "--allow-empty", "-m", msg + ("\n\nUnit: %s" % unit if unit else "")], dest, when)


def init_repo(dest, date):
    os.makedirs(dest, exist_ok=True)
    sh(["git", "init", "-q", "."], dest)
    sh(["git", "config", "user.email", "fixture@dokime"], dest)
    sh(["git", "config", "user.name", "fixture"], dest)
    with open(os.path.join(dest, "intent.md"), "w") as f:
        f.write(INTENT)
    commit(dest, date + "T08:00:00+00:00", "intent")


def replay(dest, sessions):
    """Return one check report (dict) per session, taken at that session's start."""
    init_repo(dest, sessions[0]["date"])
    reports = []
    for i, s in enumerate(sessions):
        d = s["date"]
        p = dokime(dest, d + "T09:00:00+00:00", "session-start", "--json")
        reports.append(json.loads(p.stdout))
        for u in s.get("open", []):
            dokime(dest, d + "T09:30:00+00:00", "open", u["name"], "--condition", u["condition"],
                   "--ceiling", str(u.get("ceiling", 3)))
            commit(dest, d + "T09:30:00+00:00", "open " + u["name"])
        for path, text in s.get("files", {"work-%d.txt" % (i + 1): "work %d\n" % (i + 1)}).items():
            os.makedirs(os.path.dirname(os.path.join(dest, path)) or dest, exist_ok=True)
            with open(os.path.join(dest, path), "w") as f:
                f.write(text)
        commit(dest, d + "T11:00:00+00:00", "work session %d" % (i + 1), s.get("unit"))
        os.makedirs(os.path.join(dest, "handoffs"), exist_ok=True)
        with open(os.path.join(dest, "handoffs", "%s-session-%d.md" % (d, i + 1)), "w") as f:
            f.write("session %d\n" % (i + 1))
        commit(dest, d + "T12:00:00+00:00", "handoff session %d" % (i + 1))
    return reports


def scenario_a():
    """Unit opened session 1 with ceiling 3; done.txt (its run: condition) lands in session 2."""
    s = [{"date": "2026-09-0%d" % i, "unit": "feat"} for i in range(1, 7)]
    s[0]["open"] = [{"name": "feat", "condition": "run: test -f done.txt", "ceiling": 3}]
    s[1]["files"] = {"done.txt": "done\n"}
    return s


def scenario_b():
    return [{"date": "2026-09-0%d" % i, "unit": None} for i in range(1, 5)]


def from_handoffs(src):
    """Turn a real handoffs/ dir into sessions; opens units listed in ../units at session 1."""
    sessions = []
    for f in sorted(os.listdir(src)):
        m = re.match(r"^(\d{4}-\d{2}-\d{2})-.+\.md$", f)
        if not m:
            continue
        u = re.search(r"^unit:\s*(\S+)", dk.read(os.path.join(src, f)), re.M | re.I)
        sessions.append({"date": m.group(1), "unit": u.group(1) if u else None})
    units_dir = os.path.join(os.path.dirname(os.path.abspath(src)), "units")
    if sessions and os.path.isdir(units_dir):
        sessions[0]["open"] = [{"name": d["name"], "condition": d["done_condition"],
                                "ceiling": d["ceiling_sessions"]}
                               for d in (json.loads(dk.read(os.path.join(units_dir, f)))
                                         for f in sorted(os.listdir(units_dir)) if f.endswith(".json"))]
    return sessions


def main(argv):
    if len(argv) < 2 or argv[0] not in ("a", "b", "handoffs"):
        print(__doc__)
        return 2
    sessions = {"a": scenario_a, "b": scenario_b}[argv[0]]() if argv[0] != "handoffs" else from_handoffs(argv[1])
    dest = argv[-1]
    for i, r in enumerate(replay(dest, sessions)):
        print("session %d: %s" % (i + 1, " ".join(r["flags"]) or "no flags"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
