import logging
import os
from contextlib import asynccontextmanager

from .tracing import configure_tracing
from .session_config import session_database_kwargs, session_database_url


def create_app(agents_dir="/app"):
    session_url = session_database_url()
    provider = configure_tracing()

    from google.adk.cli.fast_api import get_fast_api_app

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            provider.force_flush(timeout_millis=5000)
            provider.shutdown()

    app = get_fast_api_app(
        agents_dir=agents_dir,
        host="0.0.0.0",
        bind_host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        web=False,
        reload_agents=False,
        session_service_uri=session_url,
        session_db_kwargs=session_database_kwargs(),
        artifact_service_uri="memory://",
        max_llm_calls=5,
        extra_plugins=["travel_agent.observability.ObservabilityPlugin"],
        trace_to_cloud=False,
        otel_to_cloud=False,
        lifespan=lifespan,
    )

    from fastapi.responses import JSONResponse
    from sqlalchemy.exc import SQLAlchemyError
    from .request_context import PreparationRequestMiddleware, preparation_request, shared_scope
    from .db.session import create_session_factory
    from .services.proposals import recover

    app.add_middleware(PreparationRequestMiddleware, request_scope=shared_scope())

    @app.get("/proposal-preparations/recovery")
    def recover_preparation():
        request = preparation_request.get()
        headers = {"Cache-Control": "no-store"}
        if request is None:
            return JSONResponse({"error": "missing_idempotency_key"}, status_code=400, headers=headers)
        factory = None
        try:
            factory = create_session_factory()
            result = recover(factory, request)
            return JSONResponse(result if result is not None else {"error": "not_found"},
                                status_code=200 if result is not None else 404, headers=headers)
        except (ValueError, SQLAlchemyError):
            return JSONResponse({"error": "proposal_storage_error"}, status_code=503, headers=headers)
        finally:
            if factory is not None:
                factory.kw["bind"].dispose()

    return app


def main():
    import uvicorn
    from google.adk.cli.utils.logs import setup_adk_logger

    setup_adk_logger(logging.INFO)
    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), reload=False)


if __name__ == "__main__":
    main()
