from unittest import TestCase

from cfdppy.handler.dest import LostSegmentTracker


class TestLostSegmentTracker(TestCase):
    def setUp(self) -> None:
        self.tracker = LostSegmentTracker()

    def test_basic(self):
        self.assertEqual(self.tracker.lost_segments, {})
        self.tracker.add_lost_segment((0, 500))
        seg_end = self.tracker.lost_segments[0]
        self.assertEqual(seg_end, 500)

    def test_coalesence_0(self):
        self.tracker.add_lost_segment((500, 1000))
        self.tracker.add_lost_segment((1000, 1500))
        self.tracker.coalesce_lost_segments()
        self.assertEqual(len(self.tracker.lost_segments), 1)
        seg_end = self.tracker.lost_segments[500]
        self.assertEqual(seg_end, 1500)

    def test_coalesence_1(self):
        self.tracker.add_lost_segment((500, 1000))
        self.tracker.add_lost_segment((1000, 1500))
        self.tracker.add_lost_segment((1500, 1700))
        self.tracker.coalesce_lost_segments()
        self.assertEqual(len(self.tracker.lost_segments), 1)
        seg_end = self.tracker.lost_segments[500]
        self.assertEqual(seg_end, 1700)

    def test_coalesence_2(self):
        self.tracker.add_lost_segment((500, 1000))
        self.tracker.add_lost_segment((1100, 1200))
        self.tracker.coalesce_lost_segments()
        self.assertEqual(len(self.tracker.lost_segments), 2)
        self.assertEqual(self.tracker.lost_segments, {500: 1000, 1100: 1200})

    def test_removal_0(self):
        self.tracker.add_lost_segment((0, 500))
        self.assertTrue(self.tracker.remove_lost_segment((0, 500)))
        self.assertEqual(self.tracker.lost_segments, {})

    def test_removal_1(self):
        self.tracker.add_lost_segment((0, 500))
        self.assertTrue(self.tracker.remove_lost_segment((0, 200)))
        self.assertEqual(self.tracker.lost_segments, {200: 500})

    def test_removal_2(self):
        self.tracker.add_lost_segment((0, 500))
        self.assertTrue(self.tracker.remove_lost_segment((300, 500)))
        self.assertEqual(self.tracker.lost_segments, {0: 300})

    def test_removal_3(self):
        self.tracker.add_lost_segment((0, 500))
        self.assertTrue(self.tracker.remove_lost_segment((300, 400)))
        self.assertEqual(self.tracker.lost_segments, {0: 300, 400: 500})

    def test_noop_removal_0(self):
        self.tracker.add_lost_segment((0, 500))
        self.assertFalse(self.tracker.remove_lost_segment((500, 1000)))
        self.assertEqual(self.tracker.lost_segments, {0: 500})

    def test_noop_removal_1(self):
        self.tracker.add_lost_segment((0, 500))
        self.assertFalse(self.tracker.remove_lost_segment((0, 0)))
        self.assertEqual(self.tracker.lost_segments, {0: 500})

    def test_noop_removal_2(self):
        self.tracker.add_lost_segment((0, 500))
        self.assertFalse(self.tracker.remove_lost_segment((500, 600)))
        self.assertEqual(self.tracker.lost_segments, {0: 500})

    def test_invalid_removal_0(self):
        self.tracker.add_lost_segment((0, 500))
        with self.assertRaises(ValueError):
            self.tracker.remove_lost_segment((0, 600))

    def test_invalid_removal_1(self):
        self.tracker.add_lost_segment((0, 500))
        with self.assertRaises(ValueError):
            self.tracker.remove_lost_segment((200, 600))
