from __future__ import annotations

import math
import pathlib
from dataclasses import dataclass


@dataclass(frozen=True)
class HostDecision:
    allowed: bool
    reason: str | None
    load_1m: float
    load_limit: float
    available_memory_bytes: int
    required_memory_bytes: int


class HostGuard:
    def __init__(self, config: dict, cpu_count: int, loadavg_path: pathlib.Path = pathlib.Path("/proc/loadavg"), meminfo_path: pathlib.Path = pathlib.Path("/proc/meminfo")):
        self.config = config
        self.cpu_count = max(1, int(cpu_count))
        self.loadavg_path = loadavg_path
        self.meminfo_path = meminfo_path

    def measure(self) -> tuple[float, int]:
        try:
            load_fields = self.loadavg_path.read_text(encoding="ascii").split()
            load_1m = float(load_fields[0])
            if not math.isfinite(load_1m) or load_1m < 0:
                raise ValueError("invalid load average")
        except (OSError, UnicodeError, ValueError, IndexError) as exc:
            raise RuntimeError("host load average could not be measured; keeping the job queued") from exc
        try:
            values = {}
            for line in self.meminfo_path.read_text(encoding="ascii").splitlines():
                key, separator, raw = line.partition(":")
                if separator:
                    fields = raw.split()
                    if fields:
                        values[key] = int(fields[0]) * (1024 if len(fields) > 1 and fields[1] == "kB" else 1)
            available = values.get("MemAvailable")
            if available is None or available < 0:
                raise ValueError("MemAvailable missing")
        except (OSError, UnicodeError, ValueError) as exc:
            raise RuntimeError("available host memory could not be measured; keeping the job queued") from exc
        return load_1m, available

    def evaluate(self) -> HostDecision:
        load_1m, available = self.measure()
        load_limit = self.cpu_count * float(self.config["max_1m_load_per_cpu"])
        required = int(self.config.get("min_available_memory_bytes", self.config.get("minimum_available_memory_bytes", 0)))
        if required < 1:
            raise RuntimeError("host guard minimum memory threshold is invalid; keeping the job queued")
        if load_1m > load_limit:
            return HostDecision(False, "waiting for host capacity", load_1m, load_limit, available, required)
        if available < required:
            return HostDecision(False, "waiting for host capacity", load_1m, load_limit, available, required)
        return HostDecision(True, None, load_1m, load_limit, available, required)
