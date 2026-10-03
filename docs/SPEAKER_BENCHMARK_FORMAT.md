# Offline speaker benchmark v1

`scripts/benchmark_speakers.py` evaluates **separate, independently annotated reference JSON and saved prediction JSON**. It does not run inference, fit calibration, decode audio, enroll participants, read settings/DB, or call any provider/device. Prepare/export the documents separately. Keep user documents in an ignored private directory; never put names, transcripts, quotes, audio, embeddings or features into this exchange. Synthetic inline test fixtures demonstrate arithmetic only.

```powershell
& '.\.venv\Scripts\python.exe' scripts/benchmark_speakers.py `
  --reference data/private/reference.json `
  --predictions data/private/predictions.json `
  --output data/private/new-report.json
```

Inputs must be regular `.json` files; explicit UNC paths are rejected for both input and output. Use local disks (the evaluator does not detect remotely mapped drives). Output defaults to stdout, or exclusively creates the explicitly supplied new `.json` file. It does not create parent directories or overwrite existing files. Reports are deterministic for the same documents: sorted keys, no timestamps or paths. Exit codes: `0` computed declared-domain PASS; `1` NEEDS_REVIEW; `2` NOT_QUALIFIED; `3` malformed input/read/write failure. Argparse usage errors use its standard exit `2` and produce no report. Validation errors are generic and do not echo input contents. A failed OS write may leave a partial newly created report; never treat that file as evidence when exit code is `3`.

## Strict exchange contract

Both roots have `format: "secretary-speaker-benchmark/v1"` and `meetings: [...]`. Reference root optionally has `evidence`; prediction root has no extra fields. Objects reject unlisted fields and duplicate JSON keys. IDs are opaque, canonical pseudonyms: `m_...` meeting, `p_...` participant, `t_...` turn, `a_...` task, `s_...` anonymous speaker. Suffix is 1–64 ASCII letters/digits/underscore/hyphen. This syntax reduces accidental disclosure but cannot prove identifiers contain no names; the exporter must assign opaque IDs. Participant IDs are **meeting-scoped**; global profile IDs require explicit conversion to that meeting roster.

Each input is limited to 16 MiB, 100 meetings, 100 participants per meeting and 100,000 total turns + tasks + speech intervals. Any individual array is capped at 100,000 entries. Numbers must be finite, nonnegative, nonboolean, and at most `1e12`. Recording durations must additionally be `>0` and `<=86400` seconds. Hashes are lowercase 64-character SHA256 declarations. Revisions use 1–128 ASCII letters/digits/`_.:-`; a model revision must identify the model and artifact version, not just a generic numeric version. Changing model/artifacts, calibration, roster/profile snapshot or microphone must change its revision. No scoring-time calibration fitting is provided.

Reference meeting fields (all required):

| Field | Contract |
|---|---|
| `meeting_id`, `transcript_version`, `source_hash` | Meeting ID, positive integer transcript version, declared source SHA256 |
| `duration_seconds` | Full recording timeline duration in seconds |
| `participants` | Objects `{participant_id, role}`; role `known`, `guest`, `unknown` |
| `turns` | Objects `{turn_id, participant_id, eligible, short, overlap}`; ID or null identity; three booleans |
| `speech` | Objects `{participant_id, start, end}`; canonical roster identity, `0 <= start < end <= duration_seconds` |
| `tasks` | Objects `{task_id, participant_id, source_turn_ids}`; owner ID or null; nonempty unique reference turn IDs |

`eligible` is determined independently by the annotator before examining predictions. It means a turn suitable for automatic known-person matching: a non-null `known` participant, neither short nor overlapping. A known turn can still be ineligible for other independently defined reasons such as poor audio. A guest turn is never eligible for this known-person metric; guest protection is scored separately. `short` is an independent annotation according to the declared evaluation protocol, not inferred from predicted text/timestamps. The evaluator does not verify the protocol itself. Every listed turn counts toward labeled sample size, including unknown, guest, short and overlap cases. Empty speech annotations mean no DER evidence. Unresolved speech identity cannot be null: use a meeting-scoped `p_...` with role `unknown`, or do not claim complete DER ground truth.

Speech timelines and turn annotations are deliberately separate: DER compares speech-time intervals and does not derive them from turn counts or assume turn ordinal alignment. Annotators/exporters are responsible for their semantic consistency. Same-person overlapping speech intervals are invalid; adjacent same-person intervals merge for collar calculation. Different-person overlaps are valid reference overlap and are excluded from DER.

Prediction meeting fields:

| Field | Contract |
|---|---|
| `meeting_id`, `transcript_version`, `source_hash` | Must exactly match the reference meeting/source/version |
| `turns` | `{turn_id, participant_id, status, method}`; status `proposed`, `confirmed`, `unknown`, `conflict`; method `automatic`, `manual` |
| `tasks` | `{task_id, participant_id, status, source_turn_ids}`; status `proposed`, `confirmed`, `needs_review` |
| `speech` | `{speaker_id, start, end}`; positive timeline intervals within the reference duration |
| `label_mode` | `canonical` or `anonymous_mapped` |
| `label_mapping` | Optional for canonical and must be empty; required to cover exactly the present anonymous labels in anonymous mode |
| `runtime_config` | Optional `{model_revision, calibration_revision, profile_revision, microphone_revision}`; required to match frozen config for qualification |
| `local_engine_elapsed_seconds` | Optional nonnegative elapsed seconds for one complete saved local engine run for this meeting |

Named turn predictions require non-null identity and status `proposed` or `confirmed`; unknown/conflict require null identity. A confirmed task requires a non-null owner. Source turn IDs must match the reference task's set exactly and exist in the same meeting. Task/turn IDs cannot be duplicated within their meeting. Prediction meetings, turns or tasks not in the reference are errors; missing ones are retained as missing, never silently dropped or aligned by ordinal. Use stable preassigned task IDs: the evaluator does not guess whether regenerated action text represents the same task.

For `canonical`, `speaker_id` is a canonical roster `p_...`; mappings are forbidden. This produces **identity-aware DER**, sensitive to an identity swap; it is not permutation-invariant anonymous clustering DER. For `anonymous_mapped`, `speaker_id` is an anonymous `s_...` and `label_mapping` explicitly maps **each and only each** observed anonymous label to a roster `p_...` or null (unmatched). Non-null mapping is injective, with at most 100 labels. Unmatched labels remain active hypothesis speech and contribute confusion/false alarms. The evaluator neither finds an optimal assignment nor silently remaps canonical names. Anonymous DER measures the supplied mapping; a poor mapping may increase error. Mapping does not alter named turn/task predictions or their identity scores. Reported per-meeting `label_mode` and report limitations make this distinction visible.

An abbreviated synthetic reference meeting shape (extend arrays and duration only from actual annotations):

```json
{
  "meeting_id": "m_001", "transcript_version": 1,
  "source_hash": "1111111111111111111111111111111111111111111111111111111111111111",
  "duration_seconds": 10,
  "participants": [{"participant_id": "p_001", "role": "known"}],
  "turns": [{"turn_id": "t_001", "participant_id": "p_001", "eligible": true, "short": false, "overlap": false}],
  "speech": [{"participant_id": "p_001", "start": 0, "end": 10}],
  "tasks": [{"task_id": "a_001", "participant_id": "p_001", "source_turn_ids": ["t_001"]}]
}
```

## Metrics and weighting

Name precision = correct **automatic** named predictions / all automatic named predictions on independently eligible reference turns. Coverage = automatic named predictions on eligible turns / all eligible reference turns. Automatic origin stays automatic when its saved status is confirmed; manually corrected predictions must be exported as manual and do not inflate automatic metrics. Preserve the original automated holdout result for quality evaluation.

Guest false accepts = guest turns automatically assigned to any roster `known` person / all reference guest turns. Unknown false accepts use the same definition for null-reference or role-unknown turns. Confusion counts cover all named predictions (manual and automatic separately), keyed by meeting, reference identity, predicted identity, method. Unknown/short/overlap/guest/eligible breakdowns have counts, missing predictions, automatic naming rates and precision; categories can overlap. Each turn has unit weight, regardless of duration.

Incorrect confirmed task owners = confirmed predictions whose owner differs from the independent reference owner / all confirmed task predictions. A null reference owner still makes a non-null confirmed predicted owner incorrect. Report reference/missing task counts as well; no confirmed tasks yields a null proportion, never perfect accuracy. This checks owner identities and source linkage, not whether the quoted obligation is semantically correct: quotes and task semantics are intentionally absent.

Every reported proportion has numerator, denominator, point estimate and two-sided 95% Wilson interval (`z=1.959963984540054`). Empty denominators produce null point estimate and interval. Zero observed guest errors still have a positive Wilson upper bound; it is not proof of zero population risk. Gate thresholds use point estimates, not interval bounds. Elapsed aggregates have saved-run count, missing count, total, mean, min/max and nearest-rank p50/p95. They are declared saved local measurements, not profiler-verified performance or provider latency.

## Exact DER definition

Time is measured over the full `[0, duration_seconds)` recording, including silence for false-alarm scoring. The evaluator uses an event sweep; intervals are half-open. Adjacent intervals of the same reference speaker merge. A **250 ms collar on each side** of every resulting reference speech onset/offset and reference speaker boundary is excluded, clipped to recording bounds. Thus a boundary excludes up to 500 ms total; recording onset/offset typically exclude 250 ms each. Silence inside collars is excluded too. Reference intervals with more than one active speaker are excluded; overlap exclusions take precedence over collars so exclusion counters partition time without double-counting. Hypothesis overlap on reference singleton speech/silence is scored and may add false alarms.

On each remaining time cell of duration `dt`, let `Nref` be active reference speakers (0 or 1), `Nhyp` active hypothesis speakers and `Ncorrect` the count of matching labels after the explicit mapping:

```text
miss_seconds        += max(0, Nref - Nhyp) * dt
false_alarm_seconds += max(0, Nhyp - Nref) * dt
confusion_seconds   += (min(Nref, Nhyp) - Ncorrect) * dt
reference_seconds   += Nref * dt
DER = (miss_seconds + false_alarm_seconds + confusion_seconds) / reference_seconds
```

Aggregate DER is the sum of component durations across meetings divided by summed scored reference speech duration, **not** an unweighted mean of meeting DERs. DER can exceed 1 because false alarms include reference silence. No scored reference speech produces null DER. DER is duration-weighted, unlike turn-level names, guests and task proportions. This explicitly defined collar/exclusion/mapping protocol is not a claim of bit-for-bit parity with an external scoring package.

Hand cases in tests: reference `[0,10)` has 9.5 scored seconds; perfect hypothesis gives 0, all missing gives 1, full identity swap gives 1. Reference `[0,2)` with a `[0,10)` hypothesis has 1.5 scored reference seconds and 7.75 false-alarm seconds. A 200 ms misplaced boundary falls inside the 250 ms collars and contributes 0. Reference A `[0,6)`, B `[4,10)` has 2 excluded overlap seconds and 7 scored singleton seconds. Reference `[0,0.4)` has no scored time and null DER.

## Evidence and qualification

Optional reference `evidence` has all these fields when present:

```text
kind: real | synthetic | unspecified
human_annotations: boolean
independent_holdout: boolean
enrollment_hashes: [sha256, ...]
calibration_hashes: [sha256, ...]
holdout_hashes: [sha256, ...]
frozen_config: {model_revision, calibration_revision, profile_revision, microphone_revision}
```

Freeze calibration using calibration data **before** independent holdout evaluation. The three source-hash sets must be nonempty, duplicate-free and pairwise disjoint; holdout must equal the reference meeting hashes. Duplicate source hashes across meetings cannot satisfy the two-meeting requirement. Hash overlap yields `declared_source_contamination`; a changed/missing runtime config yields NOT_QUALIFIED. Crops/re-encodings of the same original recording must declare the original parent source hash for partition checks. SHA256 of differing derivatives cannot establish independence; this tool does not discover concealed common source ancestry or threshold tuning on holdout.

NOT_QUALIFIED means missing/insufficient supporting evidence: no real/human/independent declarations, partition mismatch/contamination, incompatible frozen runtime revisions, fewer than two distinct-source meetings, less than 900 full-recording seconds, fewer than 150 labeled turns including 30 guest turns, no eligible turns/name proposals, any meeting without DER-scored reference time, no labeled tasks, or missing task predictions. This status takes precedence over failing thresholds and still shows computed failures. Omitted entire evidence is valid and produces NOT_QUALIFIED; a present incomplete/malformed evidence object is a format error. Synthetic/unspecified data cannot qualify by default.

With sufficient declared evidence, any name precision `<0.98`, coverage `<0.80`, observed guest false accept, aggregate DER `>0.15`, or incorrect confirmed task owner gives NEEDS_REVIEW. PASS is limited to the **computed metrics on the declared holdout**, with sample sizes and uncertainty shown. Zero confirmed tasks means zero observed incorrect confirmations with no measured confirmation accuracy; it does not certify task-owner automation. Zero task reference cases cannot qualify.

Neither PASS nor source metadata cryptographically proves independent source material, human annotation, consent, four real voices, common-room microphone conditions, similar voices, changed seating/order, a late guest, noise, overlaps, real device capture or live provider behavior. Those require separate human/hardware/provider evidence. This tool consumes no recordings and creates no ground-truth labels.
