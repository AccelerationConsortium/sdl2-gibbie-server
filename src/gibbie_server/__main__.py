"""`gibbie-server --config config.toml` -- run the monitor + API.

`--once` polls every device a single time and prints the envelopes as JSON,
for checking a config on the bench without starting the server.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

import uvicorn

from .api import create_app
from .config import load_config
from .envelope import device_envelope
from .monitor import Monitor


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="gibbie-server")
    p.add_argument("--config", default="config.toml")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--once", action="store_true", help="probe every device once, print envelopes, exit")
    p.add_argument("--log-level", default="info")
    args = p.parse_args(argv)

    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    config = load_config(args.config)

    if args.once:
        mon = Monitor(config)
        mon.poll_once()
        out = {rec.device_id: device_envelope(rec, mon).model_dump(mode="json") for rec in mon.records.values()}
        json.dump(out, sys.stdout, indent=2)
        print()
        return 0

    app = create_app(config)
    uvicorn.run(app, host=args.host or config.service.host, port=args.port or config.service.port, log_level=args.log_level)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
