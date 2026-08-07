####################################################################################################
# Project name      : Outlook Support Classification Agent -- Foundry v2 (LOCAL runner)            #
# Business owner    : <fill: business owner / team>                                                #
# Notebook Author   : <fill: author name / team>                                                   #
# Date              : <fill: date>                                                                 #
#                                                                                                  #
# Purpose of file:                                                                                 #
#   LOCAL test runner (NOT deployed). Calls the SAME code the Azure Function calls                 #
#   (get_turn_service().handle_turn_json) so you can drive the v2 agent from VS Code without       #
#   deploying or hitting the HTTP trigger. The agent connects to Azure AI Foundry via              #
#   DefaultAzureCredential (your az login / VS Code sign-in); KB selection runs in-process         #
#   from kb_index.json (no re-ranker, no HTTP loopback).                                           #
#                                                                                                  #
# USAGE (run from a terminal in VS Code):                                                          #
#   az login                                    # once, so DefaultAzureCredential has a token      #
#   pip install -r requirements.txt             # once, into your venv                             #
#   python run_local.py                         # interactive multi-turn chat (recommended)        #
#   python run_local.py "outlook stuck in outbox"          # single turn                           #
#   python run_local.py --conv-id conv_abc "desktop app"   # continue a conversation               #
#                                                                                                  #
# PREREQS: (1) kb_index.json present in THIS folder; (2) config_poc.yaml filled with your          #
#   Foundry project_endpoint + agent_name (clasification-agent-v2).                                #
#                                                                                                  #
# Source:-                                                                                         #
#   - turn_orchestrator.get_turn_service (this folder) -- builds + runs one classification turn.   #
#   - argparse / json / os / sys (stdlib) -- CLI parsing, pretty-print, path bootstrap.            #
####################################################################################################

# ============================================ Imports =============================================
from __future__ import annotations  # Postponed evaluation of type annotations (PEP 563)            # future import

import argparse  # Parse the optional message / --conv-id command-line arguments                    # stdlib argparse
import json  # Pretty-print the JSON response returned by the turn service                          # stdlib json
import os  # Build paths + check the config/index files exist before running                       # stdlib os
import sys  # Put this folder on sys.path so the sibling modules import cleanly                     # stdlib sys

# This folder holds the function's modules; add it to sys.path FIRST so `turn_orchestrator` and its
# siblings (runtime_config, foundry_agent_client, servicenow_kb_source, ...) import exactly as they
# do inside the Azure Functions worker. (Mirrors what __init__.py does.)
_THIS_FOLDER = os.path.dirname(os.path.abspath(__file__))  # Absolute path to this deployment folder  # this folder
sys.path.insert(0, _THIS_FOLDER)  # Make the sibling modules importable regardless of the cwd        # path bootstrap


# ================================= Preflight (fail fast, friendly) ===============================
def _preflight() -> None:  # Check the local files the service needs before building anything        # preflight
    """Verify kb_index.json is present; print a clear hint and exit if it is not."""
    index_path = os.path.join(_THIS_FOLDER, "kb_index.json")  # The ServiceNow-shaped KB the source loads  # index path
    if not os.path.isfile(index_path):  # The service cannot build without the index                 # missing?
        print(  # Friendly, actionable message (this is the #1 local gotcha)                         # print hint
            "ERROR: kb_index.json is not in this folder.\n"
            f"       Expected: {index_path}\n"
            "       Build it locally with `python ingest_kb_articles.py` (put KB####.docx in kb_source/),\n"
            "       or use the shipped sample, then re-run.",
            file=sys.stderr,
        )
        raise SystemExit(2)  # Non-zero exit                                                          # exit


# ============================================ One turn ============================================
def _run_once(turn_service, message: str, conv_id: str | None) -> dict:  # Send one turn, print reply  # one turn
    """Send one {conv_id?, message} turn through the service and pretty-print the JSON response.

    Args:
        turn_service: The cached ClassificationTurnService (from get_turn_service()).
        message: The user's message for this turn.
        conv_id: The conversation id to continue, or None to start a new conversation.

    Returns:
        The response dict (so the caller can read conv_id / chat_close).
    """
    payload: dict[str, object] = {"message": message}  # The JSON body -- same shape the HTTP trigger receives  # body
    if conv_id:  # Only include conv_id on follow-up turns                                           # have conv?
        payload["conv_id"] = conv_id  # Continue the existing Foundry conversation                   # set conv id
    response = turn_service.handle_turn_json(payload)  # SAME call the Azure Function makes (JSON in -> dict out)  # run turn
    print(json.dumps(response, indent=2, ensure_ascii=False))  # Pretty-print the response           # print json
    return response  # Return it for the interactive loop                                            # return


# =========================================== Entry point =========================================
def main() -> int:  # Local runner: single-shot (with a message) or interactive multi-turn           # entry point
    """Run the v2 classification agent locally: one turn if a message is given, else interactive chat."""
    parser = argparse.ArgumentParser(description="Run the Outlook classification agent v2 locally (VS Code).")  # CLI
    parser.add_argument("message", nargs="?", default=None, help="A single message to send (omit for interactive mode).")  # msg
    parser.add_argument("--conv-id", default=None, help="Continue an existing conversation id (optional).")  # conv id
    args = parser.parse_args()  # Parse the command line                                             # parse args

    _preflight()  # Fail fast if kb_index.json is missing                                            # preflight

    # Build the service once (reads config_poc.yaml; authenticates to Foundry via DefaultAzureCredential).
    from turn_orchestrator import get_turn_service  # Imported AFTER sys.path is set                 # import factory
    try:  # Guard the build so a config/auth problem prints a hint, not a raw traceback              # guard build
        turn_service = get_turn_service()  # Build + cache the turn service (Foundry gateway + KB source)  # build service
    except Exception as build_error:  # Any startup failure (bad config, not logged in, missing dep)  # build failed
        print(  # Point at the usual causes                                                          # print hint
            f"ERROR: could not start the service: {build_error}\n"
            "       Check: (1) `az login` done; (2) config_poc.yaml has your Foundry project_endpoint +\n"
            "       agent_name (clasification-agent-v2); (3) dependencies installed.",
            file=sys.stderr,
        )
        return 1  # Non-zero exit                                                                    # exit

    # --- Single-shot: a message was passed on the command line ---
    if args.message:  # Run exactly one turn and exit                                                # single-shot?
        _run_once(turn_service, args.message, args.conv_id)  # Send it                               # run once
        return 0  # Done                                                                             # exit

    # --- Interactive: multi-turn chat that reuses conv_id automatically ---
    conversation_id = args.conv_id  # Start from a supplied conv_id, or None (new conversation)      # conv id
    print("Interactive mode - type your Outlook issue, or 'exit' to quit.\n")  # Banner              # banner
    while True:  # Loop until the user exits                                                         # loop
        try:  # Read the next user message                                                           # read input
            user_message = input("you> ").strip()  # Prompt for input                                # prompt
        except (EOFError, KeyboardInterrupt):  # Ctrl-D / Ctrl-C -> quit cleanly                      # ctrl-c/d
            print()  # Newline for a tidy prompt                                                      # newline
            break  # Leave the loop                                                                   # break
        if not user_message:  # Ignore empty lines                                                    # empty?
            continue  # Ask again                                                                     # continue
        if user_message.lower() in ("exit", "quit"):  # Explicit exit                                 # exit word?
            break  # Leave the loop                                                                   # break
        response = _run_once(turn_service, user_message, conversation_id)  # Send the turn            # run turn
        conversation_id = response.get("conv_id") or conversation_id  # Reuse the conv_id next turn   # reuse conv
        if response.get("chat_close"):  # Resolved / no_match -> this conversation is finished        # closed?
            print("\n[conversation closed - your next message starts a fresh one]\n")  # Note it       # note
            conversation_id = None  # Next message begins a new conversation                          # reset conv
    return 0  # Clean exit                                                                           # exit


if __name__ == "__main__":  # Allow `python run_local.py`                                            # guard
    raise SystemExit(main())  # Exit with the return code                                             # run
