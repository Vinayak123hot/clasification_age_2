####################################################################################################
# Project name      : Outlook Support Classification Agent -- Foundry v2 (ServiceNow-shaped KB)    #
# Business owner    : <fill: business owner / team>                                                #
# Notebook Author   : <fill: author name / team>                                                   #
# Date              : <fill: date>                                                                 #
#                                                                                                  #
# Purpose of file:                                                                                 #
# Instrumented client for the Microsoft Foundry agent via the Responses API (agent_reference +     #
# conversation). Auth is Managed Identity by name (Entra ID, NO API keys).                         #
#   1. Lazily builds AIProjectClient(allow_preview=True) -> get_openai_client() on first use.      #
#   2. Creates a stable Foundry conversation (conv_id) and sends each turn with agent_reference.   #
#   3. Bounds every SDK call with a hard per-call timeout + exponential-backoff retry.             #
#   4. Records response token usage as a logged cost figure; releases clients on close().          #
#                                                                                                  #
# Source:-                                                                                         #
#   - From correlation_ids generate_correlation_id is imported.                                    #
#       - generate_correlation_id:- mints a correlation id for shutdown-time logs.                 #
#   - From model_cost_meter CostTracker is imported.                                               #
#       - CostTracker:- turns response token usage into a logged cost figure.                      #
#   - From telemetry_logging LogFactory / StructuredLogger are imported.                           #
#       - LogFactory / StructuredLogger:- logger factory + structured logger type.                 #
#   - From backoff_retry run_with_retry is imported.                                               #
#       - run_with_retry:- generic exponential-backoff retry runner for transient errors.          #
#   - azure.core.exceptions + openai supply the transient SDK error types worth retrying.          #
####################################################################################################

# ===================================== Imports =====================================
from __future__ import annotations  # Enable postponed evaluation of type annotations (PEP 563)     # future import

from concurrent.futures import ThreadPoolExecutor  # Executor used to enforce a hard per-call timeout  # timeout executor
from concurrent.futures import TimeoutError as FuturesTimeoutError  # Timeout raised by future.result(timeout=...)  # timeout error
from typing import Any, Callable  # Generic type hints for SDK objects and operation callables       # type hints

from azure.core.exceptions import AzureError, ServiceRequestError, ServiceResponseError  # Azure SDK error types  # azure errors
from openai import (  # OpenAI SDK errors (the Responses client returned by get_openai_client is an openai client)  # openai errors
    APIConnectionError,  # Network/connection failure                                                # conn error
    APITimeoutError,  # Request timeout                                                              # timeout error
    InternalServerError,  # HTTP 5xx                                                                 # server error
    OpenAIError,  # Base OpenAI SDK error                                                            # base error
    RateLimitError,  # HTTP 429                                                                      # rate limit
)

from correlation_ids import generate_correlation_id  # Helper to mint a correlation id for shutdown-time logs  # correlation ids
from model_cost_meter import CostTracker  # Turns response token usage into a logged cost figure     # cost meter
from telemetry_logging import LogFactory, StructuredLogger  # Logger factory + structured logger type  # telemetry
from backoff_retry import run_with_retry  # Generic exponential-backoff retry runner                 # backoff retry

# Transient errors (Azure transport + OpenAI) worth retrying with backoff.
_RETRYABLE_EXCEPTIONS: tuple[type[Exception], ...] = (  # Exceptions eligible for a retry            # retryable set
    ServiceRequestError,  # Azure request-send/network failure                                       # azure send
    ServiceResponseError,  # Azure incomplete/failed response (incl. read timeouts)                  # azure response
    APIConnectionError,  # OpenAI connection failure                                                 # openai conn
    APITimeoutError,  # OpenAI request timeout                                                        # openai timeout
    InternalServerError,  # OpenAI 5xx                                                               # openai 5xx
    RateLimitError,  # OpenAI 429                                                                     # openai 429
)


# ===================================== Exceptions =====================================
class FoundryAgentError(Exception):  # Domain error raised when a Foundry Agent Service call fails/times out
    """Raised when a Foundry Agent Service operation fails or times out.

    What this class is:
        - The single domain-level exception surfaced by this client. Every SDK / transport failure
          is mapped to this type so callers never see a raw Azure/OpenAI stack trace.

    Why this exists:
        - To keep the boundary clean: the turn service catches ONE error type and maps it to a safe
          fallback instead of reasoning about every underlying transient/permanent SDK error.
    """


# ============ Foundry Agent Gateway (azure-ai-projects: Responses API + agent_reference) ============
class FoundryAgentGateway:  # Instrumented wrapper over AIProjectClient + the OpenAI Responses client
    """Thin, instrumented gateway for the Microsoft Foundry agent via the Responses API.

    What this class is:
        - A thin, instrumented wrapper over the azure-ai-projects AIProjectClient and the OpenAI
          Responses client it hands out. It owns conversation creation and per-turn responses only.

    Why this exists:
        - To centralise the exact Foundry call surface (agent_reference + conversation), the bounded
          timeout/retry policy, and cost tracking behind one small, testable object.

    Authentication uses Entra ID via DefaultAzureCredential (no API keys). Each turn
    calls the OpenAI Responses client (obtained from the project client) and selects
    the agent per call through
    extra_body={"agent_reference": {"name", "version", "type": "agent_reference"}}.
    Conversation state is held server-side by a Foundry conversation object: its id is
    STABLE for the whole conversation (the caller's conv_id) and is passed on every
    call so Foundry keeps the running history and each reply is only that turn's
    output. Every SDK call is bounded by a hard per-call timeout (worker thread) and
    retried with exponential backoff on transient errors; response token usage is
    logged as a cost figure.

    Security and production notes:
        1. Auth is Managed Identity by NAME (Entra ID via DefaultAzureCredential) - no API keys are
           held in code, config, or environment; nothing here logs a token or credential.
        2. Token scope for Foundry AI Services endpoints (*.services.ai.azure.com) is
           https://ai.azure.com/.default (handled by the credential/SDK, not by this client).
        3. The client is built with allow_preview=True and get_openai_client() takes NO agent_name -
           the agent is selected per call via extra_body agent_reference.
        4. Every remote call is bounded by a hard per-call timeout and exponential-backoff retry, and
           response token usage is recorded as cost for observability.
    """

    def __init__(  # Configure endpoint, agent, timeout, retry policy and cost tracking
        self,
        project_endpoint: str,  # Foundry project endpoint URL                                        # endpoint
        agent_name: str,  # Name of the pre-created Foundry agent                                     # agent name
        agent_version: str,  # Agent version ('latest' resolves newest; a number pins it; '' omits it)  # version
        isolation_key: str,  # Retained for config compatibility (unused in the agent_reference model)  # isolation key
        request_timeout_seconds: int,  # Hard per-call timeout in seconds                             # timeout
        cost_tracker: CostTracker,  # Injected cost tracker for response usage accounting             # cost tracker
        agent_model_name: str,  # Model name used for cost lookup/logging                             # model name
        log_factory: LogFactory,  # Factory used to obtain a structured logger                       # log factory
        retry_max_attempts: int,  # Maximum attempts per SDK call                                     # retry attempts
        retry_base_delay_seconds: float,  # Base delay for exponential backoff                        # base delay
        retry_max_delay_seconds: float,  # Upper bound on a single backoff wait                       # max delay
    ) -> None:
        """Create the gateway (no network call yet) and its bounded-call executor.

        What this method is:
            - The constructor: it only stores config and builds the bounded-call executor. No
              credential, project client, or network call is created here (that is lazy, on first use).

        Why this exists:
            - To keep construction cheap and side-effect free so the worker can build the object once
              and only touch Foundry when a turn actually arrives.

        Security and production notes:
            1. No secrets are accepted or stored - authentication is Managed Identity by name (Entra ID),
               resolved lazily when the client is first built.
            2. The timeout / retry / cost-tracking knobs are injected from config so operational limits
               are auditable and not hard-coded.

        Args:
            project_endpoint: The Foundry project endpoint URL.
            agent_name: Name of the pre-created Foundry agent.
            agent_version: 'latest' (resolve newest), a concrete version, or '' (omit).
            isolation_key: Retained for config compatibility (unused here).
            request_timeout_seconds: Hard per-call timeout in seconds.
            cost_tracker: Tracker used to log per-response cost.
            agent_model_name: Model name used for cost lookup/logging.
            log_factory: Factory used to obtain a structured logger.
            retry_max_attempts: Max attempts for a single SDK call.
            retry_base_delay_seconds: Base backoff delay.
            retry_max_delay_seconds: Upper bound on a single backoff wait.

        Returns:
            None.

        Example:
            >>> FoundryAgentGateway(  # doctest: +SKIP
            ...     "https://r.services.ai.azure.com/api/projects/p", "clasification-agent",
            ...     "latest", "", 90, cost_tracker, "gpt-4.1-mini", log_factory, 4, 0.5, 8.0)
        """
        self._project_endpoint = project_endpoint  # Store the project endpoint for lazy client creation  # endpoint
        self._agent_name = agent_name  # Store the agent name                                         # agent name
        self._configured_agent_version = agent_version  # Store the configured version ('latest'/concrete/'')  # version
        self._isolation_key = isolation_key  # Retained for compatibility (unused in the agent_reference model)  # isolation key
        self._request_timeout_seconds = request_timeout_seconds  # Store the per-call timeout          # timeout
        self._cost_tracker = cost_tracker  # Store the cost tracker                                    # cost tracker
        self._agent_model_name = agent_model_name  # Store the model name for cost lookup              # model name
        self._logger: StructuredLogger = log_factory.get_logger("foundry_agent_gateway")  # Named structured logger  # logger
        self._retry_max_attempts = retry_max_attempts  # Store max retry attempts                      # retry attempts
        self._retry_base_delay_seconds = retry_base_delay_seconds  # Store backoff base delay          # base delay
        self._retry_max_delay_seconds = retry_max_delay_seconds  # Store backoff max delay             # max delay
        self._credential: Any | None = None  # Lazily-created Entra ID credential                      # credential
        self._project_client: Any | None = None  # Lazily-created AIProjectClient                      # project client
        self._openai_client: Any | None = None  # Lazily-created OpenAI Responses client               # openai client
        self._resolved_version: str | None = None  # Cached concrete agent version once resolved       # resolved version
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="foundry-agent")  # Bounded-call executor  # executor

    # ===================================== Client Lifecycle =====================================
    def _get_project_client(self) -> Any:  # Create the AIProjectClient + OpenAI client once, on demand
        """Return the AIProjectClient, creating it, the credential and the OpenAI client on first use.

        What this method is:
            - The lazy client factory. It builds the Entra ID credential, the AIProjectClient
              (allow_preview=True), and the OpenAI Responses client exactly once, then caches them.

        Why this exists:
            - To defer all network/credential work out of construction and share one initialised
              client set across every turn on the worker.

        Security and production notes:
            1. Authentication is Managed Identity by name via DefaultAzureCredential - no API keys.
            2. get_openai_client() is called with NO agent_name; the agent is chosen per call via
               extra_body agent_reference (matches the working Foundry sample).
            3. self is committed only after EVERY step succeeds, so the cache guard is never satisfied
               with a half-initialised state.

        Args:
            None.

        Returns:
            The initialised azure-ai-projects AIProjectClient.

        Example:
            >>> gateway._get_project_client()  # doctest: +SKIP
            <AIProjectClient ...>
        """
        if self._project_client is not None and self._openai_client is not None:  # Reuse only when fully initialised  # cache guard
            return self._project_client  # Return the cached client                                   # cached
        from azure.ai.projects import AIProjectClient  # Lazy import of the AI Projects client         # lazy import
        from azure.identity import DefaultAzureCredential  # Lazy import of the Entra ID credential    # lazy import

        credential = DefaultAzureCredential()  # Build the Entra ID credential chain (into a local first)  # credential
        project_client = AIProjectClient(  # Build the Foundry project client (preview surface enabled)  # project client
            endpoint=self._project_endpoint,  # Point at the Foundry project endpoint                 # endpoint
            credential=credential,  # Authenticate via Entra ID (no API keys)                         # entra auth
            allow_preview=True,  # Enable the preview surface used by the Responses/agents APIs        # preview on
        )
        # NOTE: get_openai_client() takes NO agent_name — the agent is selected per call
        # via extra_body agent_reference (matches the working Foundry sample).
        openai_client = project_client.get_openai_client()  # OpenAI Responses client for this project  # openai client
        # Commit to self only after EVERY step succeeds, so the cache guard above is never
        # satisfied with a half-initialised (project_client set, openai_client None) state.
        self._credential = credential  # Store the credential (used by close())                       # store cred
        self._project_client = project_client  # Store the fully-initialised project client           # store client
        self._openai_client = openai_client  # Store the OpenAI client                                # store openai
        return self._project_client  # Return the freshly created client                              # return client

    def _bounded_call(  # Run one SDK operation with retries under a hard per-call timeout
        self, operation: Callable[[], Any], operation_name: str, correlation_id: str  # Operation, its name, correlation id
    ) -> Any:
        """Execute an SDK operation with retry/backoff and a hard per-call timeout.

        What this method is:
            - The single choke point for every remote SDK call: it wraps the operation in the retry
              runner and runs that on a worker thread bounded by a hard timeout.

        Why this exists:
            - To guarantee no Foundry call can hang the request indefinitely, and to apply one uniform
              retry + logging + domain-error policy to every call.

        Security and production notes:
            1. Transient errors are retried with exponential backoff; a hard timeout caps total wait.
            2. Failures/timeouts are logged with the correlation id and re-raised as FoundryAgentError,
               so no raw SDK exception (or its message internals) leaks to the caller.

        Args:
            operation: A zero-argument callable performing the SDK call.
            operation_name: A short name for the operation (used in logs).
            correlation_id: The end-to-end correlation id.

        Returns:
            Whatever the operation returns.

        Raises:
            FoundryAgentError: If the operation times out or fails after retries.

        Example:
            >>> gateway._bounded_call(lambda: 2, "noop", "cid")  # doctest: +SKIP
            2
        """
        def _retrying_operation() -> Any:  # Wrap the operation in the exponential-backoff retry runner  # retry wrapper
            """Run the operation through the retry-with-backoff runner."""
            return run_with_retry(  # Retry the operation on transient errors                         # retry call
                operation,  # The SDK call to execute/retry                                           # operation
                correlation_id=correlation_id,  # Correlation id for retry logs                       # correlation
                operation_name=operation_name,  # Operation label for logs                            # op name
                logger=self._logger,  # Structured logger for retry events                            # logger
                max_attempts=self._retry_max_attempts,  # Max attempts from config                    # attempts
                base_delay_seconds=self._retry_base_delay_seconds,  # Backoff base delay from config  # base delay
                max_delay_seconds=self._retry_max_delay_seconds,  # Backoff max delay from config     # max delay
                retryable_exceptions=_RETRYABLE_EXCEPTIONS,  # Which exceptions trigger a retry        # retryable
            )

        future = self._executor.submit(_retrying_operation)  # Schedule the retrying operation on a worker thread  # submit
        try:  # Await the result under a hard timeout                                                 # await
            return future.result(timeout=self._request_timeout_seconds)  # Block only up to the configured timeout  # bounded wait
        except FuturesTimeoutError as timeout_error:  # The call exceeded the per-call timeout        # timeout
            self._logger.log(  # Log the timeout                                                      # log timeout
                event="foundry_call_timeout",  # Event name                                           # event
                correlation_id=correlation_id,  # Correlate with this request                         # correlation
                level="ERROR",  # Severity level                                                      # level
                operation_name=operation_name,  # Which operation timed out                           # op name
                timeout_seconds=self._request_timeout_seconds,  # The configured timeout              # timeout
            )
            raise FoundryAgentError(f"Foundry operation '{operation_name}' timed out.") from timeout_error  # Domain error  # raise
        except (AzureError, OpenAIError, ValueError) as call_error:  # The call failed (after retries) or returned bad data  # failure
            self._logger.log(  # Log the failure                                                      # log failure
                event="foundry_call_failed",  # Event name                                            # event
                correlation_id=correlation_id,  # Correlate with this request                         # correlation
                level="ERROR",  # Severity level                                                      # level
                operation_name=operation_name,  # Which operation failed                              # op name
                error_type=type(call_error).__name__,  # Exception class name                         # error type
                error_message=str(call_error),  # Exception message                                   # error msg
            )
            raise FoundryAgentError(f"Foundry operation '{operation_name}' failed.") from call_error  # Domain error  # raise

    # ===================================== Version Resolution =====================================
    def _resolve_version(self, correlation_id: str) -> str:  # Resolve the concrete agent version (caches the result)
        """Return the concrete agent version, resolving 'latest' via the service once.

        What this method is:
            - The service-backed resolver: it fetches the agent metadata and reads the newest version,
              caching it for the life of the gateway.

        Why this exists:
            - So a configured 'latest' is turned into a concrete, pinned version exactly once instead
              of on every turn.

        Args:
            correlation_id: The end-to-end correlation id.

        Returns:
            The concrete agent version string.

        Raises:
            FoundryAgentError: If the version cannot be resolved.

        Example:
            >>> gateway._resolve_version("cid")  # doctest: +SKIP
            '9'
        """
        if self._resolved_version is not None:  # Reuse the cached version once resolved              # cache guard
            return self._resolved_version  # Return the cached concrete version                       # cached
        project_client = self._get_project_client()  # Ensure the project client exists               # ensure client
        agent = self._bounded_call(  # Fetch the agent to read its versions                           # bounded call
            lambda: project_client.agents.get(agent_name=self._agent_name),  # SDK call: get agent metadata  # sdk get
            operation_name="agents.get",  # Operation label for logs                                  # op name
            correlation_id=correlation_id,  # Correlation id for tracing                               # correlation
        )
        self._resolved_version = str(agent.versions["latest"].version)  # Read + cache the newest version  # read version
        self._logger.log(  # Log the resolved version                                                 # log version
            event="foundry_agent_version_resolved",  # Event name                                     # event
            correlation_id=correlation_id,  # Correlate with this request                             # correlation
            agent_version=self._resolved_version,  # The resolved version                             # version
        )
        return self._resolved_version  # Return the resolved concrete version                         # return version

    def _resolve_reference_version(self, correlation_id: str) -> str | None:  # Version to put in the agent_reference
        """Return the version string for the agent_reference, or None to omit it.

        A concrete configured version (e.g. '9') is used as-is. 'latest' is resolved to
        the newest version (best-effort; omitted if resolution fails). An empty/'default'
        value omits the version entirely so Foundry uses the agent's default.

        What this method is:
            - The policy that decides which version (if any) goes into the agent_reference for a call.

        Why this exists:
            - To keep version selection resilient: a concrete pin is honoured, 'latest' is resolved
              best-effort, and any failure degrades safely to Foundry's default rather than erroring.

        Args:
            correlation_id: The end-to-end correlation id.

        Returns:
            The version string to send, or None to omit the version key.

        Example:
            >>> gateway._resolve_reference_version("cid")  # doctest: +SKIP
            '9'
        """
        configured_version = (self._configured_agent_version or "").strip()  # Normalise the configured version  # normalise
        if configured_version and configured_version.lower() not in ("latest", "default", "none"):  # Concrete version  # concrete?
            return configured_version  # Use the pinned version as-is (e.g. '9')                      # pinned
        if configured_version.lower() == "latest":  # Resolve the newest version (best-effort)        # latest?
            try:  # Resolution can fail on some SDK/agent states; omit the version then               # try resolve
                return self._resolve_version(correlation_id)  # Resolve + cache the newest version    # resolve
            except FoundryAgentError as resolve_error:  # Could not resolve 'latest'                  # resolve failed
                self._logger.log(  # Note that we will omit the version (Foundry uses its default)    # log omit
                    event="foundry_version_resolve_failed",  # Event name                             # event
                    correlation_id=correlation_id,  # Correlation id for tracing                      # correlation
                    level="WARNING",  # Severity level                                                # level
                    error_message=str(resolve_error),  # Why resolution failed                        # error msg
                )
                return None  # Omit the version key                                                   # omit
        return None  # Empty/default -> omit the version key                                          # omit

    # ===================================== Public API =====================================
    def create_conversation(self, correlation_id: str) -> str:  # Create a Foundry conversation, return its id (conv_id)
        """Create a conversation that holds history server-side and return its id.

        The conversation id is STABLE for the whole conversation (the caller's conv_id);
        every turn is sent with this id so Foundry keeps the running history.

        What this method is:
            - The entry point that mints a new Foundry conversation and returns its stable id (conv_id).

        Why this exists:
            - So self-testing (auto_create_thread) can obtain a conv_id; in production the calling
              application usually supplies the conv_id instead.

        Security and production notes:
            1. The call runs under the bounded timeout/retry policy; failures surface as FoundryAgentError.
            2. Only the conversation id is returned/logged - no message content is persisted here.

        Args:
            correlation_id: The end-to-end correlation id.

        Returns:
            The new conversation's id (the caller's conv_id).

        Raises:
            FoundryAgentError: If the conversation cannot be created.

        Example:
            >>> gateway.create_conversation("cid")  # doctest: +SKIP
            'conv_abc123'
        """
        self._get_project_client()  # Ensure the project + OpenAI clients exist                       # ensure client
        openai_client = self._openai_client  # The OpenAI Responses client for this project           # openai client
        conversation = self._bounded_call(  # Create the conversation under timeout/retry             # bounded call
            lambda: openai_client.conversations.create(),  # SDK call: create a conversation          # sdk create
            operation_name="conversations.create",  # Operation label for logs                        # op name
            correlation_id=correlation_id,  # Correlation id for tracing                               # correlation
        )
        conversation_id = str(getattr(conversation, "id", "") or "")  # Extract the new conversation id (== conv_id)  # extract id
        self._logger.log(event="foundry_conversation_created", correlation_id=conversation_id)  # Log it (keyed by conv_id)  # log
        return conversation_id  # Return the conversation id                                          # return id

    def create_response(  # Send input to the agent within a conversation and return the reply text
        self, input_text: str, conversation_id: str | None, correlation_id: str  # Input, conversation id, correlation id
    ) -> str:
        """Send input to the agent via the Responses API and return its reply text.

        The agent is selected per call through extra_body agent_reference. When
        conversation_id is provided, the call is part of that conversation and Foundry
        carries the history server-side, so the returned text is ONLY this turn's reply.

        What this method is:
            - The core turn method: it builds the agent_reference, sends the user input on the
              Responses API, records cost, and returns just this turn's reply text.

        Why this exists:
            - To expose one simple call surface for the turn service, hiding agent selection,
              conversation attachment, timeout/retry, and cost accounting.

        Security and production notes:
            1. The agent is selected per call via extra_body agent_reference (name + optional version);
               no keys are involved - auth is Managed Identity by name (Entra ID).
            2. The call is bounded by timeout/retry and its token usage is recorded as cost.
            3. Only this turn's output_text is returned; Foundry keeps the running history server-side
               under the conversation id.

        Args:
            input_text: The text to send (user message, or fed-back KB candidates).
            conversation_id: The Foundry conversation id (conv_id), or None.
            correlation_id: The end-to-end correlation id.

        Returns:
            The agent's reply text for this turn (expected to be a strict JSON object).

        Raises:
            FoundryAgentError: If the response call fails or times out.

        Example:
            >>> gateway.create_response("outlook crashes", "conv_abc", "cid")  # doctest: +SKIP
            '{"status": "search", ...}'
        """
        self._get_project_client()  # Ensure the project + OpenAI clients exist                       # ensure client
        openai_client = self._openai_client  # The OpenAI Responses client for this project           # openai client
        agent_reference: dict[str, Any] = {  # Select the agent for this call (matches the Foundry sample)  # agent ref
            "name": self._agent_name,  # The agent name                                               # agent name
            "type": "agent_reference",  # Reference type required by Foundry                          # ref type
        }
        reference_version = self._resolve_reference_version(correlation_id)  # Version to pin (or None to omit)  # resolve version
        if reference_version:  # Only include the version when we have one                            # version?
            agent_reference["version"] = reference_version  # Pin the agent version in the reference  # pin version
        request_kwargs: dict[str, Any] = {  # Assemble the Responses API call arguments               # request kwargs
            "input": [{"role": "user", "content": input_text}],  # The user message for this call     # input
            "extra_body": {"agent_reference": agent_reference},  # Select the agent via agent_reference  # extra body
        }
        if conversation_id:  # Attach to the conversation so Foundry carries the history server-side  # conv?
            request_kwargs["conversation"] = conversation_id  # The conversation this turn belongs to  # attach conv
        response = self._bounded_call(  # Call the Responses API under timeout/retry                  # bounded call
            lambda: openai_client.responses.create(**request_kwargs),  # SDK call: create a response  # sdk create
            operation_name="responses.create",  # Operation label for logs                            # op name
            correlation_id=correlation_id,  # Correlation id for tracing                               # correlation
        )
        self._record_cost(response, correlation_id)  # Turn the response's token usage into a logged cost  # record cost
        return str(getattr(response, "output_text", "") or "")  # Return ONLY this turn's reply text  # return text

    # ===================================== Internal Helpers =====================================
    def _record_cost(self, response: Any, correlation_id: str) -> None:  # Derive and log cost from response usage
        """Extract token usage from a Responses result and record its cost.

        What this method is:
            - The cost accountant: it reads token usage off the Responses result and forwards it to
              the injected cost tracker.

        Why this exists:
            - To make every model call observable/costed without the turn service having to know the
              usage field names.

        Security and production notes:
            1. Usage is read defensively (getattr with fallbacks) so a missing/renamed field never
               raises - absent usage simply skips cost recording.

        Args:
            response: The Responses API result (expected to expose a `usage` object).
            correlation_id: The end-to-end correlation id.

        Returns:
            None.

        Example:
            >>> gateway._record_cost(response, "cid")  # doctest: +SKIP
        """
        usage = getattr(response, "usage", None)  # Safely read the response usage object             # read usage
        if usage is None:  # Nothing to record when usage is absent                                   # no usage
            return  # Skip cost recording                                                             # skip
        prompt_tokens = getattr(usage, "input_tokens", None)  # Responses API names input tokens 'input_tokens'  # input tokens
        if prompt_tokens is None:  # Fall back to the chat-style name if needed                       # fallback?
            prompt_tokens = getattr(usage, "prompt_tokens", 0)  # Chat-style prompt token count       # prompt tokens
        completion_tokens = getattr(usage, "output_tokens", None)  # Responses API names output tokens 'output_tokens'  # output tokens
        if completion_tokens is None:  # Fall back to the chat-style name if needed                   # fallback?
            completion_tokens = getattr(usage, "completion_tokens", 0)  # Chat-style completion token count  # completion tokens
        self._cost_tracker.record_usage(  # Forward token counts to the cost tracker                  # record usage
            model_name=self._agent_model_name,  # Model used for cost-rate lookup                     # model name
            prompt_tokens=int(prompt_tokens or 0),  # Prompt/input tokens (default 0)                 # prompt tokens
            completion_tokens=int(completion_tokens or 0),  # Completion/output tokens (default 0)    # completion tokens
            correlation_id=correlation_id,  # Correlate the usage record with the request             # correlation
        )

    def close(self) -> None:  # Release the executor, clients and credential (best-effort)
        """Shut down the executor and close the project client and credential.

        Cleanup failures are logged (never silently swallowed) and never raised.

        What this method is:
            - The teardown path: it stops the bounded-call executor and closes the project client and
              credential, best-effort.

        Why this exists:
            - So a worker can release Foundry/network resources cleanly on shutdown without one failed
               close masking another or crashing shutdown.

        Security and production notes:
            1. Each close is independent and guarded; failures are logged with a correlation id and
               never raised, so teardown always completes.

        Args:
            None.

        Returns:
            None.

        Example:
            >>> gateway.close()  # doctest: +SKIP
        """
        shutdown_correlation_id = generate_correlation_id()  # Mint a correlation id for shutdown logs  # shutdown cid
        try:  # Shut down the bounded-call executor first                                             # stop executor
            self._executor.shutdown(wait=False)  # Stop accepting work; do not block on in-flight calls  # shutdown
        except Exception as executor_error:  # Never let executor teardown mask the rest              # guard
            self._logger.log(  # Log the executor shutdown failure                                    # log fail
                event="foundry_executor_close_failed",  # Event name                                  # event
                correlation_id=shutdown_correlation_id,  # Correlation id for the teardown            # correlation
                level="WARNING",  # Severity level                                                    # level
                error_type=type(executor_error).__name__,  # Exception class name                    # error type
                error_message=str(executor_error),  # Exception message                               # error msg
            )
        for resource_name, resource in (("project_client", self._project_client), ("entra_id_credential", self._credential)):  # Each closable
            if resource is None:  # Skip resources that were never created                            # skip none
                continue  # Nothing to close                                                          # continue
            try:  # Attempt to close each resource independently                                      # try close
                resource.close()  # Invoke the resource's close method                               # close
            except Exception as close_error:  # Never let one cleanup failure mask another            # guard
                self._logger.log(  # Log the close failure                                            # log fail
                    event="foundry_resource_close_failed",  # Event name                              # event
                    correlation_id=shutdown_correlation_id,  # Correlation id for the teardown        # correlation
                    level="WARNING",  # Severity level                                                # level
                    resource_name=resource_name,  # Which resource failed to close                    # resource
                    error_type=type(close_error).__name__,  # Exception class name                    # error type
                    error_message=str(close_error),  # Exception message                              # error msg
                )
