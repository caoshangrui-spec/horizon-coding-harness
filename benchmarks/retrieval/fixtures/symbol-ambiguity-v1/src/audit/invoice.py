"""Audit invoice rendering with order integration notes.

Orders produce order events that are converted into immutable order records.
The order payload is retained for order-history inspection.
"""


def render_invoice(event):
    """Render an immutable invoice entry for the audit log."""

    return f"audit:{event['id']}"
