import unittest

from cotmon import store
from cotmon.transcript import render_transcript
from helpers import make_snapshot, memory_store


class RenderTranscriptTest(unittest.TestCase):
    def test_includes_every_message(self):
        conn = memory_store(make_snapshot("s1", "ep1", 5))
        (traj,) = store.select_trajectories(conn, "2026-08-20T00:00:00Z", "2026-08-21T00:00:00Z")
        text = render_transcript(traj, 120_000)
        for i in range(5):
            self.assertIn(f"### [{i}]", text)

    def test_cuts_from_the_front(self):
        conn = memory_store(make_snapshot("s1", "ep1", 60))
        (traj,) = store.select_trajectories(conn, "2026-08-20T00:00:00Z", "2026-08-21T00:00:00Z")
        text = render_transcript(traj, 1500)
        self.assertIn("### [59]", text)
        self.assertNotIn("### [1] USER", text)


if __name__ == "__main__":
    unittest.main()
