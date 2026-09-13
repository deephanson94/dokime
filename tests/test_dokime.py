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

    def tearDown(self):
        os.chdir(self.old)
        shutil.rmtree(self.tmp)

    def head(self):
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()

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
        code, out, _ = run("open", "u", "--condition", "run: true", "--json")
        self.assertEqual(code, 0)
        d = json.loads(out)["unit"]
        self.assertEqual(d["done_condition_sha256"], dokime.sha256_text("run: true"))
        self.assertEqual(dokime.load_unit("u")["status"], "open")
        self.assertEqual(run("open", "u", "--condition", "x")[0], 1)
        self.assertEqual(run("open", "bad name", "--condition", "x")[0], 1)


class PinTest(RepoCase):
    def test_pin_states(self):
        run("open", "u", "--condition", "run: true")
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
        self.assertIn("outside the ledger", self.verify("commit", self.head()))
        git("commit", "-q", "--allow-empty", "-m", "empty", when="2026-06-01T00:00:00+00:00")
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

    def test_session_start_ticks_clock(self):
        self.assertEqual(run("session-start")[0], 0)
        self.assertEqual(run("session-start")[0], 0)
        self.assertEqual(len(dokime.clock()), 2)


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
        self.assertTrue(out.startswith("dokime: ok | intent present | 0 units | sessions unknown"))

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
                {"name": "feat", "done_condition": "run: true", "ceiling_sessions": 1}))
            s = fixture.from_handoffs(os.path.join(tmp, "h"))
            self.assertEqual([x["unit"] for x in s], ["feat", None])
            self.assertEqual(s[0]["open"][0]["condition"], "run: true")
            f = [r["flags"] for r in fixture.replay(os.path.join(tmp, "out"), s)]
            self.assertIn("met-but-open(feat)", f[1])
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
