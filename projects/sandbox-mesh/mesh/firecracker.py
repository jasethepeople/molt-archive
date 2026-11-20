from __future__ import annotations

from .driver import DriverError


class FirecrackerDriver:
    def ensure_ready(self) -> dict:
        raise DriverError("Firecracker is reserved for a future phase; Foundry uses Docker only")
