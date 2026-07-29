from __future__ import annotations

from enum import Enum


class ExitAuthority(str, Enum):
    PIS = "PIS"
    HARD_RISK = "HARD_RISK"
    RECONCILIATION = "RECONCILIATION"
    EOD = "EOD"
    OPERATOR = "OPERATOR"


# These reasons may close without a PIS discretionary EXIT because they are
# safety/operational mandates, not alpha-taking policy.
NON_DISCRETIONARY_AUTHORITIES = frozenset(
    {
        ExitAuthority.HARD_RISK,
        ExitAuthority.RECONCILIATION,
        ExitAuthority.EOD,
        ExitAuthority.OPERATOR,
    }
)


class ExitAuthorityViolation(RuntimeError):
    pass


def assert_close_authorized(*, authority: ExitAuthority, pis_requests_exit: bool = False) -> None:
    """Fail closed if any legacy discretionary subsystem attempts to sell.

    The engine migration should call this immediately before discretionary
    broker close submission. A PIS close needs an explicit confirmed decision;
    operational/safety authorities remain independently valid.
    """
    if authority in NON_DISCRETIONARY_AUTHORITIES:
        return
    if authority is ExitAuthority.PIS and pis_requests_exit:
        return
    raise ExitAuthorityViolation(
        f"close rejected: authority={authority.value}; CR-290 requires confirmed PIS EXIT"
    )
