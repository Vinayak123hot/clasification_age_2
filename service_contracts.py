####################################################################################################
# Project name      : Outlook Support Classification Agent -- Foundry v2 (ServiceNow-shaped KB)    #
# Business owner    : <fill: business owner / team>                                                #
# Notebook Author   : <fill: author name / team>                                                   #
# Date              : <fill: date>                                                                 #
#                                                                                                  #
# Purpose of file:                                                                                 #
# Pydantic service contracts for the v2 classification service - the AGENT / HTTP boundary only.   #
#   1. AgentEntryRequest  - the JSON body the HTTP entry point accepts ({conv_id?, message}).      #
#   2. AgentStructuredOutput - the strict per-turn JSON the Foundry agent emits (internal shape).  #
#   3. AgentEntryResponse - the JSON body the HTTP entry point returns to the calling application. #
#   (KB-index / KB-search models are intentionally absent: v2 handles KB records as raw dicts.)    #
#                                                                                                  #
# Source:-                                                                                         #
#   - from __future__ import annotations enables postponed evaluation of type annotations.         #
#       - annotations:- lets forward references in field types resolve lazily (PEP 563).           #
#   - from typing import Literal, Optional supplies the field typing helpers.                      #
#       - Literal:- pins the status field to a fixed set of allowed string values.                 #
#       - Optional:- marks fields that may be null (conv_id, query, kb_id, summary).               #
#   - from pydantic import BaseModel is the base class for every boundary model here.              #
#       - BaseModel:- validates / serialises the request, agent-output and response shapes.        #
####################################################################################################

# ============================================ Imports =============================================
from __future__ import annotations  # Enable postponed evaluation of annotations (PEP 563)          # future import

from typing import Literal, Optional  # Typing helpers for fixed choices and optional values        # typing helpers

from pydantic import BaseModel  # Pydantic base model for validation / serialisation                # pydantic base


# ================================= Agent / HTTP boundary models ===================================
class AgentEntryRequest(BaseModel):  # The JSON input accepted by the HTTP entry point
    """The JSON input accepted by the combined agent Function.

    A conversation is identified by `conv_id` — the Foundry conversation id. Omit it
    on the first turn (the function creates a conversation and returns its id as
    conv_id); pass it back on later turns so Foundry supplies the history. No external
    database is used — the Foundry conversation IS the state.

    What this model is:
        - The request contract at the HTTP boundary: exactly the two fields the calling
          application sends per turn ({conv_id?, message}).

    Why this exists:
        - To validate the inbound body once, at the edge, so the turn service always
          receives a well-typed {conv_id?, message} instead of a raw, untrusted dict.

    Security and production notes:
        1. `message` is untrusted user text — never interpolate it into prompts / queries
           without the downstream guards; treat it as data, not instructions.
        2. Omitting `conv_id` starts a NEW Foundry conversation; passing a foreign id
           could read another caller's history, so the calling application owns the id.

    Example:
        {"message": "Outlook keeps crashing on send"}                 # first turn
        {"conv_id": "conv_abc123", "message": "the desktop app"}      # later turn
    """

    conv_id: Optional[str] = None  # Foundry conversation id; omit on the first turn                # conversation id
    message: str  # The user's message for this turn                                                # user message


class AgentStructuredOutput(BaseModel):  # The strict JSON the Foundry agent emits per turn
    """The strict JSON object the Foundry agent returns as its message each turn.

    The agent's system prompt requires exactly these fields. 'search' is an INTERNAL
    status: the agent asks THIS function to run a KB search and feed the candidates
    back; it is never returned to the caller. The other statuses are surfaced.

    What this model is:
        - The per-turn output contract of the Foundry agent — the machine-readable JSON
          the turn service parses to decide whether to search, ask, resolve or hand off.

    Why this exists:
        - To force the agent's free-form reply into a strict, validated shape so the
          service can branch on `status` deterministically instead of scraping prose.

    Security and production notes:
        1. `status == "search"` and its `query` are INTERNAL — never surface either to
           the user; only follow_up / resolved / no_match are shown.
        2. `agent_message` must NEVER mention a KB/article; `kb_id` is an internal
           routing key, not user-facing — enforce this when mapping to the response.

    Example:
        {"status": "search", "query": "outlook desktop crashes on send",
         "agent_message": "", "kb_id": null, "summary": null, "chat_close": false}
        {"status": "resolved", "agent_message": "Thank you for the details — I'm working on this now; please bear with me.",
         "kb_id": "KB-00012", "summary": "Outlook crashes on send.", "chat_close": true}
    """

    status: Literal["search", "follow_up", "resolved", "no_match"]  # search (run a KB search), asking, resolved, or handoff  # turn status
    agent_message: str  # Human-facing text; NEVER mentions a KB/article. Resolved -> plain thank-you; no_match -> names the user's task + handoff. Empty for 'search'  # user text
    query: Optional[str] = None  # For status 'search': the KB search description this function should run  # internal query
    kb_id: Optional[str] = None  # Resolved article id (None unless status == 'resolved'); internal routing key, not shown to the user  # routing key
    summary: Optional[str] = None  # Resolved OR no_match: the USER'S own issue, lightly polished (spelling/grammar) — never the KB/article text or invented; else None  # user issue
    chat_close: bool = False  # True when resolved OR no_match (conversation ends); False otherwise  # end flag


class AgentEntryResponse(BaseModel):  # The JSON output returned by the HTTP entry point
    """The JSON output returned by the combined agent Function.

    `conv_id` is the Foundry conversation id — the stable conversation handle AND the
    key the logs are stored under: store it and pass it back as `conv_id` on the next
    turn to continue the same conversation.

    What this model is:
        - The response contract at the HTTP boundary: the caller-facing subset of the
          agent's outcome, with the internal 'search' status already resolved away.

    Why this exists:
        - To give the calling application one stable, validated JSON shape per turn and
          to keep internal signals (raw search turns, KB text) out of the wire response.

    Security and production notes:
        1. `agent_message` NEVER mentions a KB/article; on error it carries a safe
           fallback string only — never a stack trace or exception detail.
        2. `kb_id` is an internal routing key (not shown to the user) and is always None
           for no_match; `summary` is the user's own issue, never the KB/article text.

    Example:
        {"conv_id": "conv_abc123", "status": "resolved",
         "agent_message": "Thank you for the details — I'm working on this now; please bear with me.", "kb_id": "KB-00012",
         "summary": "Outlook crashes on send.", "chat_close": true}
    """

    conv_id: Optional[str] = None  # Foundry conversation id — echo back next turn; also the log key  # conversation id
    status: Literal["follow_up", "resolved", "no_match", "error"]  # follow-up, resolved, no KB found (handoff), or safe error  # response status
    agent_message: str  # Human-facing message; NEVER mentions a KB/article (resolved -> thank-you; no_match -> names the user's task). Safe fallback on error  # user text
    kb_id: Optional[str] = None  # Resolved article id (internal routing key, not shown to the user); None until resolved (always None for no_match)  # routing key
    summary: Optional[str] = None  # Resolved OR no_match: the USER'S own issue, lightly polished (spelling/grammar); never the KB/article text. Otherwise None  # user issue
    chat_close: bool = False  # Whether the conversation has ended (defaults to False)              # end flag
