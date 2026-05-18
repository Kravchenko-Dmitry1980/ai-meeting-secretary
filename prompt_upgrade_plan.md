# Prompt Upgrade Plan

## Priority 0 (Today, low-risk)

1. **Harden JSON parsing in task extraction**
   - Add `try/except` around `json.loads`.
   - Validate `tasks` is a list.
   - Sanitize `confidence` to `[0,1]` or `None`.
   - On parse failure return safe `[]` + warning log.

2. **Set summary temperature to 0.0**
   - Improve determinism for enterprise protocol output.

3. **Add anti-hallucination line to both prompts**
   - "Используй только факты из расшифровки."

## Priority 1 (This sprint)

1. **Move summary to strict JSON contract**
   - Target schema:
     - `summary` (string)
     - `key_decisions` (string[])
     - `risks` (string[])
     - `next_steps` (string[])
   - Validate schema server-side before save.

2. **Add RU language guard**
   - Lightweight language check after generation.
   - If non-RU -> single retry with strict RU repair prompt.

3. **Task extraction quality constraints**
   - Enforce:
     - action-oriented descriptions
     - deduplication
     - assignee inference rules
     - due date normalization (ISO)

## Priority 2 (Scalability & cost)

1. **Chunked prompt strategy**
   - Long transcript -> chunk extraction -> merge pass.
   - Reduces token spend and improves stability.

2. **Salience filtering**
   - Feed only high-signal segments to task extractor.

3. **Observability**
   - Log per-call metrics:
     - input tokens
     - output tokens
     - parse success rate
     - language compliance rate

## Priority 3 (Quality excellence)

1. **Few-shot exemplars for task extraction**
   - Add 1-2 compact examples for implicit task patterns.

2. **Centralized prompt registry**
   - Keep all prompt templates in one module.
   - Version prompts and track A/B changes.

3. **Evaluation harness**
   - Golden set of real meetings.
   - Regression checks for:
     - RU compliance
     - task recall/precision
     - summary usefulness

## Acceptance Criteria

1. `summary` always generated in Russian.
2. `tasks` parse failures < 1% on production traffic.
3. Average token usage reduced by at least 20% on long meetings.
4. No pipeline crashes due to malformed LLM output.
5. Business users confirm improved actionability of extracted tasks.
