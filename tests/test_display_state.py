import unittest
from unittest.mock import patch
from visualizer.state import DisplayState, Mode


class ScreenTransitionTests(unittest.TestCase):
    def test_startup_idle_gets_full_timeout(self):
        state = DisplayState(100)
        state.tick(100, 0)
        self.assertEqual(state.mode, Mode.IDLE)
        state.tick(129.99, 0)
        self.assertEqual(state.mode, Mode.IDLE)
        state.tick(130, 0)
        self.assertEqual(state.mode, Mode.PHOTOS)

    def test_playback_start_only_switches_idle(self):
        for mode, expected in [(Mode.IDLE, Mode.SPECTRUM), (Mode.SPECTRUM, Mode.SPECTRUM),
                               (Mode.PHOTOS, Mode.PHOTOS)]:
            state = DisplayState(100, mode=mode)
            state.tick(101, 101)
            self.assertEqual(state.mode, expected)

    def test_no_playback_times_out_spectrum_but_music_keeps_it_visible(self):
        state = DisplayState(100, mode=Mode.SPECTRUM)
        state.tick(129, 125)
        self.assertEqual(state.mode, Mode.SPECTRUM)
        state.tick(154.99, 125)
        self.assertEqual(state.mode, Mode.SPECTRUM)
        state.tick(155, 125)
        self.assertEqual(state.mode, Mode.PHOTOS)

    def test_ok_cycles_and_manual_idle_stays_during_existing_stream_then_times_out(self):
        state = DisplayState(100)
        state.tick(100, 100)
        self.assertEqual(state.mode, Mode.SPECTRUM)
        state.cycle(101)
        self.assertEqual(state.mode, Mode.PHOTOS)
        state.cycle(102)
        self.assertEqual(state.mode, Mode.IDLE)
        state.tick(103, 103)
        self.assertEqual(state.mode, Mode.IDLE)
        state.tick(132, 132)
        self.assertEqual(state.mode, Mode.PHOTOS)
        state.cycle(133)
        state.tick(134, 132)  # detect silence before a fresh playback start
        state.tick(135, 135)
        self.assertEqual(state.mode, Mode.SPECTRUM)

    def test_manual_spectrum_does_not_immediately_timeout_from_old_silence(self):
        state = DisplayState(100)
        state.cycle(101)
        state.tick(101, 0)
        self.assertEqual(state.mode, Mode.SPECTRUM)
        state.tick(131, 0)
        self.assertEqual(state.mode, Mode.PHOTOS)

    def test_shuffle_shows_every_photo_once_before_reshuffling(self):
        orders = iter(([2, 0, 3, 1], [1, 3, 0, 2]))

        def shuffle(order):
            order[:] = next(orders)

        state = DisplayState(0)
        with patch("visualizer.state.random.shuffle", side_effect=shuffle) as shuffled, \
                patch("visualizer.state.random.randrange", return_value=2):
            state.set_photos(4, 0)
            first = []
            for index in range(4):
                first.append(state.ordered_photos(["a", "b", "c", "d"])[state.photo_index])
                if index < 3:
                    state.advance_photo((index + 1) * 30)
            self.assertEqual(shuffled.call_count, 1)
            state.advance_photo(120)
            second = []
            for index in range(4):
                second.append(state.ordered_photos(["a", "b", "c", "d"])[state.photo_index])
                if index < 3:
                    state.advance_photo(150 + index * 30)
            self.assertEqual(shuffled.call_count, 2)
        self.assertCountEqual(first, ["a", "b", "c", "d"])
        self.assertCountEqual(second, first)
        self.assertNotEqual(first, second)
        self.assertNotEqual(first[-1], second[0])

    def test_shuffle_handles_empty_single_and_replaced_selections(self):
        state = DisplayState(0)
        state.set_photos(0, 0)
        self.assertEqual(state.ordered_photos([]), ())
        self.assertFalse(state.advance_photo(30))
        state.set_photos(1, 0)
        for now in (30, 60, 90):
            self.assertTrue(state.advance_photo(now))
            self.assertEqual(state.ordered_photos(["only"]), ("only",))
            self.assertEqual(state.photo_index, 0)
        state.set_photos(3, 100)
        state.photo_button("right", 101)
        previous = state.photo_order[state.photo_index]
        state.photo_button("left", 102)
        state.photo_button("right", 103)
        self.assertEqual(state.photo_order[state.photo_index], previous)
        state.set_photos(2, 200)
        self.assertEqual(state.photo_index, 0)
        self.assertCountEqual(state.photo_order, [0, 1])

    def test_photo_navigation_wraps_and_resets_timer(self):
        state = DisplayState(0, mode=Mode.PHOTOS)
        state.set_photos(3, 0)
        state.photo_button("left", 5)
        self.assertEqual(state.photo_index, 2)
        self.assertEqual(state.next_photo_at, 35)
        state.photo_button("right", 6)
        self.assertEqual(state.photo_index, 0)
        self.assertFalse(state.advance_photo(35.99))
        self.assertTrue(state.advance_photo(36))
        self.assertEqual(state.photo_index, 1)

    def test_interval_label_only_appears_after_adjustment_and_expires(self):
        state = DisplayState(0)
        self.assertIsNone(state.interval_info(0))
        state.photo_button("right", 1)
        self.assertIsNone(state.interval_info(1))
        state.photo_button("up", 10)
        self.assertEqual(state.interval_info(11.99), "Interval: 35 s")
        self.assertIsNone(state.interval_info(12))
        state.photo_button("down", 11)
        self.assertEqual(state.interval_info(12.99), "Interval: 30 s")
        self.assertIsNone(state.interval_info(13))

    def test_interval_increments_minimum_and_timer_reset(self):
        state = DisplayState(0)
        state.photo_button("up", 1)
        self.assertEqual(state.interval, 35)
        self.assertEqual(state.next_photo_at, 36)
        for _ in range(10):
            state.photo_button("down", 2)
        self.assertEqual(state.interval, 5)
        self.assertEqual(state.next_photo_at, 7)
        state.set_photos(0, 3)
        state.photo_button("right", 4)
        self.assertEqual(state.photo_index, 0)
        self.assertFalse(state.advance_photo(500))


if __name__ == "__main__":
    unittest.main()
