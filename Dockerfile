# Local-dev image. Includes dev/test tooling (pytest, mypy, ruff) so the
# same container can run the app, the test suite, and type-checking via
# `docker-compose exec app ...` — see CONTRIBUTING.md.
#
# NOTE for production: this image is NOT the lean production build. When
# extending the existing Hetzner VPS compose setup, build a separate
# multi-stage production image that drops dev dependencies and --reload
# (see README "Deployment" section — flagged as follow-up work).
FROM python:3.12-slim

# Runtime system libraries required by weasyprint (PDF rendering) at
# import/run time. Deliberately no build-essential/-dev headers: the
# Python deps this app uses (asyncpg, argon2-cffi, weasyprint's cffi/cairo
# bindings) ship prebuilt wheels for this platform, so nothing needs to be
# compiled here.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    libcairo2 \
    libffi8 \
    shared-mime-info \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir -e ".[dev]"

COPY . .
RUN chmod +x docker/entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["docker/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
