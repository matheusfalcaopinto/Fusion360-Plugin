"""Programmatic verification package."""

from verifier.geometry import GeometryVerifier
from verifier.result_models import FailureCode, VerificationIssue, VerificationResult

__all__ = ["FailureCode", "GeometryVerifier", "VerificationIssue", "VerificationResult"]
