####################################################################################################
# Project name      : Outlook Support Classification Agent -- Foundry v2 (ServiceNow-shaped KB)    #
# Business owner    : <fill: business owner / team>                                                #
# Notebook Author   : <fill: author name / team>                                                   #
# Date              : <fill: date>                                                                 #
#                                                                                                  #
# Purpose of file:                                                                                 #
# Coordinate ONE classification turn - run the Foundry agent over a stable conversation and, on a  #
# 'search' request, hand it the KB entirely IN-PROCESS (no HTTP loopback, no re-ranker).           #
#   1. Validate the {conv_id?, message} boundary and start / continue the Foundry conversation.    #
#   2. On agent status 'search', feed the ENTIRE ServiceNow-shaped KB back so the AGENT selects.   #
#   3. Guard resolutions against fabricated kb_ids and shape a safe response for every outcome.    #
#                                                                                                  #
# Source:-                                                                                         #
#   - runtime_config supplies load_settings / resolve_path (merged env config + index path).       #
#   - correlation_ids supplies generate_correlation_id (bootstrap log id before a conv exists).    #
#   - model_cost_meter supplies CostTracker (turns response token usage into a logged cost).       #
#   - telemetry_logging supplies EventHubLogEmitter / LogFactory / StructuredLogger.               #
#   - service_contracts supplies AgentEntryRequest / AgentEntryResponse / AgentStructuredOutput.   #
#   - foundry_agent_client supplies FoundryAgentGateway + FoundryAgentError.                       #
#   - servicenow_kb_source supplies KnowledgeBaseSource + KnowledgeBaseSourceError (whole-KB feed) #
####################################################################################################

# ============================================ Imports =============================================
from __future__ import annotations  # Enable postponed evaluation of type annotations (PEP 563)      # future import

import json  # Parse the agent's strict-JSON reply and serialise KB candidates                       # stdlib json
import os  # Read the APP_ENV environment variable selecting the config file                         # stdlib os
import re  # Strip accidental markdown code fences from the agent's reply                            # stdlib re
import threading  # Lock guarding one-time service construction across worker threads                # stdlib threading
from typing import Any, Optional  # Generic type hints for JSON dicts and optional values            # stdlib typing

from pydantic import ValidationError  # Raised when a boundary payload fails schema validation        # boundary error

from runtime_config import load_settings, resolve_path  # Load the merged config + resolve the index path  # config loader
from correlation_ids import generate_correlation_id  # Helper to mint a bootstrap log id before a conversation exists  # log id
from model_cost_meter import CostTracker  # Turns response token usage into a logged cost figure      # cost meter
from telemetry_logging import EventHubLogEmitter, LogFactory, StructuredLogger  # Logging: emitter, factory, logger type  # telemetry
from service_contracts import AgentEntryRequest, AgentEntryResponse, AgentStructuredOutput  # HTTP + agent boundary models  # contracts
from foundry_agent_client import FoundryAgentGateway, FoundryAgentError  # Instrumented Foundry gateway + its error  # foundry client
from servicenow_kb_source import KnowledgeBaseSource, KnowledgeBaseSourceError  # In-process whole-KB source + its error  # kb source

# Environment variable that selects which config_<env>.yaml to load.
_ENVIRONMENT_VARIABLE_NAME = "APP_ENV"  # Name of the env var controlling config selection            # env var name
_DEFAULT_ENVIRONMENT_NAME = "poc"  # Fallback environment name when APP_ENV is unset                  # default env

# Strips accidental ```json ... ``` fences from the agent's reply.
_JSON_FENCE_PATTERN = re.compile(r"^```(?:json)?|```$", re.IGNORECASE | re.MULTILINE)  # Opening/closing fence matcher  # fence regex

# Safe, human-facing fallbacks.
_SAFE_FALLBACK_MESSAGE = (  # Generic apology shown to the user on any internal failure               # safe fallback
    "Sorry, something went wrong while processing your request. Please try again in a moment."
)
_NO_MATCH_FALLBACK_MESSAGE = (  # Shown when the search loop is exhausted without a decision          # no_match fallback
    "I'm sorry, I couldn't find the right article for this. I've logged it and one of our "
    "team members will reach out to you."
)

# Process-wide singleton so services + clients are built once per worker.
_CACHED_TURN_SERVICE: "ClassificationTurnService | None" = None  # Lazily-built service, cached for the worker's life  # cache slot
_TURN_SERVICE_LOCK = threading.Lock()  # Guards lazy construction so concurrent cold requests build only once  # build lock


# ========================================== Turn service ==========================================
class ClassificationTurnService:  # Coordinates one turn against the Foundry agent (in-process KB feed)  # turn service
    """Drive one turn of the Outlook Support Classification Agent (combined deployment).

    What this class is:
        The agent cannot call tools directly, so THIS service supplies the KB IN-PROCESS
        (no HTTP): each call to the agent goes through the Responses API (agent_reference),
        and conversation state is held server-side by a Foundry conversation whose id is
        the caller's stable conv_id. When the agent replies with status "search" and a
        query, this service feeds the ENTIRE ServiceNow-shaped KB back into the same
        conversation and lets the AGENT select; it repeats until the agent returns
        follow_up / resolved / no_match, or the search budget is hit.

    Why it exists (v2 change from v1):
        v1 ran an LLM re-ranker to shortlist candidates before handing them to the agent.
        v2 REMOVES that re-ranker entirely: on a "search" request it feeds the WHOLE KB
        back and the agent does the selection. This drops a model hop (latency + cost) and
        removes a second place a wrong article could be introduced.

    Security and production notes:
        1. Any failure yields a safe error response - no exception ever propagates to the caller.
        2. A resolved kb_id is accepted ONLY if it is a REAL entry in the index (fabricated ids
           are rejected); the whole index is validated, not just this turn's candidates.
        3. Logs are keyed by conv_id so a turn can be traced end-to-end.
    """

    def __init__(  # Wire the service's collaborators                                                # constructor
        self,
        foundry_agent_gateway: FoundryAgentGateway,  # Instrumented Foundry gateway (conversation + responses)  # gateway
        knowledge_source: KnowledgeBaseSource,  # In-process whole-KB source (feeds every candidate to the agent)  # kb source
        agent_name: str,  # Configured agent name (used for a config guard)                          # agent name
        auto_create_session: bool,  # True: start a conversation if conv_id is absent; False: require conv_id  # auto session
        max_search_rounds: int,  # Max KB searches per turn before forcing a no_match handoff        # search budget
        logger: StructuredLogger,  # Structured logger for turn events                               # logger
    ) -> None:
        """Create the turn service.

        What this does:
            Stores the collaborators the turn loop needs (gateway, whole-KB source, config
            guards, and the logger). There is NO re-ranker and NO per-search candidate count
            in v2 - the agent receives the entire KB on a search request and selects itself.

        Args:
            foundry_agent_gateway: The instrumented Foundry gateway.
            knowledge_source: The in-process KB source (KnowledgeBaseSource) that returns
                the entire candidate set for the agent to select from.
            agent_name: The configured agent name (guarded before running).
            auto_create_session: If True, start a conversation when conv_id is absent.
            max_search_rounds: Max KB searches per turn before a no_match handoff.
            logger: A structured logger for turn events.

        Returns:
            None.

        Example:
            >>> ClassificationTurnService(gateway, kb_source, "clasification-agent", True, 6, logger)  # doctest: +SKIP
        """
        self._foundry_agent_gateway = foundry_agent_gateway  # Store the Foundry gateway              # keep gateway
        self._knowledge_source = knowledge_source  # Store the in-process whole-KB source             # keep kb source
        self._agent_name = agent_name  # Store the agent name (config guard)                          # keep agent name
        self._auto_create_session = auto_create_session  # Store the new-conversation toggle          # keep auto session
        self._max_search_rounds = max_search_rounds  # Store the per-turn search budget               # keep budget
        self._logger = logger  # Store the structured logger                                          # keep logger

    # ------------------------------------------ Public API ------------------------------------------
    def handle_turn_json(self, payload: dict[str, Any]) -> dict[str, Any]:  # JSON boundary for the HTTP host  # json boundary
        """Handle one turn from a raw JSON body and return a JSON-serialisable dict.

        Args:
            payload: The decoded request body (optional conv_id + required message).

        Returns:
            A JSON-serialisable dict (see AgentEntryResponse), including conv_id.

        Raises:
            pydantic.ValidationError: If the body is missing 'message' or malformed
                (mapped to HTTP 400 by the caller).

        Example:
            >>> service.handle_turn_json({"message": "outlook crashes"})  # doctest: +SKIP
            {'conv_id': 'conv_abc', 'status': 'follow_up', ...}
        """
        request = AgentEntryRequest.model_validate(payload)  # Parse/validate the raw body into the request model  # validate body
        return self.handle_turn(request).model_dump()  # Process the turn and serialise the response to a dict  # run + dump

    def handle_turn(self, request: AgentEntryRequest) -> AgentEntryResponse:  # Core per-turn entry point  # core entry
        """Process one user turn against the Foundry agent (with in-process whole-KB feed).

        Args:
            request: The validated turn input (optional conv_id + message).

        Returns:
            An AgentEntryResponse describing a follow-up, resolution, no_match, or error.

        Example:
            >>> service.handle_turn(AgentEntryRequest(message="hi"))  # doctest: +SKIP
            AgentEntryResponse(conv_id='conv_abc', status='follow_up', ...)
        """
        # conv_id (when supplied) is the STABLE Foundry conversation id that holds history.
        conversation_id: Optional[str] = request.conv_id  # Foundry conversation id (None on the first turn)  # conv id
        turn_log_id = conversation_id or generate_correlation_id()  # Log key = conv_id, or a bootstrap id  # log key

        # Guard: the agent must be configured before any turn can run.
        if not self._agent_name:  # No agent name means the deployment is misconfigured               # config guard
            self._logger.log(  # Log the misconfiguration                                             # log misconfig
                event="foundry_agent_not_configured",  # Event name                                   # event
                correlation_id=turn_log_id,  # Log key                                                # log key
                level="ERROR",  # Severity level                                                      # level
            )
            return self._build_error_response(conversation_id)  # Safe error response                 # safe error

        try:  # Guard the whole turn so no exception escapes to the caller                            # turn guard
            if conversation_id:  # A conv_id was supplied -> continue that conversation               # continue conv
                self._logger.log(event="foundry_conversation_continued", correlation_id=conversation_id)  # Log continuation  # log continue
            elif self._auto_create_session:  # First turn, auto-start ON -> create a fresh conversation  # auto start
                conversation_id = self._foundry_agent_gateway.create_conversation(turn_log_id)  # Create it (logged under bootstrap id)  # create conv
                self._logger.log(  # Bridge the bootstrap id to the new conv_id                       # log start
                    event="foundry_conversation_started",  # Event name                               # event
                    correlation_id=conversation_id,  # From now on the log key is conv_id             # log key
                    bootstrap_id=turn_log_id,  # The id the pre-conversation logs used                # bootstrap id
                )
                turn_log_id = conversation_id  # Switch the log key to conv_id                         # switch log key
            else:  # First turn, auto-start OFF -> the calling application must supply conv_id         # require conv
                self._logger.log(  # Log the rejection                                                # log reject
                    event="foundry_conv_id_required",  # Event name                                   # event
                    correlation_id=turn_log_id,  # Log key (bootstrap; no conv_id yet)                # log key
                    level="ERROR",  # Severity level                                                  # level
                )
                return self._build_error_response(  # Reject: conv_id is mandatory in this mode        # reject
                    None,  # No conversation id to echo                                               # no conv id
                    "No conversation id (conv_id) was provided. The calling application must supply the conv_id.",  # Message  # message
                )

            self._logger.log(event="foundry_turn_start", correlation_id=turn_log_id)  # Turn start (keyed by conv_id)  # log start
            return self._run_turn_loop(conversation_id, request.message, turn_log_id)  # Run the agent + search loop  # run loop
        except (FoundryAgentError, KnowledgeBaseSourceError) as known_error:  # Expected failure modes -> safe fallback  # known error
            self._logger.log(  # Log the known failure                                                # log known
                event="foundry_turn_failed",  # Event name                                            # event
                correlation_id=turn_log_id,  # Log key                                                # log key
                level="ERROR",  # Severity level                                                      # level
                error_type=type(known_error).__name__,  # Exception class name                        # error type
                error_message=str(known_error),  # Exception message                                  # error msg
            )
            return self._build_error_response(conversation_id)  # Safe error response                 # safe error
        except Exception as unexpected_error:  # Any unexpected failure -> safe fallback (never leak)  # catch-all
            self._logger.log(  # Log the unexpected failure                                           # log unexpected
                event="foundry_turn_error",  # Event name                                             # event
                correlation_id=turn_log_id,  # Log key                                                # log key
                level="ERROR",  # Severity level                                                      # level
                error_type=type(unexpected_error).__name__,  # Exception class name                   # error type
                error_message=str(unexpected_error),  # Exception message                             # error msg
            )
            return self._build_error_response(conversation_id)  # Safe error response                 # safe error

    # ------------------------------------------ Search loop -----------------------------------------
    def _run_turn_loop(  # Agent + in-process whole-KB feed loop within a stable conversation         # search loop
        self, conversation_id: Optional[str], message: str, turn_log_id: str  # Conversation id, user text, log key  # loop args
    ) -> AgentEntryResponse:
        """Run the agent, feeding the WHOLE KB on any search request, until a terminal reply.

        What this does (v2):
            On an agent 'search' request this feeds the ENTIRE KB back (no re-ranker) and lets
            the AGENT choose; it loops until follow_up / resolved / no_match or the budget hits.

        Args:
            conversation_id: The stable Foundry conversation id carrying history.
            message: The user's message for this turn.
            turn_log_id: The correlation id used as the log key for this turn.

        Returns:
            An AgentEntryResponse for a follow-up, resolution, or no_match.

        Example:
            >>> service._run_turn_loop("conv_abc", "outlook crashes", "cid")  # doctest: +SKIP
            AgentEntryResponse(status='resolved', ...)
        """
        searches_done = 0  # How many KB searches we have run this turn                                # search count
        input_text = message  # First input is the user's message; later inputs are fed-back candidates / nudges  # next input
        conv_id = conversation_id  # The STABLE conversation id echoed back to the caller (same every turn)  # echo id

        # Hard iteration backstop = search budget + a little headroom for decide/nudge turns.
        for _iteration_index in range(self._max_search_rounds + 3):  # Bounded loop (never infinite)  # bounded loop
            reply_text = self._foundry_agent_gateway.create_response(  # Ask the agent within the conversation  # call agent
                input_text,  # The user message, fed-back candidates, or a nudge                      # input
                conversation_id,  # The Foundry conversation carrying history server-side (stable)    # conv id
                turn_log_id,  # Correlation id for logs                                               # log key
            )
            structured_output = self._parse_agent_output(reply_text, turn_log_id)  # Parse its strict-JSON reply  # parse reply

            if structured_output is None:  # Unparseable reply -> treat as a follow-up (safe, non-leaking)  # parse fail
                self._logger.log(event="foundry_reply_parse_fallback", correlation_id=turn_log_id, level="WARNING")  # Log it  # log fallback
                cleaned_text = _JSON_FENCE_PATTERN.sub("", reply_text).strip()  # Best-effort clean of the raw text  # clean text
                return self._follow_up_response(conv_id, cleaned_text or "Could you tell me a bit more?")  # Follow-up  # follow-up

            if structured_output.status == "search":  # The agent wants a KB search                   # search branch
                if not structured_output.query:  # Malformed search (no query) -> fall back to a follow-up  # no query
                    self._logger.log(event="foundry_search_without_query", correlation_id=turn_log_id, level="WARNING")  # Log  # log no query
                    return self._follow_up_response(  # Keep the conversation open                    # follow-up
                        conv_id, structured_output.agent_message or "Could you tell me a bit more?"
                    )
                if searches_done >= self._max_search_rounds:  # Search budget exhausted -> nudge the agent to decide  # budget hit
                    self._logger.log(event="foundry_search_budget_reached", correlation_id=turn_log_id, level="WARNING")  # Log  # log budget
                    input_text = (  # Instruct the agent to conclude with what it already has         # nudge text
                        "You have reached the maximum number of KB searches. Based on the candidates you already "
                        "have, either resolve with a kb_id or return no_match with a short summary of the user's issue."
                    )
                    continue  # Loop once more so the agent can produce a terminal reply              # loop again
                candidates = self._knowledge_source.get_all_candidates(turn_log_id)  # feed the ENTIRE KB (no re-ranker)  # whole kb
                search_payload = {"results": candidates}  # Wrap the whole-KB list in the results envelope the formatter expects  # envelope
                searches_done += 1  # Count this search against the budget                            # count search
                self._logger.log(  # Log the search we performed                                      # log search
                    event="foundry_kb_search_performed",  # Event name                                # event
                    correlation_id=turn_log_id,  # Log key                                            # log key
                    round=searches_done,  # Which search round this was                               # round
                    result_count=len(candidates),  # How many candidates we fed back (the whole KB)   # result count
                )
                input_text = self._format_candidates(structured_output.query, search_payload)  # Feed candidates back  # feed back
                continue  # Loop: send the candidates to the agent to decide                          # loop again

            # Anti-hallucination guard: a 'resolved' MUST use a REAL kb_id from the index. Validating
            # against the whole index (not just this turn's results) allows valid resolutions that
            # reuse a candidate from an earlier turn's search (held in the conversation history), while
            # still rejecting fabricated ids that do not exist in the knowledge base.
            if structured_output.status == "resolved" and (  # The agent claims a resolution...       # resolved guard
                not structured_output.kb_id  # ...with no id...                                       # missing id
                or not self._knowledge_source.is_known_kb_id(structured_output.kb_id)  # ...or a fabricated (non-index) id  # unknown id
            ):
                self._logger.log(  # Log the fabricated / unverified kb_id (never trust it)           # log hallucination
                    event="foundry_hallucinated_kb_id",  # Event name                                 # event
                    correlation_id=turn_log_id,  # Log key                                            # log key
                    level="WARNING",  # Severity level                                                # level
                    kb_id=structured_output.kb_id,  # The id the agent tried to resolve with          # bad id
                )
                if searches_done < self._max_search_rounds:  # Budget remains -> force the agent back to real articles  # budget left
                    input_text = (  # Nudge: only real ids from the knowledge base are allowed        # nudge text
                        "That kb_id is not a real article in the knowledge base. You may ONLY resolve with a kb_id "
                        "that appeared in the KB_SEARCH_RESULTS. Re-read the candidates and resolve with one of "
                        "their kb_ids, or return no_match with a short summary of the user's issue."
                    )
                    continue  # Loop again so the agent can correct itself                            # loop again
                return AgentEntryResponse(  # Budget exhausted -> safe no_match (never emit a fabricated id)  # safe no_match
                    conv_id=conv_id,  # Echo the conversation id                                      # echo id
                    status="no_match",  # Hand off instead of returning a fake article                # status
                    agent_message=_NO_MATCH_FALLBACK_MESSAGE,  # Polite handoff text                  # message
                    kb_id=None,  # No article (the claimed id was not real)                           # no article
                    summary=structured_output.summary,  # Keep any issue summary the agent produced   # summary
                    chat_close=True,  # End the conversation                                          # close chat
                )

            # Terminal reply: follow_up / resolved / no_match.
            return self._build_response_from_output(structured_output, conv_id)  # Map to a response   # terminal reply

        # Backstop exhausted without a terminal reply -> safe no_match handoff.
        self._logger.log(event="foundry_turn_loop_exhausted", correlation_id=turn_log_id, level="WARNING")  # Log it  # log exhausted
        return AgentEntryResponse(  # Close with a human-handoff no_match                              # backstop no_match
            conv_id=conv_id,  # Echo the conversation id                                              # echo id
            status="no_match",  # No article found within the budget                                  # status
            agent_message=_NO_MATCH_FALLBACK_MESSAGE,  # Polite handoff text                          # message
            kb_id=None,  # No article                                                                # no article
            summary=None,  # No issue summary available in this backstop path                         # no summary
            chat_close=True,  # End the conversation                                                  # close chat
        )

    # ------------------------------ Reply parsing / response building -------------------------------
    def _format_candidates(self, query: str, search_payload: dict[str, Any]) -> str:  # Render candidates for the agent  # render candidates
        """Render the KB candidates as the next input message for the agent.

        What this does (v2):
            Serialises the WHOLE-KB candidate list (under search_payload["results"]) into the
            KB_SEARCH_RESULTS block the prompt tells the agent to expect, so the AGENT selects.

        Args:
            query: The search description that produced these candidates.
            search_payload: The candidate envelope (has a 'results' list = the whole KB).

        Returns:
            A text block containing the candidate articles as JSON.

        Example:
            >>> service._format_candidates("outlook crashes", {"results": []})  # doctest: +SKIP
            'KB_SEARCH_RESULTS for query "outlook crashes" ... []'
        """
        results = search_payload.get("results", []) or []  # Extract the candidate list (default empty)  # get results
        return (  # Build a clear text block the prompt tells the agent to expect                     # build block
            f'KB_SEARCH_RESULTS for query "{query}" '
            f"(JSON list of candidate articles; decide follow_up / resolved / no_match from these):\n"
            f"{json.dumps(results, ensure_ascii=False)}"
        )

    def _build_response_from_output(  # Map a terminal AgentStructuredOutput to an AgentEntryResponse  # map terminal
        self, structured_output: AgentStructuredOutput, conv_id: Optional[str]  # Parsed terminal output + conversation id  # map args
    ) -> AgentEntryResponse:
        """Map a terminal agent reply (follow_up/resolved/no_match) to the response.

        Args:
            structured_output: The parsed terminal agent output.
            conv_id: The conversation id (echoed + log key).

        Returns:
            A validated AgentEntryResponse.

        Example:
            >>> service._build_response_from_output(out, "conv_abc")  # doctest: +SKIP
            AgentEntryResponse(status='resolved', ...)
        """
        if structured_output.status == "resolved":  # Valid resolution with a kb_id (already guarded upstream)  # resolved
            self._logger.log(  # Log the resolution                                                   # log resolved
                event="foundry_kb_article_returned",  # Event name                                    # event
                correlation_id=conv_id,  # Log key                                                    # log key
                kb_id=structured_output.kb_id,  # The resolved article id                             # kb id
            )
            return AgentEntryResponse(  # Return the matched article and close the chat                # build resolved
                conv_id=conv_id,  # Echo the conversation id                                          # echo id
                status="resolved",  # Resolved                                                        # status
                agent_message=structured_output.agent_message,  # Confirmation text                   # message
                kb_id=structured_output.kb_id,  # Matched article id                                  # kb id
                summary=structured_output.summary,  # Matched article summary                         # summary
                chat_close=True,  # A resolution closes the chat                                       # close chat
            )

        if structured_output.status == "no_match":  # No article found -> human handoff, close the chat  # no_match
            self._logger.log(event="foundry_no_match_handoff", correlation_id=conv_id)  # Log it       # log no_match
            return AgentEntryResponse(  # Close with a handoff message; carry the issue summary, no article  # build no_match
                conv_id=conv_id,  # Echo the conversation id                                          # echo id
                status="no_match",  # No KB article found                                             # status
                agent_message=structured_output.agent_message,  # Polite handoff text                 # message
                kb_id=None,  # No article (enforced null)                                             # no article
                summary=structured_output.summary,  # Summary of the user's issue (null only for pure off-topic)  # summary
                chat_close=True,  # A no_match closes the chat                                         # close chat
            )

        # Otherwise: a follow-up turn.
        self._logger.log(event="foundry_follow_up_asked", correlation_id=conv_id)  # Log the follow-up  # log follow-up
        return self._follow_up_response(conv_id, structured_output.agent_message)  # Keep the conversation open  # follow-up

    def _follow_up_response(self, conv_id: Optional[str], agent_message: str) -> AgentEntryResponse:  # Build a follow-up  # follow-up builder
        """Build a follow-up response (conversation stays open, no article).

        Args:
            conv_id: The conversation id (echoed + log key).
            agent_message: The follow-up question / message to show the user.

        Returns:
            A follow-up AgentEntryResponse.

        Example:
            >>> service._follow_up_response("conv_abc", "Tell me more?")  # doctest: +SKIP
            AgentEntryResponse(status='follow_up', ...)
        """
        return AgentEntryResponse(  # Assemble the follow-up response                                  # build follow-up
            conv_id=conv_id,  # Echo the conversation id (pass back next turn)                         # echo id
            status="follow_up",  # Awaiting more info                                                 # status
            agent_message=agent_message or "Could you tell me a bit more?",  # The follow-up text or a default  # message
            kb_id=None,  # No article yet                                                            # no article
            summary=None,  # No summary yet                                                           # no summary
            chat_close=False,  # Conversation stays open                                              # keep open
        )

    def _parse_agent_output(self, reply_text: str, correlation_id: str) -> Optional[AgentStructuredOutput]:  # Strict parse  # parse output
        """Parse the agent's reply into an AgentStructuredOutput, or None on failure.

        Tolerant by design: takes the FIRST JSON object in the reply and ignores any
        prose before it or extra objects after it. This defends against the agent
        emitting more than one JSON object in a single turn.

        Args:
            reply_text: The agent's reply text.
            correlation_id: The correlation id used as the log key.

        Returns:
            A validated AgentStructuredOutput, or None if not valid contract JSON.

        Example:
            >>> service._parse_agent_output('{"status":"search","query":"x","agent_message":""}', "s")  # doctest: +SKIP
            AgentStructuredOutput(status='search', ...)
        """
        cleaned_text = _JSON_FENCE_PATTERN.sub("", reply_text).strip()  # Remove any code fences and trim  # clean text
        parsed_value = self._extract_first_json_object(cleaned_text)  # Take ONLY the first JSON object (robust to extras)  # first object
        if parsed_value is None:  # No decodable JSON object found in the reply                        # no json
            self._logger.log(  # Log the parse failure (keyed by conv_id)                             # log parse fail
                event="foundry_reply_parse_failed",  # Event name                                     # event
                correlation_id=correlation_id,  # Log key                                             # log key
                level="WARNING",  # Severity level                                                    # level
                error_type="JSONDecodeError",  # No valid JSON object present                         # error type
                error_message="No JSON object found in the agent reply.",  # Detail                   # error msg
            )
            return None  # Signal a parse failure to the caller                                       # signal none
        try:  # Validate the extracted object against the strict output schema                        # validate
            return AgentStructuredOutput.model_validate(parsed_value)  # Validate against the contract  # model validate
        except (ValidationError, TypeError) as parse_error:  # Object present but wrong shape         # bad shape
            self._logger.log(  # Log the validation failure detail (keyed by conv_id)                 # log validate fail
                event="foundry_reply_parse_failed",  # Event name                                     # event
                correlation_id=correlation_id,  # Log key                                             # log key
                level="WARNING",  # Severity level                                                    # level
                error_type=type(parse_error).__name__,  # Exception class name                        # error type
                error_message=str(parse_error),  # Exception message                                  # error msg
            )
            return None  # Signal a parse failure to the caller                                       # signal none

    def _extract_first_json_object(self, text: str) -> Optional[dict[str, Any]]:  # First top-level JSON object in text  # extract json
        """Return the first top-level JSON object found in the text, or None.

        Uses raw_decode from the first '{' so any trailing content (a second object,
        stray prose) after a complete object is ignored.

        Args:
            text: The text that should contain a JSON object.

        Returns:
            The first decoded JSON object, or None if none is decodable.

        Example:
            >>> service._extract_first_json_object('{"a": 1}{"b": 2}')  # doctest: +SKIP
            {'a': 1}
        """
        start_index = text.find("{")  # Locate the first opening brace                                # find brace
        if start_index == -1:  # No object present at all                                             # no brace
            return None  # Nothing to decode                                                          # signal none
        try:  # Decode a single JSON value starting at the first brace                                # decode one
            parsed_object, _end_index = json.JSONDecoder().raw_decode(text[start_index:])  # First object only  # raw decode
        except json.JSONDecodeError:  # The text from the first brace is not a valid object           # decode fail
            return None  # Signal no decodable object                                                 # signal none
        if not isinstance(parsed_object, dict):  # The contract requires a JSON object                # type check
            return None  # Reject arrays/scalars                                                      # signal none
        return parsed_object  # Return the first decoded object                                       # return object

    def _build_error_response(  # Build a safe error response (status='error', conversation kept open)  # error builder
        self, conv_id: Optional[str], agent_message: str = _SAFE_FALLBACK_MESSAGE  # Conversation id + optional message  # error args
    ) -> AgentEntryResponse:
        """Build a safe error response.

        Args:
            conv_id: The conversation id when known, else None.
            agent_message: The human-facing message (defaults to the generic fallback).

        Returns:
            An AgentEntryResponse describing the error.

        Example:
            >>> service._build_error_response("conv_abc")  # doctest: +SKIP
            AgentEntryResponse(status='error', ...)
        """
        return AgentEntryResponse(  # Assemble the safe error response                                 # build error
            conv_id=conv_id,  # Echo the conversation id when known                                   # echo id
            status="error",  # Mark the turn as errored                                               # status
            agent_message=agent_message,  # The human-facing message                                  # message
            kb_id=None,  # No article resolved                                                        # no article
            summary=None,  # No summary                                                               # no summary
            chat_close=False,  # Keep the conversation open                                            # keep open
        )


# ========================================== Build / cache =========================================
def _build_turn_service() -> ClassificationTurnService:  # Construct the turn service + its collaborators  # composition root
    """Build the turn service, Foundry gateway and the in-process whole-KB source.

    What this does (v2):
        v2 has NO LLM re-ranker, so there is NO ChatClient and NO default_top_k. The KB source
        (KnowledgeBaseSource) simply loads the index and returns every candidate to the agent.

    Args:
        None.

    Returns:
        A fully wired ClassificationTurnService.

    Example:
        >>> isinstance(_build_turn_service(), ClassificationTurnService)  # doctest: +SKIP
        True
    """
    environment_name = os.environ.get(_ENVIRONMENT_VARIABLE_NAME, _DEFAULT_ENVIRONMENT_NAME)  # Read APP_ENV or default  # read env
    settings = load_settings(environment_name)  # Load + validate the merged environment config       # load config

    # Build only the cross-cutting services this deployment needs.
    emitter = None  # Default: no Event Hub emitter                                                    # no emitter
    if settings.event_hub.enabled:  # Only build an emitter when Event Hub is enabled in config        # emitter toggle
        emitter = EventHubLogEmitter(  # Construct the Event Hub log emitter                           # build emitter
            fully_qualified_namespace=settings.event_hub.fully_qualified_namespace,  # Namespace from config  # namespace
            event_hub_name=settings.event_hub.event_hub_name,  # Event Hub name from config           # hub name
        )
    log_factory = LogFactory(log_level=settings.logging.log_level, emitter=emitter)  # Structured-logger factory  # log factory
    cost_tracker = CostTracker(  # Cost tracker for response token usage                               # cost tracker
        prices=settings.cost.prices,  # Per-model pricing from config                                  # prices
        logger=log_factory.get_logger("usage_cost_tracker"),  # Dedicated cost logger                 # cost logger
    )

    foundry_config = settings.foundry  # Foundry agent config section                                  # foundry cfg

    foundry_agent_gateway = FoundryAgentGateway(  # Build the instrumented Foundry gateway             # build gateway
        project_endpoint=foundry_config.project_endpoint,  # Foundry project endpoint URL             # endpoint
        agent_name=foundry_config.agent_name,  # Pre-created agent name                               # agent name
        agent_version=foundry_config.agent_version,  # Agent version ('latest' / concrete / '')       # agent version
        isolation_key=foundry_config.isolation_key,  # Retained for config compatibility (unused)     # isolation key
        request_timeout_seconds=foundry_config.request_timeout_seconds,  # Hard per-call timeout       # timeout
        cost_tracker=cost_tracker,  # Cost tracker for response token usage                            # cost tracker
        agent_model_name=foundry_config.agent_model_name,  # Model name for cost lookup/logging        # model name
        log_factory=log_factory,  # Logger factory for the gateway                                    # log factory
        retry_max_attempts=settings.retry.max_attempts_for("foundry_agent"),  # Per-operation retry attempts  # retries
        retry_base_delay_seconds=settings.retry.base_delay_seconds,  # Backoff base delay             # base delay
        retry_max_delay_seconds=settings.retry.max_delay_seconds,  # Backoff max delay                # max delay
    )

    index_path = resolve_path(settings.knowledge_base.index_path)  # Resolve the seed-index path against THIS folder  # index path
    knowledge_source = KnowledgeBaseSource(  # Build the in-process whole-KB source (no re-ranker)     # build kb source
        index_path=index_path,  # Absolute path to the KB index in this deployment folder             # index path
        log_factory=log_factory,  # Logger factory for the KB source                                  # log factory
    )

    return ClassificationTurnService(  # Assemble and return the turn service                          # build service
        foundry_agent_gateway=foundry_agent_gateway,  # Instrumented Foundry gateway                   # gateway
        knowledge_source=knowledge_source,  # In-process whole-KB source                               # kb source
        agent_name=foundry_config.agent_name,  # Agent name (config guard)                             # agent name
        auto_create_session=foundry_config.auto_create_session,  # New-conversation toggle            # auto session
        max_search_rounds=foundry_config.max_search_rounds,  # Per-turn search budget                 # budget
        logger=log_factory.get_logger("classification_turn_service"),  # Turn-service logger           # logger
    )


def get_turn_service() -> ClassificationTurnService:  # Return the cached turn service (build once per worker)  # cache accessor
    """Return the cached turn service, building it once per worker (thread-safe).

    Args:
        None.

    Returns:
        The process-wide ClassificationTurnService singleton.

    Example:
        >>> get_turn_service() is get_turn_service()  # doctest: +SKIP
        True
    """
    global _CACHED_TURN_SERVICE  # Refer to the module-level cache                                     # global cache
    if _CACHED_TURN_SERVICE is None:  # Fast path: avoid taking the lock once built                    # fast path
        with _TURN_SERVICE_LOCK:  # Serialise construction so concurrent cold requests build only once  # take lock
            if _CACHED_TURN_SERVICE is None:  # Re-check inside the lock (another thread may have just built it)  # double-check
                _CACHED_TURN_SERVICE = _build_turn_service()  # Construct and cache on first use       # build once
    return _CACHED_TURN_SERVICE  # Return the cached turn service                                      # return cached
