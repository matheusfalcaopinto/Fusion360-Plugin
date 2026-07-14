"""Fail-closed protected-payload boundary probe for the Fusion executor.

The package intentionally ships no Autodesk or desktop-control adapter.  A
real campaign must inject lifecycle and dispatch adapters explicitly.
"""

from .calibration import CalibratedScript, PayloadScriptCalibrator, ast_topology_sha256
from .loader import load_probe_matrix
from .models import (
    CanaryContract,
    CleanupReceipt,
    DispatchReceipt,
    DispatcherCapabilities,
    PayloadProbeMatrix,
    ProbeClassification,
    ProbeDispatchRequest,
    ProbeReadback,
    ProbeRunConfig,
    ProbeRunReport,
    ProbeTrialContext,
    ProbeTrialFixture,
    ProbeTrialResult,
)
from .runner import (
    PayloadProbeAbort,
    PayloadProbeConfigurationError,
    PayloadProbeRunner,
    classify_probe_observation,
)

__all__ = [
    "CalibratedScript",
    "CanaryContract",
    "CleanupReceipt",
    "DispatchReceipt",
    "DispatcherCapabilities",
    "PayloadProbeAbort",
    "PayloadProbeConfigurationError",
    "PayloadProbeMatrix",
    "PayloadProbeRunner",
    "PayloadScriptCalibrator",
    "ProbeClassification",
    "ProbeDispatchRequest",
    "ProbeReadback",
    "ProbeRunConfig",
    "ProbeRunReport",
    "ProbeTrialContext",
    "ProbeTrialFixture",
    "ProbeTrialResult",
    "ast_topology_sha256",
    "classify_probe_observation",
    "load_probe_matrix",
]
