#!/usr/bin/env python3
"""dokime (doh-kee-MAY): an agreement, a ledger of work units, a boundary check.

Stdlib + git only. See intent.md for goals, non-goals, invariants.
"""
import argparse, datetime, hashlib, json, os, re, subprocess, sys

UNITS, HANDOFFS, INTENT = "units", "handoffs", "intent.md"
CLOCK = os.path.join(".dokime", "sessions.log")
STATUSES = ("open", "met", "closed")
KINDS = ("commit", "artifact", "sha256")
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
LEDGER_DIRS = (HANDOFFS + "/", UNITS + "/", ".dokime/")


class DokimeError(Exception):
    pass


# ---------------------------------------------------------------- helpers
def now():
    if os.environ.get("DOKIME_NOW"):
        return os.environ["DOKIME_NOW"]
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def utc_date(iso):
    return datetime.datetime.fromisoformat(iso).astimezone(datetime.timezone.utc).date().isoformat()


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
    with open(path) as f:
        return f.read()


def sha256_text(s):
    return hashlib.sha256(s.encode()).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def run_condition(cond, timeout=600):
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
    if not NAME_RE.match(d["name"]):
        e.append("bad name: " + d["name"])
    if d["status"] not in STATUSES:
        e.append("bad status: " + d["status"])
    if d["ceiling_sessions"] < 1:
        e.append("ceiling_sessions must be >= 1")
    if not d["done_condition"].strip():
        e.append("done_condition is empty")
    if not re.match(r"^[0-9a-f]{64}$", d["done_condition_sha256"]):
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
    if not SHA_RE.match(ref):
        return "not a SHA (symbolic refs are refused)"
    full = git("rev-parse", "--verify", "--quiet", ref + "^{commit}")
    if not full:
        return "no such commit"
    if git("merge-base", "--is-ancestor", full, "HEAD") is None:
        return "not reachable from HEAD"
    when = git("show", "-s", "--format=%cI", full) or ""
    if when[:19] < opened_at[:19]:
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
    if kind == "artifact":
        if not os.path.exists(ref):
            return "path does not exist"
        if git("ls-files", "--error-unmatch", ref) is None:
            return "path is not tracked by git"
        return None
    path, _, digest = ref.rpartition(":")
    if not path or not re.match(r"^[0-9a-f]{64}$", digest):
        return "expected path:sha256"
    if not os.path.isfile(path):
        return "path does not exist"
    return None if sha256_file(path) == digest else "content hash does not match"


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
    if not NAME_RE.match(a.name):
        raise DokimeError("bad unit name: " + a.name)
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
    return bool(u) and utc_date(u["opened_at"]) <= date and (u["status"] != "closed" or (u.get("closed_at") or "9") [:10] > date)


def check_unit(u, at, starts, hs, days):
    r = {"name": u["name"], "status": u["status"], "flags": []}
    r["condition"] = run_condition(u["done_condition"]) if at != "stop" else "unverified"
    bad = [ev for ev in u["evidence"] if verify_evidence(ev, u["opened_at"])]
    r["evidence"] = "valid" if not bad else "invalid"
    r["pin"] = pin_status(u)
    if r["pin"].startswith("CHANGED"):
        r["flags"].append("pin-changed")
    if bad:
        r["flags"].append("evidence-invalid")
    if u["status"] == "open" and r["condition"] == "pass":
        r["flags"].append("met-but-open")
    if u["status"] != "closed" and at != "stop":
        opened = u["opened_at"]
        r["sessions"] = None if starts is None else 1 + sum(1 for t in starts if t > opened)
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
    for u in units:
        r = check_unit(u, at, starts, hs, days)
        rep["units"].append(r)
        rep["flags"] += ["%s(%s)" % (f, u["name"]) for f in r["flags"]]
    if at != "stop":
        last = hs[-1] if hs else None
        prev = hs[-2][0] if len(hs) > 1 else ""
        rep["handoffs"] = {"count": len(hs), "last": last and last[1], "unit": last and last[2],
                           "unit_valid": bool(last and last[2] and unit_open_on(units, last[2], last[0]))}
        rep["sessions"] = {"clock": None if starts is None else len(starts), "handoffs": len(hs),
                           "commit_days": len(set(days)), "divergent": False}
        if starts is not None and (len(hs) < len(starts) - 1 or len(set(days)) > len(starts)):
            rep["sessions"]["divergent"] = True
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


def cmd_session_start(a):
    """SessionStart hook: append to the clock, print the full check, never block."""
    os.makedirs(os.path.dirname(CLOCK), exist_ok=True)
    with open(CLOCK, "a") as f:
        f.write("%s %s\n" % (now(), git("rev-parse", "--short", "HEAD") or "-"))
    rep = run_check("all")
    return rep, render(rep)


def cmd_stop_hook(a):
    """Stop hook: ledger-integrity rules only; exit 2 with stderr only under --strict."""
    try:
        if json.load(sys.stdin).get("stop_hook_active"):
            return {"skipped": True}, ""
    except (ValueError, OSError):
        pass
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
    s.add_argument("--at", choices=("all", "start", "stop"), default="all",
                   help="stop = ledger integrity only (no run: conditions, no session rules)")
    s.add_argument("--warn-only", action="store_true", help="exit 0 even when flagged")
    add("status", cmd_status, "one-line status")
    add("session-start", cmd_session_start, "SessionStart hook: tick the clock, print the check")
    s = add("stop-hook", cmd_stop_hook, "Stop hook: integrity rules; blocks only with --strict")
    s.add_argument("--strict", action="store_true", help="exit 2 (blocks) when flagged")
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
    except DokimeError as ex:
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
