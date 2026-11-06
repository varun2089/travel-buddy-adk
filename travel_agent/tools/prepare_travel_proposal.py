def prepare_travel_proposal(
    destination: str,
    departure_date: str,
    return_date: str,
    purpose: str,
    proposed_budget_aud: float,
    registration_fee_aud: float | None = None,
) -> dict:
    from sqlalchemy.exc import SQLAlchemyError
    from travel_agent.callbacks import tool_error
    from travel_agent.db.session import create_session_factory
    from travel_agent.services.proposals import prepare

    from travel_agent.request_context import preparation_request

    request = preparation_request.get()
    if request is None:
        return tool_error("missing_idempotency_key", "Supply an Idempotency-Key for preparation.")
    factory = None
    try:
        try:
            factory = create_session_factory()
        except ValueError:
            return tool_error(
                "database_configuration_error",
                "Proposal storage is not configured correctly. Do not retry automatically.",
            )
        return prepare(factory, destination, departure_date, return_date, purpose,
                       proposed_budget_aud, registration_fee_aud, request=request)
    except SQLAlchemyError:
        return tool_error(
            "proposal_storage_error",
            "Proposal storage could not be confirmed. Do not retry automatically.",
        )
    finally:
        if factory is not None:
            factory.kw["bind"].dispose()
