"""
Payment provider seam — the thing that *decides* a payment is good or bad.
Every provider hands its decision to billing.payment_service, which is the
only thing allowed to touch the database (activation, audit log,
notifications, coupon/referral side-effects). This keeps "how was this
payment verified" swappable without ever touching product-activation logic.

ManualQRProvider is the only implementation today — an admin manually
reviewing a screenshot IS the verification. A future FonepayProvider/
KhaltiProvider/EsewaProvider/ConnectIPSProvider/BankEPGProvider would each
verify a payment via their own API/webhook signature, then call
payment_service.activate()/.reject() exactly the same way — no other code in
this app would need to change.
"""
from . import payment_service


class ManualQRProvider:
    """Verification is a human admin's decision, made from the Payment
    Verification dashboard."""

    @staticmethod
    def approve(purchase_id, *, actor, request=None):
        return payment_service.activate(purchase_id, actor=actor, request=request)

    @staticmethod
    def reject(purchase_id, reason, *, actor, request=None):
        return payment_service.reject(purchase_id, reason, actor=actor, request=request)
