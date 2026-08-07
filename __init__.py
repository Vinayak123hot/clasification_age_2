####################################################################################################
# Project name      : Outlook Support Classification Agent -- Foundry v2 (ServiceNow-shaped KB)    #
# Business owner    : <fill: business owner / team>                                                #
# Notebook Author   : <fill: author name / team>                                                   #
# Date              : <fill: date>                                                                 #
#                                                                                                  #
# Purpose of file:                                                                                 #
# HTTPS-triggered Azure Function - the single entry point for the v2 classification service.       #
#   1. Decodes the JSON body {conv_id?, message} (bad body -> HTTP 400).                           #
#   2. Delegates the turn to the turn orchestrator (get_turn_service().handle_turn_json).          #
#   3. Maps the outcome to HTTP 200 (agent JSON), 400 (bad/schema), or 500 (build/config).         #
#                                                                                                  #
# Source:-                                                                                         #
#   - From turn_orchestrator get_turn_service is imported.                                         #
#       - get_turn_service:- builds once per worker and returns the cached turn service that       #
#         drives the agent + in-process KB selection.                                              #
#   - pydantic.ValidationError is caught to map a schema-invalid body to HTTP 400.                 #
#   - azure.functions supplies the HttpRequest / HttpResponse types for the v1 HTTP trigger.       #
####################################################################################################

# ============================================ Imports =============================================
from __future__ import annotations  # Enable postponed evaluation of type annotations (PEP 563)   # future import

import json  # Serialise the JSON response / error bodies                                          # stdlib json
import os  # Derive this file's folder for the import-path bootstrap                               # stdlib os
import sys  # Put this function folder on sys.path so sibling modules import cleanly               # stdlib sys

import azure.functions as func  # Azure Functions SDK: HttpRequest / HttpResponse types            # functions sdk
from pydantic import ValidationError  # Raised when the request body fails schema validation -> 400 # boundary error

# Flat function folder: add this folder to sys.path BEFORE the local imports so the uniquely
# named sibling modules resolve in the Functions worker (no clash with the v1 deployment).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # Make sibling modules importable  # path bootstrap

from turn_orchestrator import get_turn_service  # Cached service that drives one classification turn  # service factory


# ====================================== HTTP entry point (JSON) ===================================
def main(req: func.HttpRequest) -> func.HttpResponse:  # v1 entry point named by function.json (entryPoint=main)
    """Handle one HTTP request: decode {conv_id?, message} -> run one turn -> JSON.

    What this function is:
        - The single HTTP surface of the v2 classification service. It owns request decoding and
          response shaping only; every routing / KB-selection decision lives in the turn orchestrator.

    Why this function exists:
        - To give the calling application one stable endpoint that always returns JSON, and to map
          failures to the right HTTP status (400 / 500) without leaking a stack trace to the caller.
          Foundry keeps the conversation history under `conv_id`; KB selection runs in-process.

    Args:
        req: The incoming HTTP request carrying a JSON body ({conv_id?, message}).

    Returns:
        An HttpResponse whose body is the JSON-serialised AgentEntryResponse (200), or a JSON error
        payload for a malformed / schema-invalid body (400) or an unexpected internal failure (500).

    Example:
        POST /api/outlook-classification-v2  {"message": "Outlook crashes on send"}
        -> 200 {"conv_id": "conv_abc", "status": "follow_up", ...}
    """
    # --- Decode the request body as JSON (a malformed body is a client error -> HTTP 400) ---
    try:  # Attempt to decode the request body as JSON                                             # parse body
        request_body = req.get_json()  # Parse the JSON request body into a dict                   # decode json
    except ValueError:  # The body was missing or not valid JSON                                   # bad body
        bad_request_payload = {  # Build a JSON error payload describing the bad request            # 400 payload
            "status": "error",  # Mark the outcome as an error                                     # status
            "agent_message": "Request body must be valid JSON containing a 'message' field.",  # hint  # user message
        }
        return func.HttpResponse(  # Return a 400 Bad Request with a JSON body                     # respond 400
            json.dumps(bad_request_payload),  # Serialise the error payload to JSON                 # json dump
            status_code=400,  # HTTP 400 Bad Request                                               # status
            mimetype="application/json",  # Declare the JSON content type                          # mimetype
        )

    # --- Build (or reuse) the turn service and process the turn ---
    try:  # Build (or reuse) the turn service and process the turn                                 # run turn
        turn_service = get_turn_service()  # Obtain the cached turn service (built once per worker)  # get service
        response_dict = turn_service.handle_turn_json(request_body)  # Run one turn -> JSON-serialisable dict  # run turn
    except ValidationError as validation_error:  # The JSON body failed schema validation -> 400   # schema error
        invalid_request_payload = {  # Build a JSON error payload describing the invalid request     # 400 payload
            "status": "error",  # Mark the outcome as an error                                     # status
            "agent_message": "Request JSON is missing required fields (e.g. 'message') or is malformed.",  # hint  # user message
            "error_type": type(validation_error).__name__,  # Exception class name for diagnostics  # error type
        }
        return func.HttpResponse(  # Return a 400 Bad Request with a JSON body                     # respond 400
            json.dumps(invalid_request_payload),  # Serialise the error payload to JSON             # json dump
            status_code=400,  # HTTP 400 Bad Request                                               # status
            mimetype="application/json",  # Declare the JSON content type                          # mimetype
        )
    except Exception as service_error:  # Guard against service-build / config failures            # internal error
        server_error_payload = {  # Build a JSON error payload for an internal failure             # 500 payload
            "status": "error",  # Mark the outcome as an error                                     # status
            "agent_message": "The service is not available right now. Please try again later.",  # message  # user message
            "error_type": type(service_error).__name__,  # Exception class name for diagnostics    # error type
        }
        return func.HttpResponse(  # Return a 500 Internal Server Error with a JSON body            # respond 500
            json.dumps(server_error_payload),  # Serialise the error payload to JSON                # json dump
            status_code=500,  # HTTP 500 Internal Server Error                                     # status
            mimetype="application/json",  # Declare the JSON content type                          # mimetype
        )

    # --- Success: return the agent's response as JSON (HTTP 200) ---
    return func.HttpResponse(  # Return the agent's response as JSON                                # respond 200
        json.dumps(response_dict),  # Serialise the AgentEntryResponse dict to JSON                 # json dump
        status_code=200,  # HTTP 200 OK                                                            # status
        mimetype="application/json",  # Declare the JSON content type                              # mimetype
    )
