"""M2 — Acquisition. See BUILD_SPEC.md §7.

Build AcquisitionOrder (pending_confirm) from approved domains. Send to provider
(backorder/optimizator) ONLY when confirmed_by_human is True. Track order status;
on success set Domain.status='purchased'.
"""


def create_order(domain_id: int, provider: str) -> int:
    """Create a pending_confirm order. TODO."""
    raise NotImplementedError


def execute_confirmed_order(order_id: int) -> dict:
    """Send a human-confirmed order to the provider. Enforce the gate. TODO."""
    raise NotImplementedError
