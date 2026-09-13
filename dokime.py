#!/usr/bin/env python3
"""dokime (doh-kee-MAY): an agreement, a ledger of work units, a boundary check.

Stdlib + git only. See intent.md for goals, non-goals, invariants.
"""
import argparse, datetime, difflib, hashlib, json, os, re, select, shutil, subprocess, sys, tempfile

UNITS, HANDOFFS, INTENT = "units", "handoffs", "intent.md"
CLOCK = os.path.join(".dokime", "sessions.log")
STATUSES = ("open", "met", "closed")
KINDS = ("commit", "artifact", "sha256")
SHA_RE = re.compile(r"[0-9a-f]{7,40}")
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
LEDGER_DIRS = (HANDOFFS + "/", UNITS + "/", ".dokime/")


class DokimeError(Exception):
    pass


# ---------------------------------------------------------------- helpers
def now():
    if os.environ.get("DOKIME_NOW"):
        return os.environ["DOKIME_NOW"]
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def parse_ts(iso):
    t = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=datetime.timezone.utc)


def utc_date(iso):
    return parse_ts(iso).astimezone(datetime.timezone.utc).date().isoformat()


def git(*args, check=False):
    """Run git; return stdout stripped, or None on failure (unless check)."""
    p = subprocess.run(["git", *args], capture_output=True, text=True)
    if p.returncode != 0:
        if check:
            raise DokimeError("git %s: %s" % (" ".join(args), p.stderr.strip()))
        return None
    return p.stdout.strip()


def root():
    top = git("rev-parse", "--show-toplevel")
    if top is None:
        raise DokimeError("not inside a git repository")
    return top


def read(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def sha256_text(s):
    return hashlib.sha256(s.encode()).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def run_condition(cond, timeout=120):
    """Return 'pass' | 'fail' | 'unverified'. Only run: conditions execute."""
    if not cond.startswith("run:"):
        return "unverified"
    try:
        p = subprocess.run(cond[4:].strip(), shell=True, timeout=timeout,
                           capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        return "fail"
    return "pass" if p.returncode == 0 else "fail"


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
    if not NAME_RE.fullmatch(d["name"]):
        e.append("bad name %r (letters, digits, . _ -; no spaces or slashes)" % d["name"])
    if d["status"] not in STATUSES:
        e.append("bad status: " + d["status"])
    if d["ceiling_sessions"] < 1:
        e.append("ceiling_sessions must be >= 1")
    if not d["done_condition"].strip():
        e.append("done_condition is empty")
    try:
        parse_ts(d["opened_at"])
    except ValueError:
        e.append("opened_at is not an ISO-8601 timestamp")
    if not re.fullmatch(r"[0-9a-f]{64}", d["done_condition_sha256"]):
        e.append("done_condition_sha256 is not a sha256 hex digest")
    for i, ev in enumerate(d["evidence"]):
        if not isinstance(ev, dict) or ev.get("kind") not in KINDS or not isinstance(ev.get("ref"), str):
            e.append("evidence[%d] must be {kind: commit|artifact|sha256, ref: str}" % i)
    for k in ("closed_at", "closed_by", "force_reason"):
        if d.get(k) is not None and not isinstance(d[k], str):
            e.append("wrong type: %s (want str or null)" % k)
    if d["status"] != "open" and not d["evidence"]:
        e.append("status %s without evidence" % d["status"])
    return e


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
    blob = git("show", "%s:%s" % (first, unit_path(d["name"])))
    try:
        orig = json.loads(blob or "")
    except ValueError:
        return "UNVERIFIABLE (committed copy unreadable)"
    if orig.get("done_condition") != d["done_condition"] or orig.get("done_condition_sha256") != d["done_condition_sha256"]:
        return "CHANGED since " + first[:10]
    return "unchanged"


# --------------------------------------------------------------- evidence
def verify_commit(ref, opened_at):
    if not SHA_RE.fullmatch(ref):
        return "not a SHA (symbolic refs are refused; use git rev-parse HEAD)"
    full = git("rev-parse", "--verify", "--quiet", ref + "^{commit}")
    if not full:
        return "no such commit"
    if git("merge-base", "--is-ancestor", full, "HEAD") is None:
        return "not reachable from HEAD"
    if int(git("show", "-s", "--format=%ct", full) or 0) <= parse_ts(opened_at).timestamp():
        return "committed before unit opened"
    files = (git("show", "--name-only", "--format=", full) or "").splitlines()
    for f in files:
        if f.startswith(LEDGER_DIRS):
            continue
        size = git("cat-file", "-s", "%s:%s" % (full, f))
        if size and int(size) > 0:
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
    if os.path.normpath(path).startswith(tuple(d.rstrip("/") for d in LEDGER_DIRS)):
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
    if not NAME_RE.fullmatch(a.name):
        raise DokimeError("bad unit name %r (letters, digits, . _ -; no spaces or slashes)" % a.name)
    if os.path.exists(unit_path(a.name)):
        raise DokimeError("unit already exists: " + a.name)
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
    warn = [] if cond.startswith("run:") else ["done_condition is prose: it will stay unverified"]
    return {"unit": d, "warnings": warn}, "opened %s (pin %s)%s" % (
        a.name, d["done_condition_sha256"][:12], "".join("\nwarning: " + w for w in warn))


def finish(a, status):
    d = load_unit(a.name)
    if d["status"] == "closed":
        raise DokimeError("unit already closed: " + a.name)
    evidence = d["evidence"] + parse_evidence(a.evidence)
    if not evidence:
        raise DokimeError("refusing to set %s without evidence" % status)
    problems = []
    for ev in evidence:
        why = verify_evidence(ev, d["opened_at"])
        if why:
            problems.append("%s:%s %s" % (ev["kind"], ev["ref"], why))
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


def cmd_close(a):
    return finish(a, "closed")


def cmd_met(a):
    return finish(a, "met")


def cmd_list(a):
    units = all_units()
    lines = ["%-24s %-7s %3s  %-9s %2d ev  %s" % (
        u["name"], u["status"], u["ceiling_sessions"],
        "run" if u["done_condition"].startswith("run:") else "prose", len(u["evidence"]),
        u["opened_at"][:10]) for u in units]
    return {"units": units}, "\n".join(lines) if lines else "no units"


# ------------------------------------------------------------------ check
HEADINGS = ("## Goals", "## Non-goals", "## Acceptance criteria", "## Invariants")


def intent_status():
    if not os.path.exists(INTENT):
        return "missing"
    text = read(INTENT)
    return "present" if all(h in text for h in HEADINGS) else "incomplete"


def handoffs():
    """Sorted [(date, filename, unit-or-None)] for handoffs/YYYY-MM-DD-*.md."""
    out = []
    for f in sorted(os.listdir(HANDOFFS)) if os.path.isdir(HANDOFFS) else []:
        if not re.match(r"^\d{4}-\d{2}-\d{2}-.+\.md$", f):
            continue
        m = re.search(r"^unit:\s*(\S+)", read(os.path.join(HANDOFFS, f)), re.M | re.I)
        out.append((f[:10], f, m.group(1) if m else None))
    return out


def clock():
    """Session-start timestamps from the hook-written log, or None if absent."""
    if not os.path.exists(CLOCK):
        return None
    return [ln.split()[0] for ln in read(CLOCK).splitlines() if ln.strip()]


def work_dates():
    """UTC dates of commits touching files outside the ledger, oldest first."""
    log = git("log", "--reverse", "--format=%cI", "--", ".", ":(exclude)" + HANDOFFS,
              ":(exclude)" + UNITS, ":(exclude).dokime") or ""
    return [utc_date(x) for x in log.splitlines()]


def unit_open_on(units, name, date):
    u = next((u for u in units if u["name"] == name), None)
    return bool(u) and utc_date(u["opened_at"]) <= date and (u["status"] != "closed" or (u.get("closed_at") or "9")[:10] >= date)


def check_unit(u, at, starts, hs, days):
    r = {"name": u["name"], "status": u["status"], "flags": []}
    live = u["status"] != "closed"
    r["condition"] = run_condition(u["done_condition"]) if live and at != "stop" else "unverified"
    bad = [ev for ev in u["evidence"] if live and verify_evidence(ev, u["opened_at"])]
    r["evidence"] = "valid" if not bad else "invalid"
    r["pin"] = pin_status(u)
    r["forced"] = u.get("force_reason")
    if r["pin"].startswith("CHANGED"):
        r["flags"].append("pin-changed")
    if bad:
        r["flags"].append("evidence-invalid")
    if u["status"] == "open" and r["condition"] == "pass":
        r["flags"].append("met-but-open")
    if u["status"] != "closed" and at != "stop":
        opened = u["opened_at"]
        r["sessions"] = None if starts is None else max(1, sum(1 for t in starts if t > opened) + any(t <= opened for t in starts))
        r["handoffs"] = sum(1 for h in hs if h[0] >= utc_date(opened))
        r["commit_days"] = len(set(d for d in days if d >= utc_date(opened)))
        r["ceiling"] = u["ceiling_sessions"]
        if r["sessions"] is not None and r["sessions"] > u["ceiling_sessions"]:
            r["flags"].append("over-ceiling")
    return r


def run_check(at="all"):
    units, hs, days, starts = all_units(), handoffs(), work_dates(), clock()
    rep = {"at": at, "intent": intent_status(), "units": [], "flags": []}
    if rep["intent"] != "present" and at != "stop":
        rep["flags"].append("intent-" + rep["intent"])
    ever = (git("log", "--diff-filter=A", "--format=", "--name-only", "--", UNITS) or "").split()
    for path in sorted(set(ever)):
        if not os.path.exists(path):
            rep["flags"].append("unit-missing(%s)" % path[len(UNITS) + 1:-5])
    for u in units:
        r = check_unit(u, at, starts, hs, days)
        rep["units"].append(r)
        rep["flags"] += ["%s(%s)" % (f, u["name"]) for f in r["flags"]]
    if at != "stop":
        last = hs[-1] if hs else None
        prev = max((h[0] for h in hs if last and h[0] < last[0]), default="")
        rep["handoffs"] = {"count": len(hs), "last": last and last[1], "unit": last and last[2],
                           "unit_valid": bool(last and last[2] and unit_open_on(units, last[2], last[0]))}
        since = utc_date(starts[0]) if starts else ""
        n_days, n_hs = len(set(d for d in days if d >= since)), sum(1 for h in hs if h[0] >= since)
        rep["sessions"] = {"clock": None if starts is None else len(starts), "handoffs": len(hs),
                           "commit_days": len(set(days)),
                           "divergent": bool(starts) and (n_hs < len(starts) - 1 or n_days > len(starts))}
        if last and last[0] > utc_date(now()):
            rep["flags"].append("handoff-future(%s)" % last[1])
        if last and not rep["handoffs"]["unit_valid"] and any(d > prev for d in days):
            rep["flags"].append("work-without-unit(%s)" % last[1])
    rep["ok"] = not rep["flags"]
    return rep


def render(rep):
    L = ["intent: " + rep["intent"]]
    for r in rep["units"]:
        line = "unit %s: %s  condition %s  evidence %s  pin %s" % (
            r["name"], r["status"], r["condition"], r["evidence"], r["pin"])
        if "ceiling" in r:
            line += "  sessions %s/%d" % ("?" if r["sessions"] is None else r["sessions"], r["ceiling"])
        if r.get("forced"):
            line += "  FORCED(%s)" % r["forced"]
        L.append(line + "".join("  " + f.upper() for f in r["flags"]))
    if "sessions" in rep:
        h, s = rep["handoffs"], rep["sessions"]
        L.append("handoffs: %d  last %s  unit %s" % (h["count"], h["last"] or "-",
                 (h["unit"] or "-") + ("" if not h["unit"] or h["unit_valid"] else " (INVALID)")))
        L.append("sessions: %s (clock)  handoffs %d  commit-days %d%s" % (
            "unknown" if s["clock"] is None else s["clock"], s["handoffs"], s["commit_days"],
            "  DIVERGENT" if s["divergent"] else ""))
    L.append("flags: " + (" ".join(rep["flags"]) or "none"))
    return "\n".join(L)


def cmd_check(a):
    rep = run_check(a.at)
    text = render(rep)
    if rep["flags"] and a.warn_only:
        text += "\nwarning: %d flag(s), exit downgraded by --warn-only" % len(rep["flags"])
    elif rep["flags"]:
        raise Flagged(rep, text)
    return rep, text


def cmd_status(a):
    rep = run_check("all")
    n, u = len(rep["flags"]), len(rep["units"])
    s = rep["sessions"]["clock"]
    line = "dokime: %s | intent %s | %d unit%s | sessions %s" % (
        "ok" if not n else "%d flag%s: %s" % (n, "s"[n == 1:], " ".join(rep["flags"])),
        rep["intent"], u, "s"[u == 1:], "unknown" if s is None else s)
    return rep, line


def hook_payload():
    """JSON object a hook receives on stdin; {} for a tty, no data within 1s, or bad input."""
    try:
        if sys.stdin.isatty():
            return {}
        try:
            ready = select.select([sys.stdin], [], [], 1)[0]
        except (OSError, TypeError, ValueError):
            ready = True
        d = json.load(sys.stdin) if ready else {}
        return d if isinstance(d, dict) else {}
    except (ValueError, OSError):
        return {}


def cmd_session_start(a):
    """SessionStart hook: tick the clock on startup|clear only, print the full check, never block."""
    source = hook_payload().get("source", "startup")
    if source in ("startup", "clear"):
        os.makedirs(os.path.dirname(CLOCK), exist_ok=True)
        with open(CLOCK, "a") as f:
            f.write("%s %s\n" % (now(), git("rev-parse", "--short", "HEAD") or "-"))
    rep = run_check("all")
    rep["ticked"] = source in ("startup", "clear")
    return rep, render(rep)


def cmd_stop_hook(a):
    """Stop hook: ledger-integrity rules only; exit 2 with stderr only under --strict."""
    if hook_payload().get("stop_hook_active"):
        return {"skipped": True}, ""
    rep = run_check("stop")
    if rep["flags"] and a.strict:
        raise Flagged(rep, render(rep), code=2)
    if rep["flags"]:
        return rep, json.dumps({"systemMessage": "dokime: " + " ".join(rep["flags"])})
    return rep, ""


class Flagged(DokimeError):
    def __init__(self, rep, text, code=1):
        super().__init__(text)
        self.rep, self.code = rep, code


# ------------------------------------------------------------ init / scan
SETTINGS, CLAUDE_MD, GITIGNORE = os.path.join(".claude", "settings.json"), "CLAUDE.md", ".gitignore"
MARK, ENDMARK = "<!-- dokime -->", "<!-- /dokime -->"
CLAUDE_LINES = MARK + """
This repo is governed by dokime: intent.md is the agreement, units/ is the ledger, `dokime check` is the boundary check.
Open a unit before work (`dokime open <name> --condition "run: ..."`) and end each session with handoffs/YYYY-MM-DD-<topic>.md containing a `unit: <name>` line.
Never edit intent.md or a unit's done_condition without asking.
""" + ENDMARK + "\n"
HANDOFF_TEMPLATE = "unit: <name>\n\n## Done\n\n## Next\n"
QUESTIONS = (("Goals", "What must this repo achieve? (one per line, blank line ends)"),
             ("Non-goals", "What will it deliberately not do?"),
             ("Acceptance criteria", "What observable results mean done?"),
             ("Invariants", "What must never change while working?"))
PIECES = ("units", "handoffs", "hooks", "claude-md", "gitignore", "intent")


def dokime_cmd():
    if shutil.which("dokime"):
        return "dokime"
    here, top = os.path.abspath(__file__), os.getcwd()
    if here.startswith(top + os.sep):
        return 'python3 "$CLAUDE_PROJECT_DIR/%s"' % os.path.relpath(here, top)
    return 'python3 "%s"' % here


def hook_config():
    c = dokime_cmd()
    return {"SessionStart": [{"hooks": [{"type": "command", "command": c + " session-start"}]}],
            "Stop": [{"hooks": [{"type": "command", "command": c + " stop-hook"}]}]}


def interview():
    if not sys.stdin.isatty() and os.environ.get("DOKIME_INTERVIEW") is None:
        raise DokimeError("interview needs a terminal, or DOKIME_INTERVIEW=<answers file>: "
                          "four blank-line-terminated blocks, then y")
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


def planned_writes():
    """{piece: (path, new_text_or_None, note)}; None means nothing to do."""
    plan = {}
    st = intent_status()
    plan["intent"] = (INTENT, None if st == "present" else "<interview>", st)
    for piece, d, fn, body in (("units", UNITS, ".gitkeep", ""), ("handoffs", HANDOFFS, "TEMPLATE.md", HANDOFF_TEMPLATE)):
        plan[piece] = (os.path.join(d, fn), None if os.path.isdir(d) else body, "present" if os.path.isdir(d) else "missing")
    try:
        cur = json.loads(read(SETTINGS)) if os.path.exists(SETTINGS) else {}
    except ValueError as ex:
        raise DokimeError("%s: invalid JSON (%s)" % (SETTINGS, ex))
    if any("dokime" in json.dumps(h) for h in (cur.get("hooks") or {}).values()):
        plan["hooks"] = (SETTINGS, None, "present")
    elif cur.get("hooks"):
        plan["hooks"] = (SETTINGS, None, "MANUAL: existing hooks; add this yourself:\n" + json.dumps(hook_config(), indent=2))
    else:
        cur["hooks"] = hook_config()
        plan["hooks"] = (SETTINGS, json.dumps(cur, indent=2) + "\n", "missing")
    md = read(CLAUDE_MD) if os.path.exists(CLAUDE_MD) else ""
    plan["claude-md"] = (CLAUDE_MD, None if MARK in md else md.rstrip("\n") + ("\n\n" if md else "") + CLAUDE_LINES, "present" if MARK in md else "missing")
    gi = read(GITIGNORE) if os.path.exists(GITIGNORE) else ""
    has = ".dokime" in [ln.strip().rstrip("/") for ln in gi.splitlines()]
    plan["gitignore"] = (GITIGNORE, None if has else gi + ("" if gi.endswith("\n") or not gi else "\n") + ".dokime/\n", "present" if has else "missing")
    return plan


def cmd_init(a):
    want = PIECES if a.write == "all" else tuple(a.write.split(",")) if a.write else ()
    if set(want) - set(PIECES):
        raise DokimeError("unknown piece in --write; choose from " + ",".join(PIECES))
    plan, report, lines = planned_writes(), {}, []
    for piece in PIECES:
        path, new, note = plan[piece]
        if piece in want and new is not None:
            try:
                new = interview() if new == "<interview>" else new
            except (DokimeError, OSError) as ex:
                report[piece], line = "not written", "%s: not written (%s)" % (piece, ex)
                lines.append(line)
                continue
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w") as f:
                f.write(new)
            report[piece], line = "written", "%s: written %s" % (piece, path)
        else:
            report[piece], line = note, "%s: %s" % (piece, note)
            if new and new != "<interview>" and not want:
                old = read(path).splitlines() if os.path.exists(path) else []
                line += "\n" + "\n".join(difflib.unified_diff(old, new.splitlines(), path, path, lineterm=""))
        lines.append(line)
    if not want and any(v[1] for v in plan.values()):
        lines.append("nothing written; use --write=all or --write=" + ",".join(PIECES))
    return {"pieces": report, "written": bool(want), "report": "\n".join(lines)}, "\n".join(lines)


def cmd_uninstall(a):
    removed = []
    if os.path.exists(SETTINGS):
        cur = json.loads(read(SETTINGS))
        for ev in list(cur.get("hooks") or {}):
            kept = [h for h in cur["hooks"][ev] if "dokime" not in json.dumps(h)]
            if len(kept) != len(cur["hooks"][ev]):
                removed.append("hook " + ev)
            cur["hooks"][ev] = kept
        cur["hooks"] = {k: v for k, v in (cur.get("hooks") or {}).items() if v}
        if not cur["hooks"]:
            del cur["hooks"]
        if removed:
            with open(SETTINGS, "w") as f:
                f.write(json.dumps(cur, indent=2) + "\n")
    if os.path.exists(CLAUDE_MD) and MARK in read(CLAUDE_MD):
        md = re.sub(r"\n*" + re.escape(MARK) + r".*?" + re.escape(ENDMARK) + r"\n?", "\n", read(CLAUDE_MD), flags=re.S).strip("\n")
        if md:
            with open(CLAUDE_MD, "w") as f:
                f.write(md + "\n")
        else:
            os.remove(CLAUDE_MD)
        removed.append("CLAUDE.md block")
    if os.path.exists(GITIGNORE) and ".dokime/" in read(GITIGNORE).splitlines():
        with open(GITIGNORE, "w") as f:
            f.write("".join(ln + "\n" for ln in read(GITIGNORE).splitlines() if ln != ".dokime/"))
        removed.append(".gitignore line")
    return {"removed": removed}, "removed: " + (", ".join(removed) or "nothing") + "\nkept: intent.md, units/, handoffs/, .dokime/ (records; delete by hand)"


def cmd_scan(a):
    """Walk back from HEAD running a run: condition in a detached worktree per commit."""
    if not a.condition.startswith("run:"):
        raise DokimeError("scan needs an executable condition (run: ...)")
    shas = (git("rev-list", "--max-count=%d" % a.limit, "HEAD") or "").splitlines()
    results, tmp = [], tempfile.mkdtemp(prefix="dokime-scan-")
    try:
        for sha in shas:
            wt = os.path.join(tmp, sha[:10])
            git("worktree", "add", "--detach", "-q", wt, sha, check=True)
            cwd = os.getcwd()
            try:
                os.chdir(wt)
                results.append((sha, run_condition(a.condition, timeout=a.timeout)))
            finally:
                os.chdir(cwd)
                git("worktree", "remove", "--force", wt)
            if results[-1][1] != "pass":
                break
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        git("worktree", "prune")
    passing = [s for s, r in results if r == "pass"]
    if not passing:
        return {"earliest_pass": None, "checked": len(results)}, "HEAD: %s  earliest passing: none in %d commit(s)" % (results[0][1] if results else "no commits", len(results))
    first = passing[-1]
    when = git("show", "-s", "--format=%cI", first)
    return {"earliest_pass": first, "date": when, "commits_after": len(passing) - 1, "checked": len(results)}, \
        "earliest passing: %s %s  commits after it: %d  (checked %d of last %d)" % (first[:10], when, len(passing) - 1, len(results), a.limit)


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
    for name, fn, help in (("close", cmd_close, "close a unit; refuses without valid evidence"),
                           ("met", cmd_met, "mark a unit met but leave it unclosed")):
        s = add(name, fn, help)
        s.add_argument("name")
        s.add_argument("--evidence", action="append", metavar="KIND:REF",
                       help="commit:<sha> | artifact:<path> | sha256:<path>:<hex>")
        s.add_argument("--by", help="who closes (default: git user.name)")
        s.add_argument("--force", metavar="REASON", help="record REASON and override verification failures")
    add("list", cmd_list, "list units")
    s = add("check", cmd_check, "print the status block; exit 1 on any flag")
    s.add_argument("--at", choices=("all", "stop"), default="all",
                   help="stop = ledger integrity only (no run: conditions, no session rules)")
    s.add_argument("--warn-only", action="store_true", help="exit 0 even when flagged")
    add("status", cmd_status, "one-line status")
    add("session-start", cmd_session_start, "SessionStart hook: print the check; tick the clock on startup|clear")
    s = add("stop-hook", cmd_stop_hook, "Stop hook: integrity rules; blocks only with --strict")
    s.add_argument("--strict", action="store_true", help="exit 2 (blocks) when flagged")
    s = add("init", cmd_init, "report missing pieces; write only with --write")
    s.add_argument("--write", metavar="PIECES", help="all or comma list of " + ",".join(PIECES))
    add("uninstall", cmd_uninstall, "remove hooks, CLAUDE.md block and .dokime/; keep records")
    s = add("scan", cmd_scan, "find the earliest recent commit where a run: condition passes")
    s.add_argument("--condition", required=True)
    s.add_argument("--limit", type=int, default=20, help="commits to walk back (default 20)")
    s.add_argument("--timeout", type=int, default=120, help="seconds per run (default 120)")
    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    try:
        os.chdir(root())
        data, text = a.fn(a)
    except Flagged as ex:
        print(json.dumps(ex.rep, indent=2, sort_keys=True) if a.json else str(ex),
              file=sys.stderr if ex.code == 2 else sys.stdout)
        return ex.code
    except (DokimeError, OSError, ValueError) as ex:
        if a.json:
            print(json.dumps({"error": str(ex)}))
        else:
            print("dokime: " + str(ex), file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(data, indent=2, sort_keys=True))
    elif text:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
