import io, json, os, shutil, subprocess, sys, tempfile, unittest
from contextlib import redirect_stdout, redirect_stderr

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import dokime, fixture  # noqa: E402


def git(*a, when=None):
    env = dict(os.environ)
    if when:
        env.update(GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
    subprocess.run(["git", *a], check=True, capture_output=True, env=env)


def write(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def run(*argv):
    """Run dokime.main in-process; return (exit, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = dokime.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class RepoCase(unittest.TestCase):
    def setUp(self):
        sys.stdin = io.StringIO("")
        self.tmp = tempfile.mkdtemp()
        self.old = os.getcwd()
        os.chdir(self.tmp)
        git("init", "-q", ".")
        git("config", "user.email", "t@t")
        git("config", "user.name", "T")
        write("intent.md", fixture.INTENT)
        write("base.txt", "base\n")
        git("add", "-A")
        git("commit", "-qm", "base", when="2026-01-01T00:00:00+00:00")
        os.environ.pop("DOKIME_NOW", None)
        self.real_file = dokime.__file__
        shutil.copy(self.real_file, "tools-dokime.py")
        dokime.__file__ = os.path.abspath("tools-dokime.py")

    def tearDown(self):
        dokime.__file__ = self.real_file
        sys.stdin = sys.__stdin__
        os.chdir(self.old)
        shutil.rmtree(self.tmp)

    def head(self):
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()

    def start(self):
        sys.stdin = io.StringIO('{"source": "startup"}')
        self.assertEqual(run("session-start")[0], 0)
        sys.stdin = io.StringIO("")

    def work_commit(self, name="w.txt", when="2026-06-01T00:00:00+00:00"):
        write(name, name + "\n")
        git("add", "-A")
        git("commit", "-qm", "work", when=when)
        return self.head()


class SchemaTest(RepoCase):
    def good(self):
        return {"name": "u", "opened_at": "2026-01-02T00:00:00+00:00", "status": "open",
                "ceiling_sessions": 3, "done_condition": "x",
                "done_condition_sha256": dokime.sha256_text("x"), "evidence": []}

    def test_valid(self):
        self.assertEqual(dokime.validate_unit(self.good()), [])

    def test_missing_and_types(self):
        d = self.good(); del d["evidence"]
        self.assertIn("missing field: evidence", dokime.validate_unit(d))
        d = self.good(); d["ceiling_sessions"] = True
        self.assertTrue(any("ceiling_sessions" in e for e in dokime.validate_unit(d)))
        d = self.good(); d["ceiling_sessions"] = 0
        self.assertTrue(dokime.validate_unit(d))

    def test_status_evidence_rule(self):
        d = self.good(); d["status"] = "met"
        self.assertIn("status met without evidence", dokime.validate_unit(d))
        d["evidence"] = [{"kind": "nope", "ref": "x"}]
        self.assertTrue(any("evidence[0]" in e for e in dokime.validate_unit(d)))
        d["status"] = "weird"
        self.assertIn("bad status: weird", dokime.validate_unit(d))

    def test_open_writes_pinned_json(self):
        code, _, err = run("open", "u", "--condition", "run: true")
        self.assertEqual((code, "already passes" in err), (1, True))
        code, out, _ = run("open", "u", "--condition", "run: test -f ok", "--json")
        self.assertEqual(code, 0)
        d = json.loads(out)["unit"]
        self.assertEqual(d["done_condition_sha256"], dokime.sha256_text("run: test -f ok"))
        self.assertEqual(dokime.load_unit("u")["status"], "open")
        self.assertEqual(run("open", "u", "--condition", "x")[0], 1)
        self.assertEqual(run("open", "bad name", "--condition", "x")[0], 1)


class PinTest(RepoCase):
    def test_pin_states(self):
        run("open", "u", "--condition", "run: test -f ok")
        d = dokime.load_unit("u")
        self.assertTrue(dokime.pin_status(d).startswith("UNVERIFIABLE"))
        git("add", "-A"); git("commit", "-qm", "open u")
        self.assertEqual(dokime.pin_status(d), "unchanged")
        d["done_condition"] = "run: false"; dokime.save_unit(d)
        self.assertTrue(dokime.pin_status(dokime.load_unit("u")).startswith("CHANGED (sha256"))
        d["done_condition_sha256"] = dokime.sha256_text("run: false"); dokime.save_unit(d)
        self.assertTrue(dokime.pin_status(dokime.load_unit("u")).startswith("CHANGED since"))
        rep = dokime.run_check("stop")
        self.assertIn("pin-changed(u)", rep["flags"])


class EvidenceTest(RepoCase):
    def setUp(self):
        super().setUp()
        os.environ["DOKIME_NOW"] = "2026-03-01T00:00:00+00:00"
        run("open", "u", "--condition", "x")
        git("add", "-A"); git("commit", "-qm", "open u", when="2026-03-01T00:00:00+00:00")
        self.opened = dokime.load_unit("u")["opened_at"]

    def verify(self, kind, ref):
        return dokime.verify_evidence({"kind": kind, "ref": ref}, self.opened)

    def test_commit_rules(self):
        self.assertIn("symbolic", self.verify("commit", "HEAD"))
        self.assertIn("before unit opened", self.verify("commit", subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD~1"], text=True).strip()))
        self.assertIn("before unit opened", self.verify("commit", self.head()))  # same second as open
        write("units/.gitkeep", ""); git("add", "-A"); git("commit", "-qm", "ledger", when="2026-06-01T00:00:00+00:00")
        self.assertIn("outside the ledger", self.verify("commit", self.head()))
        git("commit", "-q", "--allow-empty", "-m", "empty", when="2026-06-01T00:00:01+00:00")
        self.assertIn("outside the ledger", self.verify("commit", self.head()))
        good = self.work_commit()
        self.assertIsNone(self.verify("commit", good))
        self.assertIn("no such commit", self.verify("commit", "deadbeef"))
        git("checkout", "-qb", "side"); side = self.work_commit("s.txt"); git("checkout", "-q", "-")
        self.assertIn("not reachable", self.verify("commit", side))

    def test_artifact_and_sha256(self):
        self.assertIn("does not exist", self.verify("artifact", "nope.txt"))
        write("z.txt", "z\n")
        self.assertIn("not tracked", self.verify("artifact", "z.txt"))
        git("add", "z.txt")
        self.assertIsNone(self.verify("artifact", "z.txt"))
        self.assertIn("path:sha256", self.verify("sha256", "z.txt"))
        self.assertIn("does not match", self.verify("sha256", "z.txt:" + "0" * 64))
        self.assertIsNone(self.verify("sha256", "z.txt:" + dokime.sha256_file("z.txt")))
        write("y.txt", "y\n")
        self.assertIn("not tracked", self.verify("sha256", "y.txt:" + dokime.sha256_file("y.txt")))
        self.assertIn("ledger files", self.verify("artifact", "units/u.json"))
        self.assertIn("ledger files", self.verify("sha256", "./handoffs/x.md:" + "0" * 64))

    def test_close_refuses_then_closes(self):
        self.assertEqual(run("close", "u")[0], 1)
        self.assertEqual(run("close", "u", "--evidence", "commit:HEAD")[0], 1)
        self.assertEqual(dokime.load_unit("u")["status"], "open")
        good = self.work_commit()
        code, out, _ = run("close", "u", "--evidence", "commit:" + good, "--by", "me", "--json")
        self.assertEqual(code, 0)
        d = json.loads(out)["unit"]
        self.assertEqual((d["status"], d["closed_by"], d["evidence"][0]["ref"]), ("closed", "me", good))
        self.assertEqual(run("close", "u", "--evidence", "commit:" + good)[0], 1)

    def test_run_condition_gates_close_and_force(self):
        run("open", "r", "--condition", "run: test -f ok")
        good = self.work_commit()
        self.assertEqual(run("close", "r", "--evidence", "commit:" + good)[0], 1)
        write("ok", "")
        self.assertEqual(run("met", "r", "--evidence", "commit:" + good)[0], 0)
        self.assertEqual(dokime.load_unit("r")["status"], "met")
        code, _, _ = run("close", "u", "--evidence", "artifact:missing", "--force", "reviewed")
        self.assertEqual(code, 0)
        self.assertEqual(dokime.load_unit("u")["force_reason"], "reviewed")


class CountTest(RepoCase):
    def test_sessions_and_divergence(self):
        os.environ["DOKIME_NOW"] = "2026-03-01T09:30:00+00:00"
        run("open", "u", "--condition", "x", "--ceiling", "2")
        u = dokime.load_unit("u")
        r = dokime.check_unit(u, "all", None, [], [])
        self.assertIsNone(r["sessions"])
        self.assertNotIn("over-ceiling", r["flags"])
        starts = ["2026-03-01T09:00:00+00:00", "2026-03-02T09:00:00+00:00", "2026-03-03T09:00:00+00:00"]
        r = dokime.check_unit(u, "all", starts, [("2026-03-01", "f", "u"), ("2026-03-02", "g", "u")],
                              ["2026-03-01", "2026-03-02", "2026-03-02"])
        self.assertEqual((r["sessions"], r["handoffs"], r["commit_days"]), (3, 2, 2))
        self.assertIn("over-ceiling", r["flags"])
        r = dokime.check_unit(u, "all", starts[:1], [("2026-03-0%d" % d, "f", "u") for d in (1, 2, 3, 4)], [])
        self.assertEqual(r["sessions"], 4)  # handoffs outnumber the clock: handoffs win
        self.assertIn("over-ceiling", r["flags"])
        write(dokime.CLOCK, "\n".join(starts) + "\n")
        rep = dokime.run_check("all")
        self.assertEqual(rep["sessions"]["clock"], 3)
        self.assertTrue(rep["sessions"]["divergent"])  # 3 starts, 0 handoffs

    def test_handoff_parsing_and_unit_validity(self):
        write("handoffs/2026-03-02-x.md", "notes\nUnit: u\n")
        write("handoffs/not-a-handoff.md", "unit: u\n")
        hs = dokime.handoffs()
        self.assertEqual(hs, [("2026-03-02", "2026-03-02-x.md", "u")])
        os.environ["DOKIME_NOW"] = "2026-03-01T00:00:00+00:00"
        run("open", "u", "--condition", "x")
        units = dokime.all_units()
        self.assertTrue(dokime.unit_open_on(units, "u", "2026-03-02"))
        self.assertFalse(dokime.unit_open_on(units, "u", "2026-02-28"))
        self.assertFalse(dokime.unit_open_on(units, "ghost", "2026-03-02"))

    def test_session_start_tick_rules(self):
        os.environ["DOKIME_NOW"] = "2026-03-01T09:00:00+00:00"
        sys.stdin = io.StringIO("")
        code, out, _ = run("session-start", "--json")   # no payload: never ticks
        self.assertEqual((code, json.loads(out)["ticked"], dokime.clock()), (0, False, None))
        for source, when, ticks in (("startup", "2026-03-01T09:00:00+00:00", 1),
                                    ("resume", "2026-03-01T12:00:00+00:00", 1),   # 3h later: same session
                                    ("compact", "2026-03-01T13:00:00+00:00", 1),
                                    ("resume", "2026-03-02T09:00:00+00:00", 2),   # next morning: new session
                                    ("clear", "2026-03-02T09:30:00+00:00", 3),
                                    ("bogus", "2026-03-03T09:30:00+00:00", 3)):
            os.environ["DOKIME_NOW"] = when
            sys.stdin = io.StringIO(json.dumps({"source": source}))
            code, out, _ = run("session-start", "--json")
            self.assertEqual((code, len(dokime.clock())), (0, ticks), source)
        os.environ["DOKIME_NOW"] = "2026-03-02T10:00:00+00:00"   # 30 min after the last tick
        sys.stdin = io.StringIO('{"source": "resume"}')
        code, out, _ = run("session-start")
        self.assertTrue(out.startswith("dokime: ok |"), out)   # clean resume prints the one-liner
        self.assertEqual(len(dokime.clock()), 3)
        sys.stdin = sys.__stdin__


class CheckTest(RepoCase):
    def test_clean_repo_and_intent(self):
        code, out, _ = run("check")
        self.assertEqual(code, 0)
        self.assertIn("intent: present", out)
        os.remove("intent.md")
        code, out, _ = run("check", "--json")
        self.assertEqual(code, 1)
        self.assertIn("intent-missing", json.loads(out)["flags"])
        self.assertEqual(run("check", "--warn-only")[0], 0)
        self.assertEqual(run("check", "--at", "stop")[0], 0)

    def test_status_line(self):
        code, out, _ = run("status")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("dokime: ok | intent present | hooks absent | 0 units | sessions unknown"), out)

    def test_stop_hook(self):
        run("open", "u", "--condition", "x")
        d = dokime.load_unit("u"); d["done_condition"] = "y"; dokime.save_unit(d)
        sys.stdin = io.StringIO("{}")
        code, out, _ = run("stop-hook")
        self.assertEqual((code, json.loads(out)["systemMessage"]), (0, "dokime: pin-changed(u)"))
        sys.stdin = io.StringIO("{}")
        code, _, err = run("stop-hook", "--strict")
        self.assertEqual(code, 2)
        self.assertIn("PIN-CHANGED", err)
        sys.stdin = io.StringIO('{"stop_hook_active": true}')
        self.assertEqual(run("stop-hook", "--strict")[0], 0)
        sys.stdin = sys.__stdin__


class ReplayTest(unittest.TestCase):
    def flags(self, sessions):
        tmp = tempfile.mkdtemp()
        try:
            return [r["flags"] for r in fixture.replay(tmp, sessions)]
        finally:
            shutil.rmtree(tmp)

    def test_incident_a_flagged_at_session_3(self):
        f = self.flags(fixture.scenario_a())
        self.assertEqual(f[0], [])
        self.assertEqual(f[1], [])
        self.assertIn("met-but-open(feat)", f[2])
        self.assertNotIn("over-ceiling(feat)", f[2])
        self.assertIn("over-ceiling(feat)", f[3])
        self.assertTrue(all("met-but-open(feat)" in x for x in f[2:]))

    def test_incident_b_flagged_at_session_2(self):
        f = self.flags(fixture.scenario_b())
        self.assertEqual(f[0], [])
        self.assertTrue(all(any(x.startswith("work-without-unit(") for x in s) for s in f[1:]))

    def test_from_handoffs_roundtrip(self):
        tmp = tempfile.mkdtemp()
        try:
            for i, u in ((1, "feat"), (2, None)):
                write(os.path.join(tmp, "h", "2026-09-0%d-x.md" % i), ("unit: %s\n" % u if u else "") + "n\n")
            os.makedirs(os.path.join(tmp, "units"))
            write(os.path.join(tmp, "units", "feat.json"), json.dumps(
                {"name": "feat", "done_condition": "run: test -f work-1.txt", "ceiling_sessions": 1}))
            s = fixture.from_handoffs(os.path.join(tmp, "h"))
            self.assertEqual([x["unit"] for x in s], ["feat", None])
            self.assertEqual(s[0]["open"][0]["condition"], "run: test -f work-1.txt")
            f = [r["flags"] for r in fixture.replay(os.path.join(tmp, "out"), s)]
            self.assertIn("met-but-open(feat)", f[1])
        finally:
            shutil.rmtree(tmp)



class InitTest(RepoCase):
    def test_bare_repo_init_then_check(self):
        shutil.rmtree(".git"); os.remove("intent.md"); os.remove("base.txt")
        git("init", "-q", ".")
        code, out, _ = run("check")
        self.assertEqual((code, "intent-missing" in out), (1, True))
        code, out, _ = run("init")
        self.assertEqual(code, 0)
        self.assertIn("nothing written", out)
        self.assertIn("+++ .claude/settings.json", out)
        self.assertFalse(os.path.exists(".claude"))
        code, out, _ = run("init", "--write=intent")  # no terminal, no answers
        self.assertEqual((code, "intent: not written" in out, os.path.exists("intent.md")), (0, True, False))
        write("answers.txt", "g1\ng2\n\nn1\n\na1\n\ni1\n\ny\n")
        os.environ["DOKIME_INTERVIEW"] = "answers.txt"
        try:
            code, out, _ = run("init", "--write=all", "--json")
        finally:
            del os.environ["DOKIME_INTERVIEW"]
        self.assertEqual(code, 0)
        self.assertEqual(set(json.loads(out)["pieces"].values()), {"written"})
        self.assertIn("## Goals\n- g1\n- g2\n", dokime.read("intent.md"))
        self.assertEqual(run("check")[0], 0)
        hooks = json.loads(dokime.read(dokime.SETTINGS))["hooks"]
        self.assertEqual(set(hooks), {"SessionStart", "Stop"})
        self.assertEqual(hooks["SessionStart"][0]["hooks"][0]["command"], 'python3 "$CLAUDE_PROJECT_DIR/tools-dokime.py" session-start')
        self.assertIn(dokime.MARK, dokime.read("CLAUDE.md"))
        self.assertIn(".dokime/", dokime.read(".gitignore"))
        code, out, _ = run("init", "--json")
        self.assertEqual(set(json.loads(out)["pieces"].values()), {"present"})

    def test_interview_declined_writes_nothing(self):
        os.remove("intent.md")
        write("answers.txt", "g\n\nn\n\na\n\ni\n\nno\n")
        os.environ["DOKIME_INTERVIEW"] = "answers.txt"
        try:
            self.assertIn("intent: not written", run("init", "--write=intent")[1])
        finally:
            del os.environ["DOKIME_INTERVIEW"]
        self.assertFalse(os.path.exists("intent.md"))

    def test_existing_hooks_never_merged(self):
        write(dokime.SETTINGS, '{"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "echo"}]}]}}')
        code, out, _ = run("init", "--write=hooks")
        self.assertEqual(code, 0)
        self.assertIn("MANUAL", out)
        self.assertNotIn("dokime", dokime.read(dokime.SETTINGS))
        write(dokime.SETTINGS, '{"permissions": {"allow": ["Bash"]}}')
        run("init", "--write=hooks")
        s = json.loads(dokime.read(dokime.SETTINGS))
        self.assertEqual((s["permissions"], set(s["hooks"])), ({"allow": ["Bash"]}, {"SessionStart", "Stop"}))
        self.assertEqual(run("init", "--write=bogus")[0], 1)
        os.remove(dokime.SETTINGS)
        dokime.__file__ = self.real_file  # outside the repo and not on PATH
        code, out, _ = run("init", "--write=hooks")
        self.assertEqual((code, "neither on PATH nor inside" in out, os.path.exists(dokime.SETTINGS)), (0, True, False))
        self.assertIn('"dokime session-start"', out)

    def test_uninstall_keeps_records_and_foreign_config(self):
        write(dokime.SETTINGS, '{"permissions": {"allow": ["Bash"]}}')
        write("CLAUDE.md", "# mine\n")
        run("init", "--write=all")
        self.start()
        code, out, _ = run("uninstall")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(dokime.read(dokime.SETTINGS)), {"permissions": {"allow": ["Bash"]}})
        self.assertEqual(dokime.read("CLAUDE.md"), "# mine\n")
        self.assertTrue(os.path.exists(".dokime"))
        self.assertNotIn(".dokime/", dokime.read(".gitignore"))
        self.assertTrue(os.path.isdir("units") and os.path.isdir("handoffs") and os.path.exists("intent.md"))
        write(dokime.SETTINGS, '{"a": [1,2]}')
        run("uninstall")
        self.assertEqual(dokime.read(dokime.SETTINGS), '{"a": [1,2]}')
        run("init", "--write=claude-md")
        self.assertTrue(dokime.read("CLAUDE.md").startswith("# mine\n\n" + dokime.MARK))


class ScanTest(RepoCase):
    def test_scan_finds_earliest_pass(self):
        self.work_commit("a.txt"); self.work_commit("ok"); self.work_commit("b.txt")
        code, out, _ = run("scan", "--condition", "run: test -f ok", "--json")
        self.assertEqual(code, 0)
        r = json.loads(out)
        self.assertEqual((r["commits_after"], r["checked"]), (1, 3))
        self.assertEqual(r["earliest_pass"][:7], subprocess.check_output(["git", "rev-parse", "--short=7", "HEAD~1"], text=True).strip())
        code, out, _ = run("scan", "--condition", "run: test -f nope")
        self.assertIn("earliest passing: none", out)
        self.assertEqual(run("scan", "--condition", "prose")[0], 1)
        self.assertEqual(subprocess.check_output(["git", "worktree", "list"], text=True).count("\n"), 1)


class RegressionTest(RepoCase):
    """Findings from the user-test panel."""

    def open_at(self, name, when, cond="x", ceiling=3):
        os.environ["DOKIME_NOW"] = when
        self.assertEqual(run("open", name, "--condition", cond, "--ceiling", str(ceiling))[0], 0)
        git("add", "-A"); git("commit", "-qm", "open " + name, when=when)
        return dokime.load_unit(name)["opened_at"]

    def test_evidence_date_uses_offsets(self):
        opened = self.open_at("u", "2026-09-13T10:00:00+00:00")
        after = self.work_commit("a.txt", when="2026-09-13T05:00:00-07:00")   # 12:00Z, after open
        self.assertIsNone(dokime.verify_evidence({"kind": "commit", "ref": after}, opened))
        before = self.work_commit("b.txt", when="2026-09-13T18:00:00+09:00")  # 09:00Z, before open
        self.assertIn("before unit opened", dokime.verify_evidence({"kind": "commit", "ref": before}, opened))

    def test_naive_and_zulu_timestamps_are_utc(self):
        import time
        os.environ["TZ"] = "America/New_York"
        try:
            if hasattr(time, "tzset"):
                time.tzset()
            self.assertEqual(time.localtime(0).tm_hour, 19)  # the TZ change took effect
            self.assertEqual(dokime.utc_date("2026-09-13T22:00:00"), "2026-09-13")
            self.assertEqual(dokime.utc_date("2026-09-13T22:00:00Z"), "2026-09-13")
        finally:
            del os.environ["TZ"]
            if hasattr(time, "tzset"):
                time.tzset()

    def test_close_checks_pin_and_closed_units_are_not_reverified(self):
        opened = self.open_at("u", "2026-03-01T00:00:00+00:00")
        good = self.work_commit()
        d = dokime.load_unit("u"); d["done_condition"] = "y"
        d["done_condition_sha256"] = dokime.sha256_text("y"); dokime.save_unit(d)
        code, _, err = run("close", "u", "--evidence", "commit:" + good)
        self.assertEqual((code, "pin CHANGED" in err), (1, True))
        run("close", "u", "--evidence", "commit:" + good, "--force", "typo fix")
        git("add", "-A"); git("commit", "-qm", "close")
        git("rebase", "-q", "--force-rebase", "HEAD~2", when="2026-07-01T00:00:00+00:00")  # rewrites `good`
        self.assertIn("not reachable", dokime.verify_evidence({"kind": "commit", "ref": good}, opened))
        rep = dokime.run_check("all")
        u = rep["units"][0]
        self.assertEqual((u["evidence"], u["condition"], u["forced"]), ("valid", "unverified", "typo fix"))
        self.assertIn("FORCED(typo fix)", dokime.render(rep))
        self.assertNotIn("evidence-invalid(u)", rep["flags"])

    def test_closed_units_do_not_run_conditions(self):
        self.open_at("u", "2026-03-01T00:00:00+00:00", cond="run: touch ran-%d && false" % os.getpid())
        good = self.work_commit()
        run("close", "u", "--evidence", "commit:" + good, "--force", "x")
        os.remove("ran-%d" % os.getpid()) if os.path.exists("ran-%d" % os.getpid()) else None
        dokime.run_check("all")
        self.assertFalse(os.path.exists("ran-%d" % os.getpid()))

    def test_handoff_on_close_day_is_valid(self):
        self.open_at("u", "2026-03-01T00:00:00+00:00")
        good = self.work_commit(when="2026-03-02T00:00:00+00:00")
        os.environ["DOKIME_NOW"] = "2026-03-02T10:00:00+00:00"
        run("close", "u", "--evidence", "commit:" + good)
        write("handoffs/2026-03-02-done.md", "unit: u\n")
        rep = dokime.run_check("all")
        self.assertTrue(rep["handoffs"]["unit_valid"])
        self.assertEqual(rep["flags"], [])

    def test_second_handoff_same_day_cannot_hide_work(self):
        write("handoffs/2026-01-01-a.md", "unit: nope\n")
        write("handoffs/2026-01-01-b.md", "unit: nope\n")
        self.assertTrue(any(f.startswith("work-without-unit(2026-01-01-b") for f in dokime.run_check("all")["flags"]))
        write("handoffs/2999-01-01-z.md", "unit: nope\n")
        self.assertIn("handoff-future(2999-01-01-z.md)", dokime.run_check("all")["flags"])

    def test_session_count_when_unit_predates_clock(self):
        opened = self.open_at("u", "2026-03-01T00:00:00+00:00")
        for i in (1, 2, 3):
            os.environ["DOKIME_NOW"] = "2026-03-0%dT09:00:00+00:00" % (i + 1)
            self.start()
        rep = dokime.run_check("all")
        self.assertEqual((rep["units"][0]["sessions"], rep["sessions"]["clock"]), (3, 3))
        self.assertNotIn("over-ceiling(u)", rep["flags"])
        os.environ["DOKIME_NOW"] = "2026-03-05T09:00:00+00:00"
        self.start()
        self.assertIn("over-ceiling(u)", dokime.run_check("all")["flags"])

    def test_divergence_ignores_history_before_the_clock(self):
        for d in ("2026-01-02", "2026-01-03", "2026-01-04"):
            self.work_commit(d + ".txt", when=d + "T00:00:00+00:00")
        os.environ["DOKIME_NOW"] = "2026-02-01T09:00:00+00:00"
        self.start()
        self.assertFalse(dokime.run_check("all")["sessions"]["divergent"])

    def test_deleted_unit_is_flagged_and_names_must_match(self):
        self.open_at("u", "2026-03-01T00:00:00+00:00")
        os.remove("units/u.json")
        self.assertIn("unit-missing(u)", dokime.run_check("all")["flags"])
        write("units/alias.json", json.dumps({"name": "u", "opened_at": "2026-03-01T00:00:00+00:00", "status": "open",
                                             "ceiling_sessions": 1, "done_condition": "x",
                                             "done_condition_sha256": dokime.sha256_text("x"), "evidence": []}))
        self.assertEqual(run("list")[0], 1)

    def test_prose_condition_never_met(self):
        self.open_at("u", "2026-03-01T00:00:00+00:00")
        r = dokime.run_check("all")["units"][0]
        self.assertEqual(r["condition"], "unverified")
        self.assertNotIn("met-but-open", r["flags"])

    def test_no_tracebacks(self):
        write(dokime.SETTINGS, "{not json")
        code, _, err = run("init")
        self.assertEqual((code, "invalid JSON" in err), (1, True))
        os.remove(dokime.SETTINGS)
        write("units/dir.json/x", "")
        self.assertEqual(run("list")[0], 1)
        shutil.rmtree("units")
        write("handoffs/2026-01-01-bin.md", "unit: u\n")
        with open("handoffs/2026-01-01-bin.md", "ab") as f:
            f.write(b"\xff\xfe")
        self.assertEqual(run("check", "--warn-only")[0], 0)
        os.remove("intent.md")
        os.environ["DOKIME_INTERVIEW"] = "missing.txt"
        try:
            self.assertIn("intent: not written", run("init", "--write=intent")[1])
        finally:
            del os.environ["DOKIME_INTERVIEW"]
        sys.stdin = io.StringIO('"x"')
        self.assertEqual(run("stop-hook")[0], 0)
        sys.stdin = sys.__stdin__

    def test_every_command_has_json(self):
        for argv in (("list",), ("status",), ("session-start",), ("init",), ("uninstall",), ("check",)):
            code, out, _ = run(*argv, "--json")
            self.assertEqual(code, 0, argv)
            json.loads(out)

    def test_clock_absent_flag_only_with_open_unit(self):
        self.assertNotIn("clock-absent", dokime.run_check("all")["flags"])   # no units: not claimed
        self.open_at("u", "2026-03-01T00:00:00+00:00")
        rep = dokime.run_check("all")
        self.assertEqual((rep["hooks"], "clock-absent" in rep["flags"]), ("absent", True))
        self.assertNotIn("clock-absent", dokime.run_check("stop")["flags"])
        run("init", "--write=hooks")                       # hooks configured but never fired: still no clock
        self.assertIn("clock-absent", dokime.run_check("all")["flags"])
        os.remove(dokime.SETTINGS)
        self.start()                                       # a clock with no repo hook (user-level hooks): not flagged
        self.assertNotIn("clock-absent", dokime.run_check("all")["flags"])
        shutil.rmtree(".dokime"); run("init", "--write=hooks")
        self.start()
        rep = dokime.run_check("all")
        self.assertEqual((rep["hooks"], "clock-absent" in rep["flags"]), ("configured", False))
        write(dokime.SETTINGS, '{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "true # dokime"}]}]}}')
        self.assertNotIn("clock-absent", dokime.run_check("all")["flags"])  # clock ticked: the config is not the test
        shutil.rmtree(".dokime")
        self.assertIn("clock-absent", dokime.run_check("all")["flags"])     # decorative hook, no clock: flagged
        self.assertIn("hooks: configured", dokime.render(dokime.run_check("all")))

    def test_shallow_history_is_named(self):
        self.open_at("u", "2026-03-01T00:00:00+00:00")
        clone = tempfile.mkdtemp()
        try:
            subprocess.run(["git", "clone", "-q", "--depth", "1", "file://" + os.getcwd(), clone], check=True)
            os.chdir(clone)
            rep = dokime.run_check("all")
            self.assertEqual((rep["history"], rep["units"][0]["pin"]), ("shallow", "UNVERIFIABLE (shallow history)"))
            self.assertNotIn("history-shallow", rep["flags"])
            self.assertIn("history-shallow", dokime.run_check("stop")["flags"])
        finally:
            os.chdir(self.tmp)
            shutil.rmtree(clone)
        self.assertEqual(dokime.run_check("all")["history"], "full")

    def test_commit_days_use_committer_date(self):
        self.work_commit("late.txt", when="2026-09-14T01:00:00+08:00")   # 2026-09-13 in UTC
        self.assertEqual(dokime.work_dates()[-1], "2026-09-14")

    def test_condition_that_invokes_check_does_not_recurse(self):
        cond = "run: python3 %s check --json | grep -q hooks" % dokime.__file__
        dokime.save_unit({"name": "u", "opened_at": "2026-03-01T00:00:00+00:00", "status": "open", "ceiling_sessions": 1,
                          "done_condition": cond, "done_condition_sha256": dokime.sha256_text(cond), "evidence": []})
        self.assertEqual(dokime.run_condition(cond, timeout=30), "pass")  # inner check ran with conditions off
        os.environ["DOKIME_NESTED"] = "1"
        try:
            self.assertEqual(dokime.run_condition("run: true"), "unverified")
        finally:
            del os.environ["DOKIME_NESTED"]

    def test_init_dry_run_shows_every_piece(self):
        out = run("init")[1]
        self.assertIn("+++ units/.gitkeep (empty)", out)

    def test_line_budget(self):
        src = dokime.read(os.path.join(os.path.dirname(HERE), "dokime.py")).splitlines()
        self.assertLessEqual(sum(1 for ln in src if ln.strip() and not ln.strip().startswith("#")), 600)


if __name__ == "__main__":
    unittest.main()
