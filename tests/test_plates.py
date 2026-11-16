import unittest

from markedup.plates import TYPE_LABELS, VEHICLE_TYPES, display_plate, normalize_plate


class NormalizePlateTest(unittest.TestCase):
    def test_strips_spaces_dashes_and_case(self):
        for raw in ("abc1234", "ABC 1234", "abc-1234", " Abc_1234 ", "A B C 1 2 3 4"):
            self.assertEqual(normalize_plate(raw), "ABC1234", raw)

    def test_rejects_unusable_values(self):
        for raw in (None, 42, "", "A", "   ", "ABCDEFGHIJK", "!!!", "a" * 11):
            self.assertIsNone(normalize_plate(raw), repr(raw))

    def test_keeps_short_and_numeric_plates(self):
        self.assertEqual(normalize_plate("12"), "12")
        self.assertEqual(normalize_plate("1234 abc"), "1234ABC")


class DisplayPlateTest(unittest.TestCase):
    def test_splits_letters_and_digits(self):
        self.assertEqual(display_plate("ABC1234"), "ABC 1234")
        self.assertEqual(display_plate("1234ABC"), "1234 ABC")

    def test_leaves_mixed_plates_alone(self):
        self.assertEqual(display_plate("AB12CD"), "AB12CD")
        self.assertEqual(display_plate("12"), "12")


class VehicleTypesTest(unittest.TestCase):
    def test_pickup_is_first_and_ids_are_unique(self):
        self.assertEqual(VEHICLE_TYPES[0][0], "pickup")
        ids = [tid for tid, _ in VEHICLE_TYPES]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(TYPE_LABELS), len(ids))


if __name__ == "__main__":
    unittest.main()
