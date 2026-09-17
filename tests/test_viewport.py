from base import unittest
from qingjian.core import viewport


class BandTests(unittest.TestCase):
    def test_empty_list(self):
        self.assertEqual((0, -1), viewport.band(0, 0, 0))

    def test_the_band_covers_the_viewport_plus_overscan(self):
        self.assertEqual((8, 43), viewport.band(20, 31, 1000, 12))

    def test_it_never_runs_off_either_end(self):
        self.assertEqual((0, 15), viewport.band(0, 3, 1000, 12))
        self.assertEqual((984, 999), viewport.band(996, 999, 1000, 12))

    def test_a_single_visible_row(self):
        self.assertEqual((3, 27), viewport.band(15, 15, 1000, 12))

    def test_a_list_shorter_than_the_overscan(self):
        self.assertEqual((0, 4), viewport.band(0, 4, 5, 12))

    def test_an_inverted_range_is_tolerated(self):
        """indexAt can report nothing sensible while the view is laying out."""
        self.assertEqual((0, 12), viewport.band(0, -1, 100, 12))

    def test_no_overscan(self):
        self.assertEqual((20, 30), viewport.band(20, 30, 100, 0))

    def test_the_band_is_never_larger_than_the_list(self):
        low, high = viewport.band(0, 200, 50, 12)
        self.assertEqual((0, 49), (low, high))


class WindowTests(unittest.TestCase):
    def test_centred_window(self):
        self.assertEqual((20, 41), viewport.window(30, 1000, 10))

    def test_clamped_at_the_start(self):
        self.assertEqual((0, 11), viewport.window(0, 1000, 10))

    def test_clamped_at_the_end(self):
        self.assertEqual((989, 1000), viewport.window(999, 1000, 10))

    def test_shorter_than_the_window_loads_everything(self):
        self.assertEqual((0, 4), viewport.window(2, 4, 10))
        self.assertEqual((0, 21), viewport.window(0, 21, 10))
        self.assertEqual((0, 21), viewport.window(20, 21, 10))

    def test_empty(self):
        self.assertEqual((0, 0), viewport.window(0, 0, 10))

    def test_the_window_always_contains_the_index(self):
        for total in (1, 5, 80, 1000):
            for index in (0, total // 2, total - 1):
                low, high = viewport.window(index, total, 80)
                self.assertLessEqual(low, index)
                self.assertLess(index, high)


class RebuildTests(unittest.TestCase):
    def test_an_empty_window_always_rebuilds(self):
        self.assertTrue(viewport.needs_rebuild(0, (0, 0)))

    def test_a_window_already_against_either_end_is_left_alone(self):
        """The first eight and last eight items rebuilt the strip every step."""
        self.assertFalse(viewport.needs_rebuild(1, (0, 81), total=400))
        self.assertFalse(viewport.needs_rebuild(398, (319, 400), total=400))

    def test_a_window_covering_the_whole_list_never_rebuilds(self):
        """A folder smaller than the window is built once and left alone."""
        for index in range(120):
            self.assertFalse(viewport.needs_rebuild(index, (0, 120), total=120), index)

    def test_the_middle_of_a_window_does_not(self):
        self.assertFalse(viewport.needs_rebuild(100, (20, 181)))

    def test_nearing_either_edge_does(self):
        self.assertTrue(viewport.needs_rebuild(25, (20, 181)))
        self.assertTrue(viewport.needs_rebuild(175, (20, 181)))

    def test_walking_the_whole_queue_builds_few_rows_per_step(self):
        """What matters is amortised cost: rows rebuilt per navigation step.

        Rebuilding the filmstrip for the whole queue is what made every arrow
        key cost as much as opening the folder, so the window has to keep the
        churn near constant however long the queue is.
        """
        costs = []
        for total in (500, 5000, 50000):
            loaded = viewport.window(0, total, 80)
            built = loaded[1] - loaded[0]
            for index in range(total):
                if viewport.needs_rebuild(index, loaded, total=total):
                    loaded = viewport.window(index, total, 80)
                    built += loaded[1] - loaded[0]
            per_step = built / total
            costs.append((total, per_step))
            self.assertLess(per_step, 10.0, f"{total} items -> {per_step:.2f} rows/step")
        # The point is that the cost per step does not grow with the queue —
        # rebuilding the whole strip was what made a long queue unusable.
        self.assertLess(abs(costs[-1][1] - costs[0][1]), 1.0, costs)

    def test_the_loaded_window_always_holds_the_cursor(self):
        total = 5000
        loaded = viewport.window(0, total, 80)
        for index in range(total):
            if viewport.needs_rebuild(index, loaded, total=total):
                loaded = viewport.window(index, total, 80)
            self.assertTrue(loaded[0] <= index < loaded[1], index)

    def test_clamp(self):
        self.assertEqual(0, viewport.clamp_index(-5, 10))
        self.assertEqual(9, viewport.clamp_index(99, 10))
        self.assertEqual(0, viewport.clamp_index(3, 0))


if __name__ == "__main__":
    unittest.main()
