"""Regression checks for the operator's manual origin/destination route labels."""

import unittest

from src.route_identity import canonicalize_manual_route_id


class ManualRouteIdentityTests(unittest.TestCase):
    def test_matches_existing_route_ignoring_case_and_whitespace(self):
        route = canonicalize_manual_route_id("  sattur   →   CHENNAI ", {"Sattur → Chennai"})
        self.assertEqual(route, "Sattur → Chennai")

    def test_new_custom_route_is_normalized_without_inventing_a_match(self):
        route = canonicalize_manual_route_id("  My   Town → New   Stop ", set())
        self.assertEqual(route, "My Town → New Stop")

    def test_non_pair_route_ids_are_only_whitespace_normalized(self):
        route = canonicalize_manual_route_id("  Route   10 ", {"R-10"})
        self.assertEqual(route, "Route 10")


if __name__ == "__main__":
    unittest.main()

