#!/usr/bin/env python3
"""dokime (doh-kee-MAY): an agreement, a ledger of work units, a boundary check.

Stdlib + git only. See intent.md for goals, non-goals, invariants.
"""
import argparse, datetime, difflib, hashlib, json, os, re, shutil, subprocess, sys

UNITS, INTENT = "units", "intent.md"
CLOCK = os.path.join(".dokime", "sessions.log")
STATUSES = ("open", "met", "closed")
INTEGRITY = ("pin-changed", "evidence-invalid", "unit-missing")  # the only flags --strict blocks on
STOP_SEEN = os.path.join(".dokime", "stop")  # session count + HEAD + flags the Stop hook last spoke; it speaks on change
KINDS = ("commit", "artifact", "sha256")
SHA_RE = re.compile(r"[0-9a-f]{7,40}")
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


class DokimeError(Exception):
    pass


# ---------------------------------------------------------------- helpers
def now():
    return os.environ.get("DOKIME_NOW") or datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def parse_ts(iso):
    t = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=datetime.timezone.utc)


def utc_date(iso):
    return parse_ts(iso).astimezone(datetime.timezone.utc).date().isoformat()


def git(*args, check=False):
    """Run git; return stdout stripped, or None on failure (unless check)."""
    p = subprocess.run(["git", *args], capture_output=True, text=True)
    if p.returncode and check:
        raise DokimeError("git %s: %s" % (" ".join(args), p.stderr.strip()))
    return None if p.returncode else p.stdout.strip()


def read(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def write(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def sha256_text(s):
    return hashlib.sha256(s.encode()).hexdigest()


def sha256_file(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def run_condition(cond, timeout=120):
    """Return 'pass' | 'fail' | 'unverified'. Only run: conditions execute, never nested."""
    if not cond.startswith("run:") or os.environ.get("DOKIME_NESTED"):
        return "unverified"
    try:
        p = subprocess.run(cond[4:].strip(), shell=True, timeout=timeout, capture_output=True, text=True, env=dict(os.environ, DOKIME_NESTED="1"))
        return "pass" if p.returncode == 0 else "fail"
    except subprocess.TimeoutExpired:
        return "fail"


# ----------------------------------------------------------------- ledger
def unit_path(name):
    return os.path.join(UNITS, name + ".json")


def validate_unit(d):
    e = []
    req = {"name": str, "opened_at": str, "status": str, "ceiling_sessions": int,
           "done_condition": str, "done_condition_sha256": str, "evidence": list}
    for k, t in req.items():
        if k not in d:
            e.append("missing field: " + k)
        elif not isinstance(d[k], t) or isinstance(d[k], bool):
            e.append("wrong type: %s (want %s)" % (k, t.__name__))
    if e:
        return e
    try:
        parse_ts(d["opened_at"])
    except ValueError:
        e.append("opened_at is not an ISO-8601 timestamp")
    bad_ev = [i for i, ev in enumerate(d["evidence"]) if not isinstance(ev, dict) or ev.get("kind") not in KINDS or not isinstance(ev.get("ref"), str)]
    rules = ((not NAME_RE.fullmatch(d["name"]), "bad name %r (letters, digits, . _ -; no spaces or slashes)" % d["name"]),
             (d["status"] not in STATUSES, "bad status: " + d["status"]), (d["ceiling_sessions"] < 1, "ceiling_sessions must be >= 1"),
             (not d["done_condition"].strip(), "done_condition is empty"),
             (not re.fullmatch(r"[0-9a-f]{64}", d["done_condition_sha256"]), "done_condition_sha256 is not a sha256 hex digest"),
             (bad_ev, "evidence[%s] must be {kind: commit|artifact|sha256, ref: str}" % ",".join(map(str, bad_ev))),
             (d["status"] != "open" and not d["evidence"], "status %s without evidence" % d["status"]))
    return e + [msg for bad, msg in rules if bad]


def load_unit(name):
    p = unit_path(name)
    if not os.path.exists(p):
        raise DokimeError("no such unit: " + name)
    try:
        d = json.loads(read(p))
    except ValueError as ex:
        raise DokimeError("%s: invalid JSON (%s)" % (p, ex))
    errs = validate_unit(d)
    if not errs and d["name"] != name:
        errs.append("file name does not match unit name %r" % d["name"])
    if errs:
        raise DokimeError("%s: %s" % (p, "; ".join(errs)))
    return d


def save_unit(d):
    os.makedirs(UNITS, exist_ok=True)
    with open(unit_path(d["name"]), "w") as f:
        json.dump(d, f, indent=2, sort_keys=True)
        f.write("\n")


def all_units():
    if not os.path.isdir(UNITS):
        return []
    names = sorted(f[:-5] for f in os.listdir(UNITS) if f.endswith(".json"))
    return [load_unit(n) for n in names]


def pin_status(d):
    """Compare the live condition to (1) its sha256 and (2) the committed copy."""
    if sha256_text(d["done_condition"]) != d["done_condition_sha256"]:
        return "CHANGED (sha256 mismatch)"
    added = git("log", "--diff-filter=A", "--format=%H", "--", unit_path(d["name"]))
    if not added:
        return "UNVERIFIABLE (unit file not committed)"
    first = added.splitlines()[-1]
    if git("rev-parse", "--is-shallow-repository") == "true" and git("rev-parse", "--verify", "--quiet", first + "^") is None:
        return "UNVERIFIABLE (shallow history)"  # the adding commit is the graft point; its history is cut
    blob = git("show", "%s:%s" % (first, unit_path(d["name"])))
    try:
        orig = json.loads(blob or "")
    except ValueError:
        return "UNVERIFIABLE (committed copy unreadable)"
    if orig.get("done_condition") != d["done_condition"] or orig.get("done_condition_sha256") != d["done_condition_sha256"]:
        return "CHANGED since " + first[:10]
    return "unchanged"


def settings():
    try:
        return json.loads(read(SETTINGS)) if os.path.exists(SETTINGS) else {}
    except ValueError as ex:
        raise DokimeError("%s: invalid JSON (%s)" % (SETTINGS, ex))


def handoffs_dir():
    """The handoff dir: {"dokime": {"handoffs": "docs/handoffs"}} in .claude/settings.json, else handoffs/. Repo-relative only."""
    h = os.path.normpath((settings().get("dokime") or {}).get("handoffs") or "handoffs")
    if os.path.isabs(h) or h.startswith(".."):
        raise DokimeError("dokime.handoffs in %s must be a path inside the repository, not %r" % (SETTINGS, h))
    return h


def ledger_dirs():
    return (handoffs_dir() + "/", UNITS + "/", ".dokime/")


def governance():
    """Pathspecs excluding the ledger and the governance files from what counts as work."""
    return [":(exclude)" + p for p in (handoffs_dir(), UNITS, ".dokime", INTENT, "CLAUDE.md", ".claude", ".gitignore")]


# --------------------------------------------------------------- evidence
def verify_commit(ref, opened_at):
    if not SHA_RE.fullmatch(ref):
        return "not a SHA (symbolic refs are refused; use git rev-parse HEAD)"
    full = git("rev-parse", "--verify", "--quiet", ref + "^{commit}")
    if not full:
        return "no such commit"
    if git("merge-base", "--is-ancestor", full, "HEAD") is None:
        return "not reachable from HEAD"
    if int(git("show", "-s", "--format=%ct", full) or 0) < parse_ts(opened_at).timestamp():
        return "committed before unit opened"
    files = (git("show", "--name-only", "--format=", full) or "").splitlines()
    if any(not f.startswith(ledger_dirs()) and int(git("cat-file", "-s", "%s:%s" % (full, f)) or 0) > 0 for f in files):
        return None
    return "touches no non-empty file outside the ledger"


def verify_evidence(ev, opened_at):
    """Return None when valid, else a reason string."""
    kind, ref = ev["kind"], ev["ref"]
    if kind == "commit":
        return verify_commit(ref, opened_at)
    path, digest = ref, None
    if kind == "sha256":
        path, _, digest = ref.rpartition(":")
        if not path or not re.fullmatch(r"[0-9a-f]{64}", digest):
            return "expected path:sha256"
    if os.path.normpath(path).startswith(tuple(d.rstrip("/") for d in ledger_dirs())):
        return "ledger files are not evidence"
    if not os.path.isfile(path):
        return "path does not exist"
    if git("ls-files", "--error-unmatch", path) is None:
        return "path is not tracked by git"
    if digest and sha256_file(path) != digest:
        return "content hash does not match"
    return None


def parse_evidence(items):
    out = []
    for it in items or []:
        kind, _, ref = it.partition(":")
        if kind not in KINDS or not ref:
            raise DokimeError("evidence must be kind:ref with kind in %s: %r" % ("|".join(KINDS), it))
        out.append({"kind": kind, "ref": ref})
    return out


# --------------------------------------------------------------- commands
def cmd_open(a):
    if not NAME_RE.fullmatch(a.name) or os.path.exists(unit_path(a.name)):
        raise DokimeError("bad unit name %r (letters, digits, . _ -; no spaces or slashes) or unit already exists" % a.name)
    cond = a.condition.strip()
    d = {"name": a.name, "opened_at": now(), "status": "open",
         "ceiling_sessions": a.ceiling, "done_condition": cond,
         "done_condition_sha256": sha256_text(cond), "evidence": [],
         "closed_at": None, "closed_by": None}
    errs = validate_unit(d)
    if errs:
        raise DokimeError("; ".join(errs))
    if run_condition(cond) == "pass":
        raise DokimeError("done_condition already passes; a unit needs a condition that is false now")
    save_unit(d)
    warn = "" if cond.startswith("run:") else "\nwarning: done_condition is prose: it will stay unverified"
    return {"unit": d, "warnings": warn.strip()}, "opened %s (pin %s); commit work with the trailer 'Unit: %s'%s" % (
        a.name, d["done_condition_sha256"][:12], a.name, warn)


def finish(a, status):
    d = load_unit(a.name)
    if d["status"] == "closed":
        raise DokimeError("unit already closed: " + a.name)
    evidence = d["evidence"] + parse_evidence(a.evidence)
    if not evidence:
        named = [c["sha"][:7] for c in commits() if c["unit"] == a.name and parse_ts(c["ts"]) > parse_ts(d["opened_at"])]
        raise DokimeError("refusing to set %s without evidence; commits carrying 'Unit: %s': %s" % (
            status, a.name, " ".join(named) or "none"))
    whys = [(ev, verify_evidence(ev, d["opened_at"])) for ev in evidence]
    problems = ["%s:%s %s" % (ev["kind"], ev["ref"], why) for ev, why in whys if why]
    result = run_condition(d["done_condition"])
    if result == "fail":
        problems.append("done_condition failed: " + d["done_condition"])
    if pin_status(d).startswith("CHANGED"):
        problems.append("done_condition pin " + pin_status(d))
    if problems and not a.force:
        raise DokimeError("refusing to set %s:\n  " % status + "\n  ".join(problems))
    d.update(status=status, evidence=evidence)
    if a.force:
        d["force_reason"] = a.force
    if status == "closed":
        d.update(closed_at=now(), closed_by=a.by or git("config", "user.name") or os.environ.get("USER", "unknown"))
    save_unit(d)
    return {"unit": d, "condition": result, "forced": bool(a.force), "problems": problems}, \
        "%s %s (condition %s%s)" % (status, a.name, result, ", FORCED" if a.force else "")


def cmd_list(a):
    units = all_units()
    lines = ["%-24s %-7s %3s  %-9s %2d ev  %s" % (u["name"], u["status"], u["ceiling_sessions"],
             "run" if u["done_condition"].startswith("run:") else "prose", len(u["evidence"]), u["opened_at"][:10]) for u in units]
    return {"units": units}, "\n".join(lines) or "no units"


# ------------------------------------------------------------------ check
HEADINGS = ("## Goals", "## Non-goals", "## Acceptance criteria", "## Invariants")


def intent_status():
    if not os.path.exists(INTENT):
        return "missing"
    text = read(INTENT)
    return ("stub" if "- TODO" in text else "present") if all(h in text for h in HEADINGS) else "incomplete"


def handoffs():
    """Sorted [(date, filename)] for <handoff dir>/YYYY-MM-DD-*.md; optional session records."""
    names = sorted(os.listdir(handoffs_dir())) if os.path.isdir(handoffs_dir()) else []
    return [(f[:10], f) for f in names if re.fullmatch(r"\d{4}-\d{2}-\d{2}-.+\.md", f)]


def clock():
    """Session-start timestamps from the hook-written log, or None if absent."""
    if not os.path.exists(CLOCK):
        return None
    return [ln.split()[0] for ln in read(CLOCK).splitlines() if ln.strip()]


def commits():
    """Non-merge commits touching work (not ledger or governance files), oldest first, with their Unit: trailer."""
    log = git("log", "--reverse", "--no-merges", "--format=%x1e%H%x1f%cI%x1f%(trailers:key=Unit,valueonly,separator=%x2c)",
              "--", ".", *governance()) or ""
    out = []
    for rec in filter(str.strip, log.split("\x1e")):  # str.strip also eats a leading separator, so filter, do not slice
        sha, ts, unit = (rec.strip("\n").split("\x1f") + ["", ""])[:3]
        out.append({"sha": sha, "ts": ts, "date": parse_ts(ts).date().isoformat(), "unit": unit.strip().split(",")[0] or None})
    return out


def hooks_configured():
    return any("dokime" in json.dumps(h) for h in (settings().get("hooks") or {}).values())


def unit_open_on(units, name, date):
    u = next((u for u in units if u["name"] == name), None)
    return bool(u) and utc_date(u["opened_at"]) <= date and (u["status"] != "closed" or (u.get("closed_at") or "9")[:10] >= date)


def check_unit(u, at, starts, hs, cs):
    r = {"name": u["name"], "status": u["status"], "flags": []}
    live = u["status"] != "closed"
    r["condition"] = run_condition(u["done_condition"]) if live and at == "all" else "unverified"
    bad = [ev for ev in u["evidence"] if live and verify_evidence(ev, u["opened_at"])]
    r["evidence"] = "invalid" if bad else "valid" if u["evidence"] else "none"
    r["pin"] = pin_status(u)
    r["forced"] = u.get("force_reason")
    if r["pin"].startswith("CHANGED"):
        r["flags"].append("pin-changed")
    if bad:
        r["flags"].append("evidence-invalid")
    if u["status"] == "open" and r["condition"] == "pass":
        r["flags"].append("met-but-open")
    if u["status"] != "closed":
        opened = u["opened_at"]
        r["handoffs"] = sum(1 for h in hs if h[0] >= utc_date(opened))
        r["commit_days"] = len(set(c["date"] for c in cs if c["date"] >= utc_date(opened)))
        r["ceiling"] = u["ceiling_sessions"]
        r["sessions"] = None if starts is None else max(  # highest source wins; the agent can only inflate
            1, sum(1 for t in starts if t > opened) + any(t <= opened for t in starts), r["handoffs"], r["commit_days"])
        if r["sessions"] is not None and r["sessions"] > u["ceiling_sessions"]:
            r["flags"].append("over-ceiling")
    return r


def run_check(at="all"):
    units, hs, cs, starts = all_units(), handoffs(), commits(), clock()
    rep = {"at": at, "intent": intent_status(), "units": [], "flags": [],
           "hooks": "configured" if hooks_configured() else "absent",
           "history": "shallow" if git("rev-parse", "--is-shallow-repository") == "true" else "full"}
    if rep["intent"] not in ("present", "stub"):
        rep["flags"].append("intent-" + rep["intent"])
    live = [u for u in units if u["status"] != "closed"]
    if rep["history"] == "shallow" and live:
        rep["flags"].append("history-shallow")
    if at == "all" and os.environ.get("DOKIME_NESTED") and any(u["done_condition"].startswith("run:") for u in live):
        rep["flags"].append("conditions-disabled")  # every run: condition reads unverified; that is a state, not a pass
    if starts is None and rep["hooks"] == "absent" and live:
        rep["flags"].append("clock-absent")
    ever = (git("log", "--diff-filter=A", "--format=", "--name-only", "--", UNITS) or "").split()
    for path in sorted(set(ever)):
        if not os.path.exists(path):
            rep["flags"].append("unit-missing(%s)" % path[len(UNITS) + 1:-5])
    for u in units:
        r = check_unit(u, at, starts, hs, cs)
        rep["units"].append(r)
        rep["flags"] += ["%s(%s)" % (f, u["name"]) for f in r["flags"]]
    # sessions and attribution need no condition run, so every mode reports them
    rep["handoffs"] = {"count": len(hs), "last": hs[-1][1] if hs else None}
    since = utc_date(starts[0]) if starts else ""
    n_days, n_hs = len(set(c["date"] for c in cs if c["date"] >= since)), sum(1 for h in hs if h[0] >= since)
    rep["sessions"] = {"clock": None if starts is None else len(starts), "handoffs": len(hs),
                       "commit_days": len(set(c["date"] for c in cs)), "divergent": bool(starts) and (
                           (os.path.isdir(handoffs_dir()) and n_hs < len(starts) - 1) or n_days > len(starts))}
    # attribution: since the previous session start (or the earliest live unit), every work commit names an open unit
    edge = (starts[-2] if len(starts) > 1 else starts[0]) if starts else min((u["opened_at"] for u in live), default=None)
    window = [c for c in cs if edge and parse_ts(c["ts"]) >= parse_ts(edge)]
    bad = [c for c in window if not (c["unit"] and unit_open_on(units, c["unit"], c["date"]))]
    rep["attribution"] = "%d of %d commits since %s" % (len(window) - len(bad), len(window), "last session" if starts else "unit opened") if edge else "unavailable (no clock, no open unit)"
    if bad:
        rep["flags"].append("work-without-unit(%d commit%s)" % (len(bad), "s"[len(bad) == 1:]))
    if at == "all":  # next: needs the conditions; the mid-session hooks never run them and never print it
        met = [u["name"] for u, r in zip(units, rep["units"]) if r["status"] != "closed" and r["condition"] == "pass"]
        rep["next"] = ("dokime init --write=intent" if rep["intent"] not in ("present", "stub") else
                       'dokime open <name> --condition "run: <a command that fails now>"' if not live else
                       "dokime close %s   (it lists the commits carrying the unit's trailer)" % met[0] if met else
                       "dokime init --write=hooks" if "clock-absent" in rep["flags"] else
                       "start a Claude Code session; its hook ticks the clock" if starts is None else
                       "commit work under unit %s with the trailer 'Unit: %s'" % (live[0]["name"], live[0]["name"]))
    rep["ok"] = not rep["flags"]
    return rep


def render(rep):
    L = ["intent: %s  hooks: %s  history: %s" % (rep["intent"], rep["hooks"], rep["history"])]
    for r in rep["units"]:
        line = "unit %s: %s  condition %s  evidence %s  pin %s" % (
            r["name"], r["status"], r["condition"], r["evidence"], r["pin"])
        if "ceiling" in r:
            line += "  sessions %s/%d" % ("?" if r["sessions"] is None else r["sessions"], r["ceiling"])
        L.append(line + ("  FORCED(%s)" % r["forced"] if r.get("forced") else "") + "".join("  " + f.upper() for f in r["flags"]))
    if "sessions" in rep:
        h, s = rep["handoffs"], rep["sessions"]
        L.append("handoffs: %d  last %s" % (h["count"], h["last"] or "-"))
        L.append("sessions: %s (clock%s)  handoffs %d  commit-days %d%s" % (
            "unknown" if s["clock"] is None else s["clock"], ", ticked" if rep.get("ticked") else "", s["handoffs"],
            s["commit_days"], "  DIVERGENT" if s["divergent"] else ""))
        L.append("attribution: " + rep["attribution"])
    L.append("flags: " + (" ".join(rep["flags"]) or "none"))
    if "next" in rep:
        L.append("next: " + rep["next"])
    return "\n".join(L)


def cmd_check(a):
    rep = run_check(a.at)
    text = brief(rep) if a.brief or (sys.stdout.isatty() and not (a.full or a.json)) else render(rep)
    if rep["flags"]:
        raise Flagged(rep, text)
    return rep, text


def status_line(rep):
    n, u, s = len(rep["flags"]), len(rep["units"]), rep["sessions"]["clock"]
    return "dokime: %s | intent %s | hooks %s | %d unit%s | sessions %s" % (
        "ok" if not n else "%d flag%s: %s" % (n, "s"[n == 1:], " ".join(rep["flags"])),
        rep["intent"], rep["hooks"], u, "s"[u == 1:], "unknown" if s is None else s)


SENTENCE = {  # the human view: per flag, the two records that disagree; no verb aimed at the reader, next: carries the command
    "met-but-open": "%s: the run: condition passes and the unit is still open.", "pin-changed": "%s: the done condition differs from the text pinned at open.",
    "evidence-invalid": "%s: an evidence ref no longer resolves.", "over-ceiling": "%s: sessions counted exceed the unit's ceiling.",
    "unit-missing": "%s: the unit file was committed and is now gone.", "work-without-unit": "%s since the last session start carry no Unit: trailer naming an open unit.",
    "intent-missing": "intent.md is absent.", "intent-incomplete": "intent.md lacks one of the four headings.", "history-shallow": "shallow clone: the pin rule cannot run here.",
    "clock-absent": "no session clock: a unit is open and the SessionStart hook has never ticked.", "conditions-disabled": "DOKIME_NESTED is set: no run: condition is executed."}


def sentences(rep):
    """One line per flag, the token first. Built from rep["flags"] alone, so the human view cannot omit a flag the block has."""
    out = []
    for t in rep["flags"]:
        name, _, arg = t.partition("(")
        tpl = SENTENCE.get(name, "%s: flagged.")
        out.append(t + "  " + (tpl % (arg.rstrip(")") or name) if "%s" in tpl else tpl))
    return out


def brief(rep):
    """Human view: status line, flag sentences, forced closes by name (never the reason), next:. render() is the agent block."""
    forced = [r["name"] for r in rep["units"] if r.get("forced")]
    return "\n".join([status_line(rep)] + sentences(rep) + (["forced: " + " ".join(forced)] if forced else [])
                     + (["next: " + rep["next"]] if "next" in rep else []))


def cmd_status(a):
    rep = run_check("all")
    return rep, status_line(rep)


def hook_payload():
    """JSON object a hook receives on stdin; {} for a tty or bad input."""
    try:
        d = {} if sys.stdin.isatty() else json.load(sys.stdin)
    except (ValueError, OSError):
        d = None
    return d if isinstance(d, dict) else {}


def cmd_session_start(a):
    """SessionStart hook: tick on startup|clear, or on a resume 8h+ after the last tick. Never blocks."""
    source, starts = hook_payload().get("source"), clock() or []
    stale = not starts or parse_ts(now()) - parse_ts(starts[-1]) > datetime.timedelta(hours=8)
    tick = source in ("startup", "clear") or (source == "resume" and stale)
    if tick:
        write(CLOCK, (read(CLOCK) if os.path.exists(CLOCK) else "") + "%s %s\n" % (now(), git("rev-parse", "--short", "HEAD") or "-"))
    try:
        rep = run_check("all")
    except DokimeError as ex:  # the block must reach the agent: SessionStart shows stdout only on exit 0
        return {"error": str(ex), "ticked": tick}, "dokime: %s\nnext: dokime check" % ex
    rep["ticked"] = tick
    return rep, render(rep) if tick or source == "compact" or rep["flags"] else status_line(rep)


def cmd_stop_hook(a):
    """Stop hook: flags only, spoken once per change of HEAD or flag set; --strict exits 2 on ledger-integrity flags alone."""
    if hook_payload().get("stop_hook_active"):
        return {"skipped": True}, ""
    rep = run_check("hook")
    if a.strict and any(f.partition("(")[0] in INTEGRITY for f in rep["flags"]):
        raise Flagged(rep, render(rep), code=2)
    cur = "%s %s %s" % (rep["sessions"]["clock"], git("rev-parse", "HEAD") or "-", " ".join(rep["flags"]))
    quiet = cur == (read(STOP_SEEN).strip() if os.path.exists(STOP_SEEN) else None)
    write(STOP_SEEN, cur + "\n")
    return (rep, json.dumps({"systemMessage": "dokime: " + "\n".join(sentences(rep))})) if rep["flags"] and not quiet else (rep, "")


def cmd_post_tool(a):
    """PostToolUse hook (Bash|Skill): silent unless HEAD moved or a skill loaded, then flags only. No run: conditions, no next:."""
    skill, head, seen = hook_payload().get("tool_name") == "Skill", git("rev-parse", "HEAD") or "", os.path.join(".dokime", "head")
    if not skill and head == (read(seen).strip() if os.path.exists(seen) else None):
        return {"skipped": True}, ""
    write(seen, head + "\n")
    try:
        rep = run_check("hook")
        text = render(rep) if rep["flags"] else status_line(rep) if skill else ""
    except DokimeError as ex:  # a broken ledger must reach the agent, not only the terminal
        rep, text = {"error": str(ex)}, "dokime: %s" % ex
    return rep, json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}}) if text else ""


class Flagged(DokimeError):
    def __init__(self, rep, text, code=1):
        super().__init__(text)
        self.rep, self.code = rep, code


# ------------------------------------------------------------ init / scan
SETTINGS, CLAUDE_MD, GITIGNORE = os.path.join(".claude", "settings.json"), "CLAUDE.md", ".gitignore"
MARK, ENDMARK = "<!-- dokime -->", "<!-- /dokime -->"
CLAUDE_LINES = MARK + """
Open a unit before work (`dokime open <name> --condition "run: ..."`) and end every commit message with the trailer `Unit: <name>` in the final trailer block.
Never edit intent.md or a unit's done_condition without asking.
""" + ENDMARK + "\n"  # only what no hook states at the moment of use; the session-start block already says what dokime is
HANDOFF_TEMPLATE = "## Done\n\n## Next\n"
INTENT_STUB = "# intent\n\n" + "".join(h + "\n- TODO\n\n" for h in HEADINGS)
QUESTIONS = (("Goals", "What must this repo achieve? (one per line, blank line ends)"), ("Non-goals", "What will it deliberately not do?"),
             ("Acceptance criteria", "What observable results mean done?"), ("Invariants", "What must never change while working?"))
PIECES = ("units", "handoffs", "hooks", "skill", "claude-md", "gitignore", "intent")
SKILL = os.path.join(".claude", "skills", "dokime", "SKILL.md")
SKILL_MD = """---
name: dokime
description: Check or set up dokime governance (intent.md, units/ ledger, hooks). Use for /dokime, "dokime status", or "is this repo governed".
---
Run `{c} check` and show the block verbatim, `next:` line included; do not summarise it. `next:` names the one command that applies.
When pieces are missing, run `{c} init` and show its report; write only the pieces the user confirms, with `{c} init --write=<pieces>`.
`--write=intent` runs an interview: ask the user its four questions, feed the answers, and write only after they say yes. Never invent goals, non-goals, criteria or invariants.
Never edit intent.md or a unit's done_condition without asking.
"""


def dokime_cmd(hook=True):
    """How a hook (or the skill) invokes dokime; None when neither on PATH nor inside this repo (an absolute path only works here)."""
    if shutil.which("dokime"):
        return "dokime"
    here, top = os.path.abspath(__file__), os.getcwd()
    if here.startswith(top + os.sep):
        return ('python3 "$CLAUDE_PROJECT_DIR/%s"' if hook else "python3 %s") % os.path.relpath(here, top)
    return None


def hook_config(c):
    return {"SessionStart": [{"hooks": [{"type": "command", "command": c + " session-start"}]}],
            "Stop": [{"hooks": [{"type": "command", "command": c + " stop-hook"}]}],
            "PostToolUse": [{"matcher": "Bash|Skill", "hooks": [{"type": "command", "command": c + " post-tool"}]}]}


def interview():
    if not sys.stdin.isatty() and os.environ.get("DOKIME_INTERVIEW") is None:
        print("no terminal for the interview: writing intent.md as a TODO stub; fill it in", file=sys.stderr)
        return INTENT_STUB
    src = open(os.environ["DOKIME_INTERVIEW"]) if os.environ.get("DOKIME_INTERVIEW") else sys.stdin
    out = ["# intent", ""]
    for title, q in QUESTIONS:
        print("%s: %s" % (title, q), file=sys.stderr)
        out.append("## " + title)
        while True:
            line = src.readline()
            if not line.strip():
                break
            out.append("- " + line.strip())
        out.append("")
    draft = "\n".join(out)
    print("---- draft intent.md ----\n" + draft + "---- write it? [y/N] ", end="", file=sys.stderr)
    if src.readline().strip().lower() != "y":
        raise DokimeError("intent.md not written")
    return draft


def planned_writes(want=()):
    """{piece: (path, new_text_or_None, note)}; None means nothing to do."""
    plan, cur, st, hd = {}, settings(), intent_status(), handoffs_dir()
    plan["intent"] = (INTENT, None if st in ("present", "stub") else "<interview>", st)
    for piece, d, fn, body in (("units", UNITS, ".gitkeep", ""), ("handoffs", hd, "TEMPLATE.md", HANDOFF_TEMPLATE)):
        plan[piece] = (os.path.join(d, fn), None if os.path.isdir(d) else body,
                       "present" if os.path.isdir(d) else "missing" if piece == "units" else "optional (not part of all)")
    # a repo that already keeps handoffs elsewhere (docs/handoffs, notes/sessions): record that dir instead of making handoffs/
    found = next((d for d in sorted({os.path.dirname(f) for f in (git("ls-files") or "").splitlines()})
                  if os.path.basename(d) in ("handoffs", "sessions")), None) if hd == "handoffs" and not os.path.isdir(hd) else None
    if found:
        merged = dict(cur, dokime=dict(cur.get("dokime") or {}, handoffs=found))
        cur = merged if "handoffs" in want else cur  # so a hooks write in the same run carries the key instead of clobbering it
        plan["handoffs"] = (SETTINGS, json.dumps(merged, indent=2) + "\n", "found %s; --write=handoffs records it in %s" % (found, SETTINGS))
    c = dokime_cmd()
    if hooks_configured():
        plan["hooks"] = (SETTINGS, None, "present")
    elif cur.get("hooks") or c is None:
        why = "existing hooks; add this yourself" if cur.get("hooks") else \
            "dokime is neither on PATH nor inside this repo: pip install it, or vendor dokime.py (e.g. tools/dokime.py) and run init from that copy; then add"
        plan["hooks"] = (SETTINGS, None, "MANUAL: %s:\n%s" % (why, json.dumps(hook_config(c or "dokime"), indent=2)))
    else:
        cur["hooks"] = hook_config(c)
        plan["hooks"] = (SETTINGS, json.dumps(cur, indent=2) + "\n", "missing")
    plan["skill"] = (SKILL, None if os.path.exists(SKILL) else SKILL_MD.format(c=dokime_cmd(hook=False) or "dokime"), "present" if os.path.exists(SKILL) else "missing")
    md = read(CLAUDE_MD) if os.path.exists(CLAUDE_MD) else ""
    plan["claude-md"] = (CLAUDE_MD, None if MARK in md else md.rstrip("\n") + ("\n\n" if md else "") + CLAUDE_LINES, "present" if MARK in md else "missing")
    gi = read(GITIGNORE) if os.path.exists(GITIGNORE) else ""
    has = ".dokime" in [ln.strip().rstrip("/") for ln in gi.splitlines()]
    plan["gitignore"] = (GITIGNORE, None if has else gi + ("" if gi.endswith("\n") or not gi else "\n") + ".dokime/\n", "present" if has else "missing")
    return plan


def cmd_init(a):
    want = tuple(p for x in (a.write or "").split(",") if x for p in ([q for q in PIECES if q != "handoffs"] if x == "all" else [x]))
    if set(want) - set(PIECES):
        raise DokimeError("unknown piece in --write; choose from " + ",".join(PIECES))
    plan, report, lines = planned_writes(want), {}, []
    for piece in PIECES:
        path, new, note = plan[piece]
        if piece in want and new is not None:
            try:
                new = interview() if new == "<interview>" else new
            except (DokimeError, OSError) as ex:
                report[piece], line = "not written", "%s: not written (%s)" % (piece, ex)
                lines.append(line)
                continue
            write(path, new)
            report[piece], line = "written", "%s: written %s" % (piece, path)
        else:
            report[piece], line = note, "%s: %s" % (piece, note)
            if new is not None and new != "<interview>" and not want:
                old = read(path).splitlines() if os.path.exists(path) else []
                line += "\n" + ("\n".join(difflib.unified_diff(old, new.splitlines(), path, path, lineterm="")) or "+++ %s (empty)" % path)
        lines.append(line)
    if not want and any(v[1] for v in plan.values()):
        lines.append("nothing written; use --write=all or --write=" + ",".join(PIECES))
    return {"pieces": report, "written": bool(want), "report": "\n".join(lines)}, "\n".join(lines)


def cmd_uninstall(a):
    removed = []
    if os.path.exists(SETTINGS):
        cur = settings()
        hooks = {ev: [h for h in hs if "dokime" not in json.dumps(h)] for ev, hs in (cur.get("hooks") or {}).items()}
        removed += ["hook " + ev for ev, hs in hooks.items() if len(hs) != len(cur["hooks"][ev])]
        cur["hooks"] = {ev: hs for ev, hs in hooks.items() if hs}
        if not cur["hooks"]:
            del cur["hooks"]
        if removed:
            write(SETTINGS, json.dumps(cur, indent=2) + "\n")
    if os.path.exists(CLAUDE_MD) and MARK in read(CLAUDE_MD):
        md = re.sub(r"\n*" + re.escape(MARK) + r".*?" + re.escape(ENDMARK) + r"\n?", "\n", read(CLAUDE_MD), flags=re.S).strip("\n")
        write(CLAUDE_MD, md + "\n") if md else os.remove(CLAUDE_MD)
        removed.append("CLAUDE.md block")
    if os.path.exists(GITIGNORE) and ".dokime/" in read(GITIGNORE).splitlines():
        write(GITIGNORE, "".join(ln + "\n" for ln in read(GITIGNORE).splitlines() if ln != ".dokime/"))
        removed.append(".gitignore line")
    if os.path.exists(SKILL) and read(SKILL).startswith("---\nname: dokime\n"):  # only dokime's own skill, never a neighbour
        shutil.rmtree(os.path.dirname(SKILL))
        removed.append("skill " + os.path.dirname(SKILL))
    return {"removed": removed}, "removed: " + (", ".join(removed) or "nothing") + "\nkept: intent.md, units/, %s/, .dokime/ (records; delete by hand)" % handoffs_dir()


# -------------------------------------------------------------------- cli
def build_parser():
    p = argparse.ArgumentParser(prog="dokime", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help):
        s = sub.add_parser(name, help=help)
        s.add_argument("--json", action="store_true", help="machine-readable output")
        s.set_defaults(fn=fn)
        return s

    s = add("open", cmd_open, "open a unit and pin its done_condition")
    s.add_argument("name")
    s.add_argument("--condition", required=True, help="done condition; prefix run: to make it executable")
    s.add_argument("--ceiling", type=int, default=3, help="ceiling in sessions (default 3)")
    for name, st, help in (("close", "closed", "close a unit; refuses without valid evidence"),
                           ("met", "met", "mark a unit met but leave it unclosed")):
        s = add(name, lambda a, st=st: finish(a, st), help)
        s.add_argument("name")
        s.add_argument("--evidence", action="append", metavar="KIND:REF",
                       help="commit:<sha> | artifact:<path> | sha256:<path>:<hex>")
        s.add_argument("--by", help="who closes (default: git user.name)")
        s.add_argument("--force", metavar="REASON", help="record REASON and override verification failures")
    add("list", cmd_list, "list units")
    s = add("check", cmd_check, "print the status block; exit 1 on any flag")
    s.add_argument("--at", choices=("all", "hook"), default="all",
                   help="hook = what the mid-session hooks see: no run: conditions, no next:")
    s.add_argument("--brief", action="store_true", help="human view, one line per flag (the default at a terminal)")
    s.add_argument("--full", action="store_true", help="the agent block (the default when piped; hooks and --json always get it)")
    add("status", cmd_status, "one-line status")
    add("session-start", cmd_session_start, "SessionStart hook: print the check; tick the clock on startup|clear")
    s = add("stop-hook", cmd_stop_hook, "Stop hook: flags only; --strict blocks on integrity flags")
    s.add_argument("--strict", action="store_true", help="exit 2 (blocks) on an integrity flag: pin, evidence, missing unit")
    add("post-tool", cmd_post_tool, "PostToolUse hook (Bash|Skill): after HEAD moves or a skill loads, flags only")
    s = add("init", cmd_init, "report missing pieces; write only with --write")
    s.add_argument("--write", metavar="PIECES", help="all or comma list of " + ",".join(PIECES))
    add("uninstall", cmd_uninstall, "remove hooks, CLAUDE.md block and .gitignore line; keep records")
    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    try:
        os.chdir(git("rev-parse", "--show-toplevel", check=True))
        data, text = a.fn(a)
    except Flagged as ex:
        print(json.dumps(ex.rep, indent=2, sort_keys=True) if a.json else str(ex),
              file=sys.stderr if ex.code == 2 else sys.stdout)
        return ex.code
    except (DokimeError, OSError, ValueError) as ex:
        print(json.dumps({"error": str(ex)}) if a.json else "dokime: %s" % ex, file=sys.stdout if a.json else sys.stderr)
        return 1
    if a.json:
        print(json.dumps(data, indent=2, sort_keys=True))
    elif text:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
