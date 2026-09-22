web: python -m db_bootstrap; uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
# Scheduled work in its own process, so a long job cannot take the API down.
# Run as a SECOND service on the same image; it needs the same environment.
worker: python -m worker
