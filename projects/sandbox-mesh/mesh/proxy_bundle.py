from __future__ import annotations

import threading

from egress_proxy import create_server as create_egress_server
from ingress_proxy import create_server as create_ingress_server


def main() -> None:
    servers = (create_egress_server(), create_ingress_server())
    threads = [threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True) for server in servers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


if __name__ == "__main__":
    main()
