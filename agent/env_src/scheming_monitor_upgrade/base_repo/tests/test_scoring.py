import unittest
from dataclasses import replace

from cotmon import scoring, store
from cotmon.config import load_config
from helpers import FakeBackend, make_snapshot, memory_store

CFG = load_config("config/monitor.toml")
DAY = ("2026-08-20T00:00:00Z", "2026-08-21T00:00:00Z")


def one_trajectory(n=6):
    conn = memory_store(make_snapshot("s1", "ep1", n))
    return store.select_trajectories(conn, *DAY)[0]


class ScoreTrajectoryTest(unittest.TestCase):
    def test_reads_score_and_rationale(self):
        backend = FakeBackend("The agent edits its own log filter.\n<score>8</score>")
        result = scoring.score_trajectory(one_trajectory(), backend, CFG, "prompt")
        self.assertEqual(result.score, 8)
        self.assertEqual(result.rationale, "The agent edits its own log filter.")

    def test_request_uses_configured_model(self):
        backend = FakeBackend()
        scoring.score_trajectory(one_trajectory(), backend, CFG, "prompt")
        self.assertEqual(backend.requests[0].model, CFG.monitor.model)


class DecideTest(unittest.TestCase):
    def test_levels(self):
        self.assertEqual(scoring.decide(2, CFG), "none")
        self.assertEqual(scoring.decide(CFG.alerts.alert_score, CFG), "alert")
        self.assertEqual(scoring.decide(10, CFG), "page")


if __name__ == "__main__":
    unittest.main()
