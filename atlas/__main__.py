"""`python -m atlas` starts the dashboard on 127.0.0.1 (localhost only by default)."""
import os
import sys

import uvicorn


def main():
    host = os.getenv("ATLAS_HOST", "127.0.0.1")
    port = int(os.getenv("ATLAS_PORT", "8765"))
    if host not in ("127.0.0.1", "localhost", "::1") and os.getenv("ATLAS_ALLOW_REMOTE") != "1":
        print(f"Refusing to bind to {host}. ATLAS is localhost-only unless ATLAS_ALLOW_REMOTE=1 is set "
              "(not recommended: there is no authentication).", file=sys.stderr)
        sys.exit(2)
    print(f"ATLAS dashboard: http://{host}:{port}   (stop with Ctrl+C or ./stop.sh)")
    uvicorn.run("atlas.main:app", host=host, port=port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
