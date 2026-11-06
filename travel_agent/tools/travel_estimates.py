
TRAVEL_FIXTURES = {
    "Sydney": {
        "return_flight_aud": 350,
        "hotel_per_night_aud": 250,
        "meals_per_day_aud": 70,
        "local_transport_aud": 100,
    },
    "Brisbane": {
        "return_flight_aud": 400,
        "hotel_per_night_aud": 200,
        "meals_per_day_aud": 65,
        "local_transport_aud": 90,
    },
}


def get_travel_estimates(destination: str) -> dict:

    cleaned_destination = destination.strip()

    for city, prices in TRAVEL_FIXTURES.items():
        if cleaned_destination.casefold() == city.casefold():
            return {
                "destination": city,
                "currency": "AUD",
                "status": "success",
                "source": "synthetic_training_data",
                "departure_city": "Melbourne",
                "note": "Flight prices assume departure from Melbourne.",
                **prices,
            }

    return {
        "destination": cleaned_destination,
        "status": "no_data",
        "message": (
            "No synthetic travel estimates are available for this destination. "
            "Supported destinations are Sydney and Brisbane."
            if cleaned_destination
            else "Destination is empty. Please provide Sydney or Brisbane."
        ),
    }
