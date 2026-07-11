"""Emit the app's OpenAPI spec as JSON to stdout.

Pure import + serialize: no uvicorn, no network, no DB.

Usage: ``uv run python openapi_export.py``
"""

import json

from main import app


def main() -> None:
    print(json.dumps(app.openapi()))


if __name__ == "__main__":
    main()
