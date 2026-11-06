import json
import subprocess
import sys
import unittest

from travel_agent.tools.calculate_trip_cost import calculate_trip_cost


class CalculateTripCostTests(unittest.TestCase):
    def calculate(self, **changes):
        inputs = dict(destination="Sydney", departure_date="2026-11-16", return_date="2026-11-18", proposed_budget_aud=1500)
        inputs.update(changes)
        return calculate_trip_cost(**inputs)

    def test_sydney_unknown_registration(self):
        result = self.calculate()
        self.assertEqual(result["nights"], 2)
        self.assertEqual(result["travel_days"], 3)
        self.assertEqual(result["return_flight_aud"], 350)
        self.assertEqual(result["hotel_total_aud"], 500)
        self.assertEqual(result["meals_total_aud"], 210)
        self.assertEqual(result["local_transport_aud"], 100)
        self.assertEqual(result["subtotal_excluding_registration_aud"], 1160)
        self.assertIsNone(result["registration_fee_aud"])
        self.assertIsNone(result["complete_total_aud"])
        self.assertEqual(result["remaining_budget_aud"], 340)
        self.assertEqual(result["budget_status"], "incomplete")
        self.assertEqual(result["source"], "synthetic_training_data")
        self.assertEqual(result["currency"], "AUD")
        self.assertEqual(result["departure_city"], "Melbourne")
        json.dumps(result, allow_nan=False)

    def test_known_registration(self):
        result = self.calculate(registration_fee_aud=200)
        self.assertEqual(result["complete_total_aud"], 1360)
        self.assertEqual(result["remaining_budget_aud"], 140)
        self.assertEqual(result["budget_status"], "within_budget")

    def test_zero_registration_and_exact_budget(self):
        result = self.calculate(registration_fee_aud=0, proposed_budget_aud=1160)
        self.assertEqual(result["complete_total_aud"], 1160)
        self.assertEqual(result["registration_fee_aud"], 0)
        self.assertEqual(result["budget_status"], "within_budget")

    def test_same_day(self):
        result = self.calculate(return_date="2026-11-16", registration_fee_aud=0)
        self.assertEqual(result["nights"], 0)
        self.assertEqual(result["travel_days"], 1)
        self.assertEqual(result["hotel_total_aud"], 0)
        self.assertEqual(result["meals_total_aud"], 70)
        self.assertEqual(result["complete_total_aud"], 520)

    def test_over_budget(self):
        for registration in (None, 200):
            with self.subTest(registration=registration):
                result = self.calculate(proposed_budget_aud=1000, registration_fee_aud=registration)
                self.assertEqual(result["budget_status"], "over_budget")
                self.assertLess(result["remaining_budget_aud"], 0)

    def test_invalid_dates(self):
        for field in ("departure_date", "return_date"):
            for value in ("2026-1-16", "16-11-2026", "2026-02-30", "2026-11-16T00:00:00", "2026-11-16\n", None, "0000-01-01"):
                with self.subTest(field=field, value=value):
                    result = self.calculate(**{field: value})
                    self.assertEqual(result["status"], "error")
                    self.assertEqual(result["error"]["code"], "invalid_date")
                    self.assertEqual(result["error"]["field"], field)

    def test_reversed_dates(self):
        self.assertEqual(self.calculate(return_date="2026-11-15")["error"]["code"], "invalid_date_range")

    def test_invalid_money(self):
        for field in ("proposed_budget_aud", "registration_fee_aud"):
            for value in (-1, float("nan"), float("inf"), float("-inf"), True, "200"):
                with self.subTest(field=field, value=value):
                    result = self.calculate(**{field: value})
                    self.assertEqual(result["error"]["code"], "invalid_money")
                    self.assertEqual(result["error"]["field"], field)
        self.assertEqual(self.calculate(proposed_budget_aud=None)["error"]["code"], "invalid_money")

    def test_decimal_arithmetic(self):
        result = self.calculate(registration_fee_aud=0.1, proposed_budget_aud=1160.3)
        self.assertEqual(result["remaining_budget_aud"], 0.2)
        json.dumps(result, allow_nan=False)

    def test_unsupported_destination(self):
        result = self.calculate(destination="Tokyo")
        self.assertEqual(result["error"]["code"], "unsupported_destination")
        self.assertNotIn("complete_total_aud", result)

    def test_reuses_destination_estimates(self):
        result = self.calculate(destination="  brISbane ", registration_fee_aud=0)
        self.assertEqual(result["destination"], "Brisbane")
        self.assertEqual(result["complete_total_aud"], 1085)

    def test_tools_import_without_adk(self):
        code = "import sys; from travel_agent.tools.calculate_trip_cost import calculate_trip_cost; assert 'travel_agent.agent' not in sys.modules; assert 'google.adk' not in sys.modules"
        subprocess.run([sys.executable, "-c", code], check=True)


if __name__ == "__main__":
    unittest.main()
