"""
Approval Gate Package - Distributed Valkey-backed HITL state machine.
"""
from .models import ApprovalRecord, ApprovalStatus
from .filter import evaluate_command_safety, normalize_command, HARDENED_DANGEROUS_PATTERNS
from .store import ValkeyApprovalStore, get_approval_store, ApprovalStoreMappingProxy

__all__ = [
    "ApprovalRecord",
    "ApprovalStatus",
    "evaluate_command_safety",
    "normalize_command",
    "HARDENED_DANGEROUS_PATTERNS",
    "ValkeyApprovalStore",
    "get_approval_store",
    "ApprovalStoreMappingProxy",
]
