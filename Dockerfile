FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TRAVEL_TRACE_EXPORT=false \
    ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=false \
    OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=NO_CONTENT \
    ADK_TELEMETRY_IGNORE_RUN_CONFIG=true \
    ADK_DISABLE_LOAD_DOTENV=true

WORKDIR /app

COPY pyproject.toml ./
COPY travel_agent/ ./travel_agent/

RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 app

USER app

EXPOSE 8080

CMD ["python", "-m", "travel_agent.server"]
