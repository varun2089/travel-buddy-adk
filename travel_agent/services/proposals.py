from decimal import Decimal
from datetime import date
import re

from sqlalchemy.exc import IntegrityError
from travel_agent.callbacks import tool_error
from travel_agent.request_context import PreparationRequest
from uuid import UUID, uuid4

from travel_agent.db.repositories import add_audit, add_proposal, get_proposal, find_proposal_request
from travel_agent.tools.calculate_trip_cost import calculate_trip_cost_exact


def decimal_strings(values):
    return {key: str(value) if isinstance(value, Decimal) else value for key, value in values.items()}


def snapshot(proposal, review):
    return {
        "proposal_id": str(proposal.id),
        "status": review.status,
        "inputs": proposal.inputs,
        "results": proposal.results,
        "confirmation": review.confirmation,
        "submission_mode": "simulated",
    }


def normalize_inputs(values):
    normalized = {"version": 1}
    destination = values["destination"]
    purpose = values["purpose"]
    if not isinstance(destination, str) or not destination.strip():
        raise ValueError
    if not isinstance(purpose, str) or not purpose.strip():
        raise ValueError
    if destination.strip().casefold() not in {"sydney", "brisbane"}:
        raise ValueError
    normalized.update(destination=destination.strip().casefold(), purpose=purpose.strip())
    for field in ("departure_date", "return_date"):
        value = values[field]
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            raise ValueError
        normalized[field] = date.fromisoformat(value).isoformat()
    if normalized["return_date"] < normalized["departure_date"]:
        raise ValueError
    for field in ("proposed_budget_aud", "registration_fee_aud"):
        value = values[field]
        if field == "registration_fee_aud" and value is None:
            normalized[field] = None
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            raise ValueError
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            raise ValueError
        text = format(amount, "f")
        normalized[field] = (text.rstrip("0").rstrip(".") if "." in text else text) if amount else "0"
    return normalized


def replay(existing, normalized):
    proposal, review = existing
    if proposal.normalized_inputs != normalized:
        return tool_error("idempotency_key_conflict", "This request key was used with different inputs.")
    return snapshot(proposal, review)


def recover(session_factory, request):
    with session_factory.begin() as session:
        existing = find_proposal_request(session, request)
        return None if existing is None else snapshot(*existing)


def prepare(session_factory, destination, departure_date, return_date, purpose,
            proposed_budget_aud, registration_fee_aud=None, *, request=None):
    if not isinstance(request, PreparationRequest):
        return tool_error("missing_idempotency_key", "Supply an Idempotency-Key for preparation.")
    values = dict(destination=destination, departure_date=departure_date, return_date=return_date,
                  purpose=purpose, proposed_budget_aud=proposed_budget_aud,
                  registration_fee_aud=registration_fee_aud)
    try:
        normalized = normalize_inputs(values)
    except (ValueError, TypeError, ArithmeticError):
        if not isinstance(purpose, str) or not purpose.strip():
            return {"status": "error", "error": {"code": "invalid_input", "field": "purpose",
                    "message": "Provide a non-empty travel purpose."}}
        return calculate_trip_cost_exact(destination, departure_date, return_date,
                                         proposed_budget_aud, registration_fee_aud)
    with session_factory.begin() as session:
        existing = find_proposal_request(session, request)
        if existing is not None:
            return replay(existing, normalized)
    result = calculate_trip_cost_exact(
        destination, departure_date, return_date, proposed_budget_aud, registration_fee_aud
    )
    if result["status"] == "error":
        return result
    inputs = dict(destination=destination, departure_date=departure_date,
                  return_date=return_date, purpose=purpose,
                  proposed_budget_aud=str(Decimal(str(proposed_budget_aud))),
                  registration_fee_aud=None if registration_fee_aud is None
                  else str(Decimal(str(registration_fee_aud))))
    try:
        with session_factory.begin() as session:
            proposal, review = add_proposal(session, inputs, decimal_strings(result), request, normalized)
            saved = snapshot(proposal, review)
        return saved
    except IntegrityError as error:
        if (getattr(error.orig, "sqlstate", None) != "23505"
                or getattr(getattr(error.orig, "diag", None), "constraint_name", None)
                != "proposals_request_unique"):
            raise
        with session_factory.begin() as session:
            existing = find_proposal_request(session, request)
            if existing is None:
                raise error
            return replay(existing, normalized)


def show(session_factory, proposal_id):
    with session_factory.begin() as session:
        return snapshot(*get_proposal(session, UUID(str(proposal_id))))


def review_proposal(session_factory, proposal_id, action, reviewer, confirmed=False):
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ValueError("A reviewer identifier is required; identity is unauthenticated at this stage.")
    if not confirmed:
        raise ValueError("Explicit confirmation is required.")
    if action not in {"approve", "reject"}:
        raise ValueError("Choose approve or reject.")
    with session_factory.begin() as session:
        proposal, review = get_proposal(session, UUID(str(proposal_id)), lock=True)
        if action == "approve" and review.status == "SUBMITTED":
            return snapshot(proposal, review)
        if review.status != "NEEDS_REVIEW":
            raise ValueError("Conflicting transition: proposal has already been reviewed.")
        if action == "approve":
            if (proposal.results["registration_fee_aud"] is None
                    or proposal.results["budget_status"] != "within_budget"):
                raise ValueError("Submission blocked: registration is unknown or the proposed budget is exceeded.")
            review.status = "SUBMITTED"
            review.confirmation = "MOCK-" + str(uuid4())
        else:
            review.status = "REJECTED"
        add_audit(session, proposal.id, reviewer, action, review.confirmation)
        return snapshot(proposal, review)
