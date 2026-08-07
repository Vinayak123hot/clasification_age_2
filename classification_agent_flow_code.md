# Outlook Support Classification Agent (v2) — Code flow (from `main`)

A simple walkthrough of what the code does when a request arrives, starting at `main()`. Pair this
with **[classification_agent_flow.md](classification_agent_flow.md)** (the behaviour/feature view).

> **What's different in v2:** there is **no separate ranking step**. On each `search` the app hands
> the agent the **whole** knowledge base and the agent picks the article by reading it. One fewer
> model call; the outcomes and the response contract are unchanged from v1.

---

## 1. The one entry point — `main()`  ([`__init__.py`](__init__.py))

Every request lands in `main(req)`, which does three things:

1. **Read the request** — parse the JSON body `{ conv_id?, message }`. Bad JSON → **HTTP 400**.
2. **Hand it to the turn service** — `get_turn_service().handle_turn_json(body)`.
3. **Return JSON** — **200** on success, **400** if the body is missing `message`/malformed, **500**
   if the service can't start. An unexpected error never leaks — the caller always gets clean JSON.

Route: `POST /api/outlook-classification-v2`.

---

## 2. First request only — everything is built once

`get_turn_service()` builds the service **one time per worker** and caches it. That startup wires:

- **Config** ([`runtime_config.py`](runtime_config.py)) — reads `config_poc.yaml`.
- **Logging + cost tracking** ([`telemetry_logging.py`](telemetry_logging.py),
  [`model_cost_meter.py`](model_cost_meter.py)).
- **The Foundry agent client** ([`foundry_agent_client.py`](foundry_agent_client.py)) — talks to the
  agent (`clasification-agent-v2`).
- **The knowledge‑base source** ([`servicenow_kb_source.py`](servicenow_kb_source.py)) — loads the
  ServiceNow‑shaped `kb_index.json` (each record has a `number` and its full **"Article Content"**).
- **The orchestrator** ([`turn_orchestrator.py`](turn_orchestrator.py)) — ties them together.

Every later request reuses this cached service (fast, no rebuild).

---

## 3. One turn, step by step

`handle_turn_json` validates the body into a typed request, then `handle_turn` runs the turn:

1. **Identify the conversation.**
   - A `conv_id` came in → continue that conversation.
   - No `conv_id` → create a new conversation; the returned `conv_id` is the "session handle" the
     caller echoes back on later turns. (History is kept for us by Foundry, keyed by `conv_id`.)

2. **Run the turn loop** — `_run_turn_loop` (this is the heart of the code):
   - **a. Ask the agent** — send the user's message into the conversation and get the agent's reply.
   - **b. Read the reply** — the agent always replies with one strict JSON object.
   - **c. If the agent asks to *search*** → the code fetches the **whole** knowledge base
     (`get_all_candidates`), feeds every record back into the same conversation, and loops. **There is
     no ranking / shortlisting** — the agent reads the candidates and decides.
   - **d. If the agent *resolves*** → the code checks the article id is **real** (grounding guard);
     if yes, it returns the match; if the id was made up, it asks the agent again or hands off.
   - **e. If the agent asks a *follow‑up* or *hands off*** → the code returns that to the user.

3. **Return one response** for the turn (the agent's terminal reply only — the internal search
   steps are never shown to the user).

---

## 4. The safety rails, in the code

- **Grounding guard** — a resolved article id is accepted **only** if it exists in the knowledge
  base (`is_known_kb_id`); invented ids are rejected, so a fake article can never be returned.
- **Bounded loop** — at most a few knowledge‑base fetches per turn (`max_search_rounds`) plus a hard
  stop, so a turn can never loop forever or run up unbounded cost. (Because every fetch returns the
  whole KB, the agent typically resolves after a single search.)
- **Safe fallback** — any failure returns a polite message, never a stack trace or internal detail.

---

## 5. Where the knowledge base comes from

- **`kb_index.json`** — the ServiceNow‑shaped KB the agent reads from. Each record carries the
  article `number` (the id) and its full **"Article Content"** text.
- **[`ingest_kb_articles.py`](ingest_kb_articles.py)** — a **local, one‑time** builder (not
  deployed): it reads one `KB####.docx` per article, takes the id from the filename and the full text
  verbatim, and writes `kb_index.json`. No model is involved.
- **Production seam:** the `search` step is the only place that changes — swap the local source for
  the live **ServiceNow** KB search API (same record shape) and the agent, prompt, grounding, and
  contract stay identical.

---

## 6. Files at a glance

| File | Role |
|---|---|
| [`__init__.py`](__init__.py) | The HTTP entry point (`main`) — decode → delegate → JSON out. |
| [`turn_orchestrator.py`](turn_orchestrator.py) | The orchestrator — the turn loop, the grounding guard, response shaping, and the one‑time build of everything. |
| [`foundry_agent_client.py`](foundry_agent_client.py) | Talks to the Foundry agent (create conversation, send message, get reply). |
| [`servicenow_kb_source.py`](servicenow_kb_source.py) | Loads the KB (`kb_index.json`), returns **all** records, and validates a resolved id. **No ranker.** |
| [`service_contracts.py`](service_contracts.py) | The request and response shapes (validated at the edge). |
| [`runtime_config.py`](runtime_config.py) | Loads and validates configuration. |
| [`telemetry_logging.py`](telemetry_logging.py) · [`model_cost_meter.py`](model_cost_meter.py) · [`backoff_retry.py`](backoff_retry.py) · [`correlation_ids.py`](correlation_ids.py) | Logging, cost tracking, automatic retries, and request‑tracing ids. |
| [`ingest_kb_articles.py`](ingest_kb_articles.py) | Local one‑time builder of `kb_index.json` (not deployed). |

---

## 7. The whole path, at a glance

```
main(req)                                   __init__.py  — HTTP in / JSON out
 └─ get_turn_service()                       (builds everything once, then cached)
     └─ handle_turn_json(body)
         └─ handle_turn(request)
             ├─ start / continue conversation   → conv_id (session handle)
             └─ _run_turn_loop:
                 ├─ ask the Foundry agent        (foundry_agent_client)
                 ├─ read the agent's JSON reply
                 ├─ "search"    → feed the WHOLE KB back → loop   ← no ranker
                 │                 (servicenow_kb_source.get_all_candidates)
                 ├─ "resolved"  → grounding guard (is the article real?) → return the match
                 └─ "follow_up" / "no_match" → return the reply
 └─ HTTP response (one per turn)
```

**In one sentence:** `main` receives the message, the orchestrator asks the Foundry agent, the app
hands the agent the **entire** knowledge base whenever it asks to search, and — after checking the
chosen article is real — returns a single clean result: a question, a matched article, or a human
hand‑off.

---

## 8. Running it locally (no deploy)

Use [`run_local.py`](run_local.py) — it calls the **same** `get_turn_service().handle_turn_json`
that `main` calls, so you can drive the agent from VS Code without deploying. Auth to Foundry is via
`DefaultAzureCredential` (your `az login`). Interactive multi‑turn, or single‑shot:

```
az login
pip install -r requirements.txt
python run_local.py                       # interactive chat
python run_local.py "outlook stuck in outbox"    # one turn
```
