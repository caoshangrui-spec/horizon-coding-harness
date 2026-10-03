"""Customer invoice rendering with audit integration notes.

The audit stream receives an audit event after rendering. Audit retention and
audit export are handled by the sibling subsystem, not by this module.
"""


def render_invoice(order):
    """Render the customer-facing invoice for an order."""

    return f"order:{order['id']}"
