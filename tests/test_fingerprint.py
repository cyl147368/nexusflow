import random
import unittest

from fingerprint import generate_fingerprint
from auth_flow import _stable_firefox_fingerprint


class FingerprintTest(unittest.TestCase):
    def test_generator_can_restrict_to_stable_firefox_only(self):
        for seed in range(20):
            fingerprint = generate_fingerprint(
                rng=random.Random(seed),
                country_code="DE",
                browser_types=("firefox",),
            )
            self.assertEqual(fingerprint["browser_type"], "firefox")
            self.assertEqual(fingerprint["timezone"], "Europe/Berlin")

    def test_generator_rejects_unknown_only_selection(self):
        with self.assertRaises(ValueError):
            generate_fingerprint(browser_types=("unknown",))

    def test_auth_flow_uses_known_stable_firefox_profile(self):
        fingerprint = _stable_firefox_fingerprint(
            rng=random.Random(7),
            country_code="DE",
        )
        self.assertEqual(fingerprint["browser_type"], "firefox")
        self.assertEqual(fingerprint["impersonate"], "firefox144")


if __name__ == "__main__":
    unittest.main()
