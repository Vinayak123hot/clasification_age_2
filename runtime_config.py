####################################################################################################
# Project name      : Outlook Support Classification Agent -- Foundry v2 (ServiceNow-shaped KB)    #
# Business owner    : <fill: business owner / team>                                                #
# Notebook Author   : <fill: author name / team>                                                   #
# Date              : <fill: date>                                                                 #
#                                                                                                  #
# Purpose of file:                                                                                 #
#   1. Load the single per-environment YAML config and validate it into typed Pydantic settings.   #
#   2. Define every config shape for the combined v2 deployment (Foundry, KB, retry, cost, logs).  #
#   3. Overlay a small allowlist of non-secret env vars (FOUNDRY_* / APP_ENV) onto the config.     #
#                                                                                                  #
# Source:-                                                                                         #
#   - os is read for the environment-variable override allowlist (no secrets ever live here).      #
#       - os.environ.get:- reads each override value; None means the env var is unset (skipped).   #
#   - pathlib.Path anchors PROJECT_ROOT to THIS file's folder (unique module name, no collision).  #
#   - yaml.safe_load parses the per-environment config_<env>.yaml file from this folder.           #
#   - pydantic.BaseModel / Field type every config boundary and supply safe field defaults.        #
####################################################################################################

# ============================================ Imports =============================================
from __future__ import annotations  # Enable postponed evaluation of annotations (PEP 563) for forward refs  # future import

import os  # Read environment variables used to override selected config values                   # stdlib os
from pathlib import Path  # Filesystem path handling                                               # stdlib pathlib

import yaml  # Parse the YAML configuration file                                                   # yaml parser
from pydantic import BaseModel, Field  # Base class for typed models and helper for field defaults  # boundary models

# Project root = the folder holding THIS file. Because this module has a unique name,
# it never collides with other functions' loaders, so PROJECT_ROOT always resolves to
# this deployment's own folder (its config + kb_index live here).
PROJECT_ROOT: Path = Path(__file__).resolve().parent  # Absolute project root derived from this file's location  # deployment root

# Environment variable -> (config section, field). Lets operators override a small set
# of critical, non-secret settings via Application Settings WITHOUT redeploying the YAML.
_ENV_OVERRIDES: dict[str, tuple[str, str]] = {  # Map of env var name to the (section, field) it overrides  # override table
    "FOUNDRY_PROJECT_ENDPOINT": ("foundry", "project_endpoint"),  # Override the Foundry project endpoint  # foundry endpoint
    "FOUNDRY_AGENT_NAME": ("foundry", "agent_name"),  # Override the Foundry agent name                # foundry agent name
    "FOUNDRY_AGENT_VERSION": ("foundry", "agent_version"),  # Override the agent version ('latest' or a number)  # foundry version
    "FOUNDRY_AUTO_CREATE_SESSION": ("foundry", "auto_create_session"),  # Toggle: start a conversation when conv_id is absent  # session toggle
}
_BOOLEAN_ENV_KEYS: set[str] = {  # Env keys whose value must be parsed as a boolean                 # boolean overrides
    "FOUNDRY_AUTO_CREATE_SESSION",  # Whether the function starts a conversation when conv_id is absent  # session bool
}


# ======================================== Path resolution ========================================
def resolve_path(relative_or_absolute_path: str) -> Path:  # Resolve a path against this deployment's folder
    """Resolve a path against the project root (absolute paths pass through).

    What this function is:
        - The single path anchor for this deployment: it turns a relative config path into an
          absolute one under PROJECT_ROOT, and returns already-absolute paths unchanged.

    Why this exists:
        - Azure Functions flat folders share a process; anchoring every path to THIS file's folder
          keeps the deployment reading its OWN config and kb_index rather than a sibling's.

    Args:
        relative_or_absolute_path: A relative or absolute path string.

    Returns:
        An absolute Path.

    Example:
        >>> resolve_path("kb_index.json")  # doctest: +SKIP
        PosixPath('/home/site/wwwroot/classification_agent__foundry_version_v2/kb_index.json')
    """
    candidate_path = Path(relative_or_absolute_path)  # Wrap the input string as a Path object      # wrap path
    if candidate_path.is_absolute():  # If the path is already absolute                             # absolute check
        return candidate_path  # Return it unchanged                                                # pass through
    return (PROJECT_ROOT / candidate_path).resolve()  # Otherwise join with the project root and resolve to absolute  # anchor + resolve


# ========================================= Config models =========================================
class EventHubConfig(BaseModel):  # Typed model for Event Hub log-forwarding settings
    """Event Hub log-forwarding settings.

    What this model is:
        - The typed shape of the optional Event Hub log sink; disabled by default so no forwarding
          happens until an operator opts in via config.

    Security and production notes:
        1. No connection string / key is stored here - authentication is Managed Identity by name.
        2. Forwarding stays OFF (enabled=False) unless explicitly enabled in the environment YAML.
    """

    enabled: bool = False  # Whether Event Hub forwarding is enabled (default off)                  # forwarding toggle
    fully_qualified_namespace: str = ""  # Event Hubs namespace host (default empty)                # namespace host
    event_hub_name: str = ""  # Target Event Hub name (default empty)                               # hub name


class KnowledgeBaseConfig(BaseModel):  # Typed model for KB retrieval settings
    """Knowledge-base retrieval settings (static seed index).

    What this model is:
        - The typed shape of the in-process KB retrieval settings: which seed index to load and how
          many candidates to return per search.

    Why this exists:
        - v2 runs KB selection IN-PROCESS (no re-ranker, no separate tool endpoint), so the loader
          only needs the index location and a default fan-out.
    """

    index_path: str = "kb_index.json"  # Path to the built seed index (relative to this folder by default)  # index location
    default_top_k: int = 5  # Default number of candidates to retrieve (default 5)                  # retrieval fan-out


class RetryConfig(BaseModel):  # Typed model for retry/backoff settings
    """Retry/backoff settings with optional per-operation overrides.

    What this model is:
        - The typed shape of the exponential-backoff policy applied to every remote call, with an
          optional per-operation attempt override map.

    Why this exists:
        - Remote calls (Foundry) must retry with bounded backoff and timeouts rather than failing on
          the first transient error; centralising the policy keeps every call site consistent.
    """

    default_max_attempts: int = 4  # Default maximum retry attempts (default 4)                     # attempt cap
    base_delay_seconds: float = 0.5  # Base backoff delay in seconds (default 0.5)                  # base delay
    max_delay_seconds: float = 8.0  # Maximum backoff delay in seconds (default 8.0)                # delay ceiling
    per_tool_max_attempts: dict[str, int] = Field(default_factory=dict)  # Per-operation attempt overrides (default empty)  # per-op overrides

    def max_attempts_for(self, operation_name: str) -> int:  # Return max attempts for an operation, defaulting when absent
        """Return the max attempts for an operation, falling back to the default.

        Args:
            operation_name: The operation name (e.g. 'foundry_agent').

        Returns:
            The configured max attempts for that operation.

        Example:
            >>> RetryConfig().max_attempts_for("foundry_agent")
            4
        """
        return self.per_tool_max_attempts.get(operation_name, self.default_max_attempts)  # Look up override or default  # per-op lookup


class ModelPrice(BaseModel):  # Typed model for one model's per-token prices
    """Per-token input/output price for one model.

    What this model is:
        - The typed price pair (input, output) for a single model, used by the cost tracker to price
          token usage.
    """

    input_price: float = 0.0  # Price per input token (default 0.0)                                 # input price
    output_price: float = 0.0  # Price per output token (default 0.0)                               # output price


class CostConfig(BaseModel):  # Typed model for cost-tracking prices keyed by model
    """Cost-tracking prices keyed by model name.

    What this model is:
        - The typed lookup table mapping a model name to its ModelPrice, letting the cost tracker
          price each response's token usage.
    """

    prices: dict[str, ModelPrice] = Field(default_factory=dict)  # Map of model name to its ModelPrice (default empty)  # price table


class LoggingConfig(BaseModel):  # Typed model for logging settings
    """Logging settings.

    What this model is:
        - The typed shape of the structured-logging settings; controls the minimum level emitted by
          the deployment.
    """

    log_level: str = "INFO"  # Minimum log level to emit (default INFO)                             # log level


class FoundryConfig(BaseModel):  # Typed model for the Microsoft Foundry agent (azure-ai-projects, Responses API)
    """Microsoft Foundry agent settings (azure-ai-projects, Responses API + conversation).

    What this model is:
        - The typed shape of every Foundry connection value: the agent is addressed by NAME +
          VERSION and invoked via the Responses API using a Foundry conversation whose id is the
          caller's stable conv_id. KB search runs IN-PROCESS in this same deployment (no separate
          tool endpoint), so no tool URL or tool audience is needed.

    Security and production notes:
        1. No secrets/keys live here - authentication is Entra ID / Managed Identity by name only.
        2. project_endpoint is a plain resource URL; there is no key or connection string to store.
    """

    project_endpoint: str = ""  # Foundry project endpoint (https://<res>.services.ai.azure.com/api/projects/<name>)  # project endpoint
    agent_name: str = ""  # Name of the pre-created Foundry agent (e.g. 'clasification-agent')      # agent name
    agent_version: str = "latest"  # Agent version to pin; 'latest' resolves newest, '<n>' pins it, '' uses the default  # agent version
    isolation_key: str = ""  # Session isolation key (optional; matches your Foundry setup)         # isolation key
    agent_model_name: str = ""  # Model name used for cost lookup/logging of response token usage    # cost model name
    auto_create_session: bool = True  # True: start a conversation when conv_id is absent. False: require conv_id  # session toggle
    request_timeout_seconds: int = 90  # Hard per-call timeout in seconds for Foundry SDK calls     # call timeout
    max_search_rounds: int = 6  # Max in-process KB searches per turn before forcing a no_match handoff  # search budget


class AppSettings(BaseModel):  # Top-level typed model aggregating all config sections
    """The full validated application configuration for the combined v2 deployment.

    What this model is:
        - The single aggregate boundary that every config section validates into; the turn service
          reads its typed fields rather than raw YAML.

    Why this exists:
        - v2 drops the Azure OpenAI re-ranker, so there is no azure_openai section here; KB
          selection runs in-process using only the knowledge_base + foundry settings.
    """

    environment_name: str  # Name of the environment this config represents                        # env name
    knowledge_base: KnowledgeBaseConfig = Field(default_factory=KnowledgeBaseConfig)  # KB retrieval settings  # kb section
    foundry: FoundryConfig = Field(default_factory=FoundryConfig)  # Foundry agent settings         # foundry section
    event_hub: EventHubConfig = Field(default_factory=EventHubConfig)  # Event Hub settings         # event hub section
    retry: RetryConfig = Field(default_factory=RetryConfig)  # Retry settings                       # retry section
    cost: CostConfig = Field(default_factory=CostConfig)  # Cost settings                           # cost section
    logging: LoggingConfig = Field(default_factory=LoggingConfig)  # Logging settings               # logging section


# ============================================= Loader ============================================
def load_settings(environment_name: str) -> AppSettings:  # Load and validate config for a given environment
    """Load and validate config_<environment_name>.yaml from this deployment folder.

    What this function is:
        - The single entry point that reads this deployment's own YAML, overlays the non-secret env
          overrides, and returns a fully validated AppSettings.

    Why this exists:
        - It keeps config loading in one typed place so the turn service never parses raw YAML and
          every value is schema-checked before use.

    Security and production notes:
        1. No secrets are read from the YAML - only non-secret settings; auth is Managed Identity.
        2. The file is read as UTF-8 from PROJECT_ROOT, so a sibling deployment cannot supply it.

    Args:
        environment_name: The environment name (e.g. 'poc').

    Returns:
        The validated AppSettings.

    Raises:
        FileNotFoundError: If the config file does not exist.

    Example:
        >>> settings = load_settings("poc")  # doctest: +SKIP
        >>> settings.foundry.agent_name  # doctest: +SKIP
        'clasification-agent'
    """
    config_path = PROJECT_ROOT / f"config_{environment_name}.yaml"  # Build path to this env's config file (this folder)  # config path
    if not config_path.exists():  # Verify the config file is present                               # existence check
        raise FileNotFoundError(  # Raise a clear error when the file is missing                    # missing file
            f"Config file not found for environment '{environment_name}': {config_path}"  # Error with env and path  # error message
        )
    with config_path.open("r", encoding="utf-8") as config_file:  # Open the config file for reading as UTF-8  # open utf-8
        config_data = yaml.safe_load(config_file)  # Parse YAML content into Python data            # parse yaml
    config_data = _apply_env_overrides(config_data or {})  # Overlay any environment-variable overrides onto the parsed data  # apply overrides
    return AppSettings.model_validate(config_data)  # Validate parsed data against AppSettings and return it  # validate + return


def _apply_env_overrides(config_data: dict) -> dict:  # Overlay selected env vars onto the parsed config dict
    """Overlay a small allowlist of environment variables onto the config data.

    What this function is:
        - The controlled override step: only the keys in _ENV_OVERRIDES are honoured, and only when
          the env var is actually set.

    Why this exists:
        - It lets a Function App override critical non-secret settings via Application Settings
          without redeploying the YAML file, while keeping the surface tiny and auditable.

    Security and production notes:
        1. The allowlist carries FOUNDRY_* endpoint/name/version + the session toggle only - never
          any secret, key, or connection string (auth remains Managed Identity by name).
        2. Unset env vars are skipped so the YAML value stays in place; a malformed (non-dict)
          section is left untouched rather than being corrupted.

    Args:
        config_data: The parsed YAML config dictionary.

    Returns:
        The same dictionary with any environment overrides applied.

    Example:
        >>> _apply_env_overrides({"foundry": {}})  # doctest: +SKIP
        {'foundry': {...}}
    """
    for env_var_name, (section_name, field_name) in _ENV_OVERRIDES.items():  # Iterate over each supported override  # loop overrides
        env_value = os.environ.get(env_var_name)  # Read the environment variable value (None when unset)  # read env var
        if env_value is None:  # Skip overrides whose env var is not set                            # unset skip
            continue  # Leave the YAML value in place                                               # keep yaml value
        section = config_data.setdefault(section_name, {})  # Ensure the target section dict exists  # ensure section
        if not isinstance(section, dict):  # Guard against a malformed (non-dict) section in the YAML  # dict guard
            continue  # Skip the override rather than corrupting the config                         # safe skip
        if env_var_name in _BOOLEAN_ENV_KEYS:  # Boolean-typed overrides need parsing from their string form  # bool branch
            section[field_name] = env_value.strip().lower() in ("1", "true", "yes", "on")  # Parse common truthy tokens  # parse bool
        else:  # All other overrides are plain string values                                        # string branch
            section[field_name] = env_value  # Set the string value directly                        # set string
    return config_data  # Return the (possibly) modified config dictionary                          # return config
