"""Pure opportunity discovery primitives.

This package deliberately stops before portfolio sizing and broker execution.
"""

from .engine import OpportunityEngine
from .models import OpportunityCandidate, OpportunityContext, OpportunitySession

__all__ = ["OpportunityCandidate", "OpportunityContext", "OpportunityEngine", "OpportunitySession"]
