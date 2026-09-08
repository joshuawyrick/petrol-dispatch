# Petrol Dispatch Optimizer

Phase 1: master data (locations, lanes, yards), settings, and the road-mileage cache.

Run: `sh run.sh` (installs requirements, seeds the database on first start, serves on port 8000).

Secrets: `GOOGLE_MAPS_API_KEY` (Routes API). Optional: `DATABASE_URL` for PostgreSQL; otherwise data/dispatch.db (SQLite).
