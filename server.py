"""Run the AsaSakyan server.

    python server.py                 # http://127.0.0.1:8080
    python server.py --port 9000
    HOST=0.0.0.0 PORT=8080 python server.py   # how the Dockerfile runs it
"""

import argparse
import os
import sys

from asasakyan import __version__
from asasakyan.api import Api
from asasakyan.db import Database
from asasakyan.http import create_server


def main(argv=None):
    parser = argparse.ArgumentParser(description="AsaSakyan: find your own vehicle by its plate.")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    parser.add_argument("--db", default=os.environ.get("ASASAKYAN_DB", os.path.join("data", "asasakyan.sqlite3")))
    args = parser.parse_args(argv)

    folder = os.path.dirname(os.path.abspath(args.db))
    os.makedirs(folder, exist_ok=True)

    server = create_server(args.host, args.port, Api(Database(args.db)))
    host, port = server.server_address[:2]
    print("AsaSakyan %s listening on http://%s:%d  (database: %s)" % (__version__, host, port, args.db))
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
