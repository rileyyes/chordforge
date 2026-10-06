import logging
import signal
import sys

from .config import Config
from .server import App, create_server


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = Config.from_env()
    app = App(cfg)
    srv = create_server(app)
    logging.getLogger("chordforge").info("ChordForge listening on http://%s:%s", cfg.host, cfg.port)
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
