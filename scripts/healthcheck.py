from __future__ import annotations

import argparse
import json
import sys
from urllib import error, request


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Call /api/health and exit nonzero if the backend is unavailable.",
    )
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8000",
        help="Backend base URL. Default: http://127.0.0.1:8000",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="Request timeout in seconds. Default: 5.0",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    url = f"{args.base_url.rstrip('/')}/api/health"
    try:
        with request.urlopen(url, timeout=args.timeout) as response:
            body = response.read().decode("utf-8")
            payload = json.loads(body)
            status_code = response.getcode()
    except error.HTTPError as exc:
        print(f"healthcheck failed: http_status={exc.code} url={url}", file=sys.stderr)
        return 1
    except error.URLError as exc:
        print(f"healthcheck failed: url={url} reason={exc.reason}", file=sys.stderr)
        return 1
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"healthcheck failed: invalid json from {url}: {exc}", file=sys.stderr)
        return 1

    if status_code != 200:
        print(f"healthcheck failed: http_status={status_code} url={url}", file=sys.stderr)
        return 1

    database_status = payload.get("database", {}).get("status")
    print(
        "healthcheck ok: "
        f"app={payload.get('app')} env={payload.get('env')} "
        f"date={payload.get('date')} database_status={database_status}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
