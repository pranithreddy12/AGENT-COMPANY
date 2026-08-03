# Phase 6 — Voice

**Gate:** a phone call updates the CRM and creates correctly-owned tasks with no manual step.

## Scope note (honest boundary)
The real-time stack (LiveKit/Twilio/Deepgram/ElevenLabs) needs external services + API keys not
available here. This phase builds the **testable, on-gate part**: the post-call pipeline
(transcript → structured summary → CRM update → correctly-owned follow-up tasks) behind a
**provider-abstracted voice seam**. `transcribe()`/`synthesize()` are the seam; the demo passes a
transcript directly and a `StubVoiceProvider` stands in. Real STT/TTS/telephony plug in behind the
seam without touching the pipeline.

## Entities
- `calls` (direction, from/to, account_id, lead_id, **consent**, recording_ref, transcript,
  extracted_fields, summary, outcome, follow_up_project_id).

## Post-call pipeline (`services/voice.py`, shares the same CRM + Task substrate as text agents)
1. **Extract** structured fields from the transcript (deterministic keyword extractor in demo; LLM
   behind the seam): budget/authority/need/timeline + commitments (action items) + outcome.
2. **CRM update** — create/update a `Lead` from the extracted fields and `qualify()` it (reuses Phase 5).
3. **Follow-up tasks** — each commitment becomes a Task **owned by the right department** (proposal→Sales,
   contract→Legal, scoping/demo→Development, onboarding/status→Client Management), under a follow-up
   Project. No manual step.
4. **Summary** — a structured post-call summary stored on the Call.

## Consent + recording (built in from day one)
`recording_ref` is stored only when `consent` is true; without consent it's dropped and an event notes it.

## Endpoints
- `POST /calls` — run the pipeline; returns the lead, its qualification state, the follow-up project,
  and the created tasks with their owners.
- `GET /calls/{id}` — call detail: summary, extracted fields, outcome.

## Tests
- a call transcript → Lead created/qualified + follow-up tasks routed to the correct departments.
- commitment→department routing is correct. consent=false drops the recording ref.

## Deferred (named)
Real-time pipeline (barge-in, endpointing, sub-800ms) behind the same seam. Twilio/Telnyx telephony,
Deepgram STT, ElevenLabs/Cartesia TTS — all keyed integrations for when credentials exist.
