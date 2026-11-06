import unittest

from travel_agent.tools.travel_estimates import get_travel_estimates


class TravelEstimatesTests(unittest.TestCase):
    def test_supported_destinations(self):
        expected_prices = {
            "Sydney": (350, 250, 70, 100),
            "Brisbane": (400, 200, 65, 90),
        }
        for city, amounts in expected_prices.items():
            with self.subTest(destination=city):
                self.assertEqual(
                    get_travel_estimates(city),
                    {
                        "destination": city,
                        "currency": "AUD",
                        "status": "success",
                        "source": "synthetic_training_data",
                        "departure_city": "Melbourne",
                        "note": "Flight prices assume departure from Melbourne.",
                        "return_flight_aud": amounts[0],
                        "hotel_per_night_aud": amounts[1],
                        "meals_per_day_aud": amounts[2],
                        "local_transport_aud": amounts[3],
                    },
                )

    def test_case_and_surrounding_whitespace(self):
        for destination, city in [("  sYdNeY\t", "Sydney"), ("\nBRISBANE  ", "Brisbane")]:
            with self.subTest(destination=destination):
                self.assertEqual(
                    get_travel_estimates(destination), get_travel_estimates(city)
                )

    def test_unsupported_and_empty_destinations_have_no_prices(self):
        for destination in ["Tokyo", "  Tokyo  ", "", " \t\n"]:
            with self.subTest(destination=destination):
                result = get_travel_estimates(destination)
                self.assertEqual(result["status"], "no_data")
                self.assertEqual(result["destination"], destination.strip())
                self.assertTrue(result["message"])
                self.assertEqual(set(result), {"destination", "status", "message"})

    def test_results_do_not_mutate_fixtures(self):
        result = get_travel_estimates("Sydney")
        result["return_flight_aud"] = 0
        self.assertEqual(get_travel_estimates("Sydney")["return_flight_aud"], 350)


if __name__ == "__main__":
    unittest.main()
