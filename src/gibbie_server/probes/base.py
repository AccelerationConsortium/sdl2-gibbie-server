"""The probe contract.

A probe answers one question, synchronously and within its own timeouts: what
did I just observe about this device? It never actuates. The monitor decides
what an observation means over time (debounce, outage spans); the envelope
builder decides how it reads on the wire.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar

from sdl_lab_contract import Activity, ComponentStatus, EquipmentState, ErrorInfo, MetricValue

from ..config import DeviceConfig


@dataclass
class Observation:
    #: Did the probe get an answer from the thing it targets? False means "could
    #: not reach it", never "it reported a fault" (§2.1).
    reachable: bool
    #: Meaningful only when reachable. The probe picks the §2.2-honest value.
    state: EquipmentState = "unknown"
    #: From observed state, never derived from `state` (§2.3).
    activity: Activity = "unknown"
    message: str = ""
    components: dict[str, ComponentStatus] = field(default_factory=dict)
    metrics: dict[str, MetricValue] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)
    last_error: ErrorInfo | None = None

    @classmethod
    def unreachable(cls, message: str, **details: Any) -> "Observation":
        return cls(reachable=False, state="unknown", activity="unknown", message=message, details=details)


class Probe:
    """Base class. Subclasses set `probe_type`, `primary_operation` and `target`."""

    #: Key used in config: `probe = "<probe_type>"`.
    probe_type: ClassVar[str] = ""
    #: What "running" means for this device (STATUS_SPEC §2.3 asks every device
    #: to say so). Surfaces in `details.primary_operation`.
    primary_operation: ClassVar[str] = ""

    def __init__(self, device_id: str, cfg: DeviceConfig) -> None:
        self.device_id = device_id
        self.cfg = cfg

    @property
    def target(self) -> str:
        """Human-readable address this probe talks to (for messages)."""
        return ""

    def probe(self) -> Observation:  # pragma: no cover - abstract
        raise NotImplementedError
