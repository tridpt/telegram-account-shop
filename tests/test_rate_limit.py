import unittest

from rate_limit import UpdateLimiter


class RateLimitTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.limiter = UpdateLimiter(clock=lambda: self.now)

    def test_one_user_is_limited_and_warning_is_not_spammed(self):
        for _ in range(10):
            self.assertEqual(self.limiter.check_update(1001), (True, False))
        self.assertEqual(self.limiter.check_update(1001), (False, True))
        self.assertEqual(self.limiter.check_update(1001), (False, False))
        self.now = 2.0
        self.assertEqual(self.limiter.check_update(1001), (True, False))
        self.assertEqual(self.limiter.check_update(1001), (False, False))
        self.now = 32.0
        for _ in range(10):
            self.assertEqual(self.limiter.check_update(1001), (True, False))
        self.assertEqual(self.limiter.check_update(1001), (False, True))

    def test_global_limit_prevents_many_accounts_from_overloading_bot(self):
        for user_id in range(1, 41):
            self.assertEqual(self.limiter.check_update(user_id), (True, False))
        self.assertEqual(self.limiter.check_update(41), (False, False))
        self.assertEqual(self.limiter.check_update(999, is_admin=True), (True, False))
        self.now = 0.1
        self.assertEqual(self.limiter.check_update(41), (True, False))

    def test_topup_has_per_user_and_global_cooldown(self):
        self.assertTrue(self.limiter.allow_topup(1))
        self.assertFalse(self.limiter.allow_topup(1))
        self.assertTrue(self.limiter.allow_topup(2))
        self.assertTrue(self.limiter.allow_topup(3))
        self.assertFalse(self.limiter.allow_topup(4))
        self.now = 10.0
        self.assertTrue(self.limiter.allow_topup(4))
        self.assertFalse(self.limiter.allow_topup(1))
        self.now = 20.0
        self.assertTrue(self.limiter.allow_topup(1))


if __name__ == "__main__":
    unittest.main()
