import re
from datetime import date
from decimal import Decimal, localcontext

from .travel_estimates import get_travel_estimates


def calculate_trip_cost_exact(
    destination: str,
    departure_date: str,
    return_date: str,
    proposed_budget_aud: float,
    registration_fee_aud: float | None = None,
) -> dict:
    def error(code, field, message):
        return {"status": "error", "error": {"code": code, "field": field, "message": message}}

    if not isinstance(destination, str) or not destination.strip():
        return error("invalid_input", "destination", "Provide a non-empty destination string.")

    dates = {}
    for field, value in (("departure_date", departure_date), ("return_date", return_date)):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            return error("invalid_date", field, "Dates must use exactly YYYY-MM-DD.")
        try:
            dates[field] = date.fromisoformat(value)
        except ValueError:
            return error("invalid_date", field, "Provide a valid calendar date in YYYY-MM-DD format.")

    nights = (dates["return_date"] - dates["departure_date"]).days
    if nights < 0:
        return error("invalid_date_range", "return_date", "Return date must be on or after departure date.")

    money = {}
    for field, value in (("proposed_budget_aud", proposed_budget_aud), ("registration_fee_aud", registration_fee_aud)):
        if field == "registration_fee_aud" and value is None:
            money[field] = None
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            return error("invalid_money", field, "Provide a finite, non-negative number.")
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            return error("invalid_money", field, "Provide a finite, non-negative number.")
        money[field] = amount

    estimates = get_travel_estimates(destination)
    if estimates["status"] != "success":
        return error("unsupported_destination", "destination", estimates["message"])

    travel_days = nights + 1
    with localcontext() as context:
        context.prec = max(
            28,
            max(max(0, amount.adjusted() + 1) for amount in money.values() if amount is not None)
            + max(max(0, -amount.as_tuple().exponent) for amount in money.values() if amount is not None)
            + 16,
        )
        flight = Decimal(str(estimates["return_flight_aud"]))
        hotel_rate = Decimal(str(estimates["hotel_per_night_aud"]))
        meals_rate = Decimal(str(estimates["meals_per_day_aud"]))
        hotel = hotel_rate * nights
        meals = meals_rate * travel_days
        transport = Decimal(str(estimates["local_transport_aud"]))
        subtotal = flight + hotel + meals + transport
        registration = money["registration_fee_aud"]
        total = None if registration is None else subtotal + registration
        known_costs = subtotal if total is None else total
        budget = money["proposed_budget_aud"]
        remaining = budget - known_costs
        budget_status = "over_budget" if remaining < 0 else "incomplete" if registration is None else "within_budget"

    def json_money(value):
        return value

    return {
        "status": "success",
        "destination": estimates["destination"],
        "departure_date": departure_date,
        "return_date": return_date,
        "nights": nights,
        "travel_days": travel_days,
        "currency": estimates["currency"],
        "source": estimates["source"],
        "departure_city": estimates["departure_city"],
        "note": estimates["note"],
        "return_flight_aud": json_money(flight),
        "hotel_per_night_aud": json_money(hotel_rate),
        "hotel_total_aud": json_money(hotel),
        "meals_per_day_aud": json_money(meals_rate),
        "meals_total_aud": json_money(meals),
        "local_transport_aud": json_money(transport),
        "subtotal_excluding_registration_aud": json_money(subtotal),
        "registration_fee_aud": None if registration is None else json_money(registration),
        "complete_total_aud": None if total is None else json_money(total),
        "known_costs_aud": json_money(known_costs),
        "proposed_budget_aud": json_money(budget),
        "remaining_budget_aud": json_money(remaining),
        "budget_status": budget_status,
    }

def calculate_trip_cost(
    destination: str,
    departure_date: str,
    return_date: str,
    proposed_budget_aud: float,
    registration_fee_aud: float | None = None,
) -> dict:
    result = calculate_trip_cost_exact(
        destination, departure_date, return_date, proposed_budget_aud, registration_fee_aud
    )
    return {
        key: (int(value) if value == value.to_integral_value() else float(value))
        if isinstance(value, Decimal) else value
        for key, value in result.items()
    }
