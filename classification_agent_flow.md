# Outlook Support Classification Agent (v2) — How it works

An AI assistant that reads a user's Outlook problem in plain English, asks a short clarifying
question when needed, finds the right knowledge‑base (KB) article, and hands it off — or, when no
article fits, routes the case to a support team member. Every match is a **real** article; the
agent never makes one up.

> **v2 in one line:** the behaviour and contract are identical to v1 — the difference is *internal*.
> Instead of a separate ranking step pre‑selecting a shortlist, the agent is handed the **whole
> knowledge base** and picks the matching article by reading it. Fewer moving parts, same outcomes.

**At a glance**

| | |
|---|---|
| **Endpoint** | `POST /api/outlook-classification-v2` |
| **Input** | a user's message (plain English) |
| **Output** | one of: a matched article, a clarifying question, or a human hand‑off |
| **Memory** | the conversation is remembered automatically (a `conv_id` handle) — no database |
| **Security** | Microsoft Entra ID / Managed Identity — **no API keys anywhere** |

---

## The idea in one line

A user describes an Outlook issue → the agent understands it, checks the knowledge base, and
returns **exactly one** outcome: a matched **article**, a **clarifying question**, or a polite
**human hand‑off**. It only ever answers with a real article, and the user always sees a friendly,
plain message — never any system internals.

---

## The conversation, step by step

1. **User sends a message** — e.g. *"outlook email stuck in outbox."*
2. **The agent understands the issue** — if it's unclear, it asks **one** short question.
3. **The agent checks the knowledge base** for the best‑matching article.
4. **The agent responds** with one of the three outcomes below.
5. **The conversation continues** (the user replies, the agent refines) until the issue is
   **resolved** or **handed off**.

---

## The three outcomes the user sees

| Outcome | What it means | What the user sees |
|---|---|---|
| **Clarifying question** | The agent needs a little more detail. | A short question; the chat stays open. |
| **Resolved** | The agent found the right article. | A warm thank‑you — *"Thank you for the details — I'm working on this for you now; please bear with me."* The article is routed on internally; the user is **not** shown KB internals. The chat closes. |
| **Human hand‑off** | No article fits the request. | A polite message that **names the user's task** and says a team member will reach out. The chat closes. |

---

## Example conversations

**Resolved**
> **User:** "outlook email stuck in outbox"
> **Agent:** "Thank you for the details — I'm working on this for you now; please bear with me."
> *(The matching article is identified and routed on behind the scenes.)*

**Human hand‑off**
> **User:** "how do I sync my Outlook calendar with an external app"
> **Agent:** "I don't have the information I need to help with syncing your Outlook calendar with an
> external app right now — one of our team members will reach out to help you."

**Clarifying question**
> **User:** "signature"
> **Agent:** "Sure — what would you like to do with your Outlook signature, or what's happening
> with it right now?"

---

## For integrators — request & response

**Request** — start a new issue with no `conv_id`; continue by sending the **same** `conv_id` back
each turn until `chat_close: true`.
```json
{ "message": "outlook email stuck in outbox" }
```

**Resolved response**
```json
{ "conv_id": "conv_abc123", "status": "resolved",
  "agent_message": "Thank you for the details — I'm working on this for you now; please bear with me.",
  "kb_id": "KB0024755",
  "summary": "Emails are stuck in the Outbox and won't send.",
  "chat_close": true }
```

**Clarifying‑question response**
```json
{ "conv_id": "conv_abc123", "status": "follow_up",
  "agent_message": "Is this the Outlook desktop app or Outlook on the web?",
  "kb_id": null, "summary": null, "chat_close": false }
```

**Human hand‑off response**
```json
{ "conv_id": "conv_abc123", "status": "no_match",
  "agent_message": "I don't have the information I need to help with syncing your Outlook calendar with an external app right now — one of our team members will reach out to help you.",
  "kb_id": null,
  "summary": "Wants to sync their Outlook calendar with an external app.",
  "chat_close": true }
```

- `agent_message` — the friendly text shown to the user (never mentions KB articles).
- `kb_id` — the matched article, used internally for routing (not shown to the user).
- `summary` — the user's **own** issue, lightly cleaned up for spelling/grammar (never invented).
- `chat_close` — `true` when the issue is resolved or handed off.

---

## Built‑in guardrails (why it's safe)

- **Only real articles.** A match is accepted **only** if it exists in the knowledge base — the
  agent can never return a made‑up article.
- **Honest summaries.** The case summary comes from the **user's own words**, lightly polished —
  never invented from the article or the model's knowledge.
- **Stays on topic.** If the user drifts off‑topic, the agent politely steers back. After repeated
  off‑topic messages (or clear misuse), it ends the chat politely and invites a fresh start.
- **No internals leaked.** The user never sees article ids, scores, or any system detail.
- **Secure by design.** Authentication is Microsoft Entra ID / Managed Identity — no API keys.

---

## Where the answers come from

The knowledge base is a curated set of Outlook support articles, shaped exactly like the results
of the production **ServiceNow** article search (each record carries the article number and its
full text). In v2 the agent is given that **whole** set and selects the single best article by
reading it (or hands off when nothing fits) — there is no separate ranking step. The knowledge
base can be refreshed independently of the agent, and for a large catalogue this local set is
simply swapped for the live ServiceNow search — the agent, contract, and guardrails stay the same.
