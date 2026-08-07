# Outlook Support Classification Agent — Foundry **v2** (ServiceNow-shaped KB)

One Azure Functions app that runs the **Microsoft Foundry classification agent** and, on
each `search`, hands the agent the **whole local knowledge base** (shaped exactly like the
ServiceNow KB search API) so the **agent itself** picks the matching article — **no LLM
re-ranker**. A single endpoint, a single config, no separate tool service, no HTTP loopback.

- **Endpoint:** `POST /api/outlook-classification-v2`
- **Input:** `{ "conv_id"?: str, "message": str }`
- **Output:** `{ "conv_id", "status", "agent_message", "kb_id", "summary", "chat_close" }`
- **State:** the Foundry **conversation** holds history server-side; `conv_id` is the stable handle. **No database.**
- **Auth:** Entra ID / Managed Identity end-to-end (**no API keys**).
- **Foundry agent:** `clasification-agent-v2` (provision with the v2 prompt in this folder).

---

## 1. What changed vs v1 (and what didn't)

**Changed**
- **KB shape:** `kb_index.json` now mirrors the **ServiceNow** search-result structure — a list of
  records, each with a `columns[]` array carrying `number` (the KB id) and the full **"Article
  Content"** text. Built by the local one-time `ingest_kb_articles.py`.
- **No re-ranker:** on `status:"search"` the function returns **every** local KB record to the agent
  (`servicenow_kb_source.get_all_candidates`) and the **agent selects by reading the content**. The
  Azure OpenAI ranker (`chat_client`) and its config are **gone**.
- **Professional module names** (see §4) and a distinct **route/agent name** so v2 co-exists with v1.

**Unchanged (by design)**
- The **response contract**, the four agent statuses, the strict-JSON output rules, the
  anti-hallucination grounding (a resolved `kb_id` must be a real `number` in the index), the
  off-topic/wind-down behaviour, and the wording rules (`resolved` → a professional thank-you that
  never mentions an article; `summary` → the user's own issue, lightly polished; never invented).

**POC ⇆ production seam:** in production, the `search` step is a **ServiceNow KB search API** call
that returns a *filtered subset* in the **same record shape**. Only `servicenow_kb_source` changes
when you switch; the agent, prompt, grounding and response stay identical.

---

## 2. How it works

```
1. Caller sends { message }                    (first turn: no conv_id)
2. App creates a Foundry conversation → conv_id (returned to the caller)
3. App sends the message to the agent
4. Agent replies with STRICT JSON, one of:
      status="search"    → "run a KB search for me"   (INTERNAL — never shown to the user)
      status="follow_up" → a clarifying question        (shown; chat stays open)
      status="resolved"  → a matched article (kb_id)    (shown as a thank-you; chat closes)
      status="no_match"  → no article; human handoff     (shown; chat closes)
5. If "search": the app feeds the WHOLE local KB (ServiceNow-shaped) back into the SAME
   conversation — NO re-ranker — and the agent decides (follow_up / resolved / no_match).
6. The app returns ONE HTTP response per turn — the terminal reply only.
```

`status="search"` is a message to **our code**, never to the user. The user only ever sees
`follow_up`, `resolved`, or `no_match`. Because every `search` returns the full KB, the agent
effectively classifies after one search; the `max_search_rounds` budget still guards the loop.

---

## 3. Request / response contract

### First turn (no `conv_id`)
```json
POST /api/outlook-classification-v2
{ "message": "outlook email stuck in outbox" }
```
```json
200 OK
{ "conv_id": "conv_abc123", "status": "resolved",
  "agent_message": "Thank you for the details — I'm working on this for you now; please bear with me.",
  "kb_id": "KB0024755", "summary": "Emails are stuck in the Outbox and won't send.",
  "chat_close": true }
```
`agent_message` never mentions an article; `kb_id` is the internal routing id (the article's
`number`); `summary` is the **user's own issue**, lightly polished.

### Follow-up turn (echo the `conv_id`)
```json
{ "conv_id": "conv_abc123", "status": "follow_up",
  "agent_message": "Is this the Outlook desktop app or Outlook on the web?",
  "kb_id": null, "summary": null, "chat_close": false }
```

### No-match (human handoff)
```json
{ "conv_id": "conv_abc123", "status": "no_match",
  "agent_message": "I don't have the information I need to help with syncing your Outlook calendar with an external app right now — one of our team members will reach out to help you.",
  "kb_id": null, "summary": "Wants to sync their Outlook calendar with an external app.",
  "chat_close": true }
```

**Rule:** start a new issue by sending a message **without** `conv_id`; continue by echoing the
**same** `conv_id` until `chat_close: true`. Off-topic/misuse: the agent steers back on every turn
and, after ~8–10 unproductive turns (or clear misuse), ends politely with a `no_match` wind-down.

---

## 4. File-by-file reference (v2 module names)

### Entry + orchestration
- **`__init__.py`** — the single Azure Function (`main`, route `outlook-classification-v2`). Decodes
  the body (bad → 400), calls `get_turn_service().handle_turn_json(body)`, maps `ValidationError` →
  400 and any build/config error → 500, else 200.
- **`turn_orchestrator.py`** — `ClassificationTurnService` (the brain). Resolves the conversation,
  runs the agent loop, and on `search` feeds the **whole KB** back (no ranker). Enforces the
  grounding guard (`is_known_kb_id` on the resolved `number`), parses strict JSON, shapes the
  response, and builds/caches the service (`_build_turn_service` / `get_turn_service`).

### Foundry + KB source
- **`foundry_agent_client.py`** — `FoundryAgentGateway`: `AIProjectClient` + Responses API.
  `create_conversation()` mints the `conv_id`; `create_response()` calls the agent via
  `agent_reference` over the conversation. Bounded timeout + retry + cost. Auth = Managed Identity;
  Foundry token scope `https://ai.azure.com/.default`.
- **`servicenow_kb_source.py`** — `KnowledgeBaseSource`: loads `kb_index.json` (ServiceNow shape),
  `get_all_candidates()` returns **every** record (the POC stand-in for the API), `is_known_kb_id()`
  validates a resolved `number`. **No LLM, no scoring.**

### Contracts + config + cross-cutting
- **`service_contracts.py`** — Pydantic boundary models: `AgentEntryRequest`,
  `AgentStructuredOutput`, `AgentEntryResponse`. (KB records are handled as raw dicts, not models.)
- **`runtime_config.py`** — typed config loader (`load_settings`, `resolve_path`, `AppSettings`, …).
  **No `azure_openai` section** (no ranker).
- **`telemetry_logging.py`** — structured JSON logging (`StructuredLogger`, `LogFactory`,
  `EventHubLogEmitter`).
- **`backoff_retry.py`** — `run_with_retry` (exponential backoff + jitter).
- **`model_cost_meter.py`** — `CostTracker` (per-token cost logging for the agent calls).
- **`correlation_ids.py`** — `generate_correlation_id` (bootstrap log key before `conv_id` exists).

### Ingestion (local, one-time) + data
- **`ingest_kb_articles.py`** — reads one `KB####.docx` per article from `kb_source/`, takes the KB
  **number from the filename** and the **full text verbatim** (python-docx), and writes
  `kb_index.json` in the ServiceNow shape. **No LLM.** Run once locally; **not deployed**.
- **`kb_index.json`** — the ServiceNow-shaped KB (ships with a 2-article sample; regenerate from
  your own `.docx`).
- **`foundry_classification_agent_system_prompt.txt`** — the v2 agent prompt (candidate shape =
  `number` + "Article Content", no score). Provision the Foundry agent from this.
- **`config_poc.yaml`**, **`function.json`**, **`requirements.txt`**, **`.funcignore`**,
  **`local.settings.json`** — config / binding / deps / ignore / local-run settings.

---

## 5. Call flow from `main()`

```
HTTP POST /api/outlook-classification-v2
│
├─ __init__.py  main(req)  → { "message": "...", "conv_id"? }
│    ├─ turn_orchestrator.get_turn_service()  (first call → _build_turn_service)
│    │     runtime_config.load_settings → telemetry_logging → model_cost_meter
│    │     → foundry_agent_client.FoundryAgentGateway
│    │     → servicenow_kb_source.KnowledgeBaseSource (loads kb_index.json)
│    │     → ClassificationTurnService
│    └─ handle_turn_json(body)
│         ├─ service_contracts.AgentEntryRequest.model_validate(body)
│         └─ handle_turn(request)
│              ├─ (no conv_id + auto_create) foundry_agent_client.create_conversation() → conv_id
│              └─ _run_turn_loop(conv_id, message, log_id)              ◄── LOOP ──┐
│                   ├─ foundry_agent_client.create_response(input, conv_id)         │
│                   ├─ _parse_agent_output → service_contracts.AgentStructuredOutput │
│                   ├─ if status == "search":                                       │
│                   │      candidates = servicenow_kb_source.get_all_candidates()   │  ← whole KB, no ranker
│                   │      _format_candidates → next input ─────────────────────────┘
│                   ├─ if status == "resolved":
│                   │      guard: servicenow_kb_source.is_known_kb_id(number)?  yes → resolved / no → nudge/no_match
│                   └─ follow_up / no_match → _build_response_from_output
│         └─ AgentEntryResponse.model_dump()
└─ HttpResponse(json)
```

---

## 6. Configuration (`config_poc.yaml`)

| Section | Key | Meaning |
|---|---|---|
| `knowledge_base` | `index_path` | `kb_index.json` (resolved to this folder) |
| `foundry` | `project_endpoint` | `…/api/projects/<project>` (fill in) |
| | `agent_name` | **`clasification-agent-v2`** (the new agent) |
| | `agent_version` | `"latest"`, a number, or `""` |
| | `auto_create_session` | `true`: mint a `conv_id` when absent |
| | `max_search_rounds` | per-turn search budget (6) |
| `event_hub` / `retry` / `cost` / `logging` | — | logging sink, retry policy, cost prices, log level |

There is **no `azure_openai` section** in v2 (the ranker is gone). Env overrides: `APP_ENV`,
`FOUNDRY_PROJECT_ENDPOINT`, `FOUNDRY_AGENT_NAME`, `FOUNDRY_AGENT_VERSION`, `FOUNDRY_AUTO_CREATE_SESSION`.

---

## 7. Build & deploy

**Managed Identity role:** **Azure AI User** on the Foundry project. *(No Cognitive Services OpenAI
role is needed anymore — there is no ranker.)*

1. **Build the KB index:** put `KB####.docx` files in `kb_source/`, then locally:
   `pip install python-docx` and `python ingest_kb_articles.py` → verify `kb_index.json`.
   *(Or use the shipped 2-article sample to test first.)*
2. **Provision the agent:** create Foundry agent **`clasification-agent-v2`** with
   `foundry_classification_agent_system_prompt.txt`; set `foundry.project_endpoint` in the config.
3. **Deploy** the folder as one Azure Function (your pipeline supplies `host.json`).

**Test (Postman)**
```
POST https://<app>.azurewebsites.net/api/outlook-classification-v2?code=<FUNCTION_KEY>
{ "message": "outlook email stuck in outbox" }
```
Copy `conv_id` from the response; send the next turn with `{ "conv_id": "...", "message": "..." }`.

---

## 8. Design notes

- **Collision-free modules:** all module names are unique (professional names, no `_v2` suffix in
  code) so v2 co-deploys with v1 in the same Function App without Python import clashes.
- **No re-ranker:** the Foundry agent reads the full candidate content and selects — one fewer model
  call, no scoring heuristics. Fine for a modest POC KB; for a large KB, switch `servicenow_kb_source`
  to the real ServiceNow API (same shape).
- **Anti-hallucination:** a `resolved` `kb_id` is accepted only if it's a real `number` in the index
  (`is_known_kb_id`); the `summary` is always the user's own issue (never the article text or invented).
- **Verify before deploy:** run `python -m py_compile *.py` in your environment (no interpreter was
  available where this was authored, so it was verified structurally — banners, imports, JSON).
