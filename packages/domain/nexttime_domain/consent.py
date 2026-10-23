class ConsentError(ValueError):
    """Raised when source processing has no valid consent basis."""


def require_processing_consent(
    *, consent_attested: bool, revoked: bool, retention_policy_present: bool
) -> None:
    if not consent_attested:
        raise ConsentError("Consent attestation is required before transcript processing")
    if revoked:
        raise ConsentError("Revoked consent stops future transcript processing")
    if not retention_policy_present:
        raise ConsentError("A retention policy is required before transcript processing")
