"""Entry point: `gunicorn wsgi:app` in production, `python wsgi.py` locally."""
import os

from app import create_app

app = create_app()

if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
