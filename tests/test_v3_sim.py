"""[merge-v3] The simulation harness (tools/sim) as part of the test suite:
every fault scenario of every feature, one test per feature, one sub-test per
scenario. The 8-hour soak is not run here (python -I tools/sim/run.py --soak).

The scenarios drive real game modules against local fakes only: a fake gpsd,
a fake Kismet REST API, a fake wigle.net / WDGWars portal, a fake dump1090
feed, fake rtl_power / rtl_433 output and scripted hardware probes.
"""
import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.sim import core  # noqa: E402

core.load_all()
FEATURES = sorted({feature for feature, _name, _function, _limit, soak in core.SCENARIOS if not soak})


class SimulationTests(unittest.TestCase):
    """One test_<feature> method per feature is added below."""

    def test_harness_lists_every_feature_the_build_promises(self):
        self.assertEqual(FEATURES, ["433", "adsb", "backup", "gpsd", "hardware", "kismet", "lora", "portal",
                                    "storage", "uploads"])
        soak = [name for feature, name, _f, _l, is_soak in core.SCENARIOS if is_soak]
        self.assertEqual(len(soak), 1)
        self.assertGreaterEqual(len(core.SCENARIOS), 60)
        names = [(feature, name) for feature, name, _f, _l, _s in core.SCENARIOS]
        self.assertEqual(len(names), len(set(names)))

    def test_a_failing_crashing_or_hanging_scenario_is_reported_not_fatal(self):
        import time

        def fails(ctx):
            core.check(False, "expected failure")

        def crashes(ctx):
            raise RuntimeError("boom")

        def hangs(ctx):
            time.sleep(5)

        cleaned = []

        def cleans(ctx):
            ctx.cleanup(cleaned.append, "done")
            ctx.patch(core, "DEFAULT_LIMIT", 1.0)

        self.assertEqual(core._run_one("x", "f", fails, 5)["status"], "fail")
        result = core._run_one("x", "c", crashes, 5)
        self.assertEqual(result["status"], "crash")
        self.assertIn("RuntimeError: boom", result["detail"])
        self.assertEqual(core._run_one("x", "h", hangs, 0.2)["status"], "hang")
        limit = core.DEFAULT_LIMIT
        self.assertEqual(core._run_one("x", "ok", cleans, 5)["status"], "pass")
        self.assertEqual((cleaned, core.DEFAULT_LIMIT), (["done"], limit))

    def test_the_run_fails_if_a_scenario_touches_the_players_files(self):
        saved, before, load_all = core.SCENARIOS[:], core._player_files, core.load_all

        def harmless(ctx):
            pass
        states = iter([{"secrets.conf": None}, {"secrets.conf": (29, 1)}])
        try:
            core.SCENARIOS[:] = [("guard", "noop", harmless, 5, False)]
            core._player_files = lambda: next(states)
            core.load_all = lambda: None
            out = io.StringIO()
            self.assertEqual(core.run(features=["guard"], out=out), 1)
            self.assertIn("touched secrets.conf", out.getvalue())
        finally:
            core.SCENARIOS[:] = saved
            core._player_files = before
            core.load_all = load_all


def _feature_test(feature):
    def test(self):
        ran = 0
        for owner, name, function, limit, soak in core.SCENARIOS:
            if owner != feature or soak:
                continue
            ran += 1
            with self.subTest(scenario=name):
                result = core._run_one(owner, name, function, limit)
                self.assertEqual(result["status"], "pass", "%s: %s\n%s" % (owner, name, result.get("detail", "")))
        self.assertGreater(ran, 0)
    test.__doc__ = "every %s scenario of tools/sim" % feature
    return test


for _feature in FEATURES:
    setattr(SimulationTests, "test_scenarios_" + _feature.replace("-", "_"), _feature_test(_feature))


if __name__ == "__main__":
    unittest.main()
