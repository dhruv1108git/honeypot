import unittest

from cotmon import store
from helpers import make_snapshot, memory_store

DAY = ("2026-08-20T00:00:00Z", "2026-08-21T00:00:00Z")


class SelectTrajectoriesTest(unittest.TestCase):
    def test_selects_snapshots_in_window(self):
        conn = memory_store(
            make_snapshot("s1", "ep1", 4),
            make_snapshot("s2", "ep2", 6, captured_at="2026-08-21T01:00:00Z"),
        )
        got = store.select_trajectories(conn, *DAY)
        self.assertEqual([t.snapshot_id for t in got], ["s1"])

    def test_messages_are_ordered(self):
        conn = memory_store(make_snapshot("s1", "ep1", 7))
        (traj,) = store.select_trajectories(conn, *DAY)
        self.assertEqual([m.idx for m in traj.messages], list(range(7)))
        self.assertEqual(traj.n_messages, 7)


if __name__ == "__main__":
    unittest.main()
