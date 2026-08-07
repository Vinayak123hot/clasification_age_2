####################################################################################################
# Project name      : Outlook Support Classification Agent -- Foundry v2 (ServiceNow-shaped KB)    #
# Business owner    : <fill: business owner / team>                                                #
# Notebook Author   : <fill: author name / team>                                                   #
# Date              : <fill: date>                                                                 #
#                                                                                                  #
# Purpose of file:                                                                                 #
# Correlation-id helper for the v2 classification service - one id per request/turn.               #
#   1. Exposes generate_correlation_id() returning a fresh UUID4 string.                           #
#   2. Threads that id through structured logs so one turn is traceable end to end.                #
#   3. Lets downstream calls (agent, KB selection) share the same correlation id.                  #
#                                                                                                  #
# Source:-                                                                                         #
#   - uuid (stdlib) supplies the UUID4 generator used to mint each correlation id.                 #
#       - uuid.uuid4:- returns a random 128-bit UUID, stringified to a 36-char id.                 #
####################################################################################################

# ============================================ Imports =============================================
from __future__ import annotations  # Enable postponed evaluation of type annotations (PEP 563)   # future import

import uuid  # Standard library module used to generate universally unique identifiers             # stdlib uuid


# ========================================= Correlation ids ========================================
def generate_correlation_id() -> str:  # Return a new correlation id string                        # id factory
    """Generate a new, unique correlation id for one request/turn.

    What this function is:
        - The single source of correlation ids for the v2 service: a thin wrapper over uuid4() that
          produces one opaque, collision-resistant string per request/turn.

    Why this exists:
        - So every log line and downstream call for a single turn can share one id, making a turn
          traceable end to end without leaking any request content into the identifier.

    Security and production notes:
        - UUID4 is random (not sequential), so the id reveals nothing about volume, ordering, or
          timing and is safe to surface in logs. It is an identifier only - never a secret or token.

    Args:
        None.

    Returns:
        A random UUID4 string used to correlate logs and downstream calls.

    Example:
        >>> cid = generate_correlation_id()  # doctest: +SKIP
        >>> isinstance(cid, str) and len(cid) == 36  # doctest: +SKIP
        True
    """
    return str(uuid.uuid4())  # Create a random UUID4 and return it as a string                    # mint id
