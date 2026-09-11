import unittest

from src.retry_policy import retry_delay


class RetryDelayTests(unittest.TestCase):
    def test_first_attempt_uses_base(self):
        self.assertEqual(retry_delay(1), 2)

    def test_second_attempt_doubles_once(self):
        self.assertEqual(retry_delay(2), 4)

    def test_custom_base(self):
        self.assertEqual(retry_delay(3, base_seconds=3), 12)

    def test_cap(self):
        self.assertEqual(retry_delay(8, base_seconds=2, cap_seconds=30), 30)

    def test_invalid_attempt(self):
        with self.assertRaises(ValueError):
            retry_delay(0)

    def test_invalid_base(self):
        with self.assertRaises(ValueError):
            retry_delay(1, base_seconds=0)

    def test_invalid_cap(self):
        with self.assertRaises(ValueError):
            retry_delay(1, cap_seconds=0)


if __name__ == "__main__":
    unittest.main()
