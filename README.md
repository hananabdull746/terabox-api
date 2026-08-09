# TeraBox Resolver API

A small FastAPI service that accepts a TeraBox share link and returns the resolved media URL.

## Run locally

Use Python 3.11.

```bash
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```

## Endpoints

- GET /health
- GET /
- POST /api/resolve

## Deploy to Render

The included [render.yaml](render.yaml) config is prepared for Render deployment.

Recommended deployment notes:
- Use Python 3.11.9 in the Render environment.
- Keep the app listening on the Render-provided $PORT.
- Set ALLOWED_ORIGINS to your allowed frontend domains for better security.
