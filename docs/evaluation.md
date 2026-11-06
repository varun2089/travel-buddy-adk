# Policy Evaluation

The 12 cases in `travel_agent/evaluation/cases.json` define questions,
expected evidence, required behaviour and forbidden claims/actions.

## Run

Load database settings from the trusted root `.env`. The sample policy must already
be ingested and active. Run from the repository root with the virtual environment active.

```sh
set -a
source ./.env
set +a
export ADK_DISABLE_LOAD_DOTENV=true
```

Vertex AI authenticates separately through Application Default Credentials (ADC).
For local use, run `gcloud auth application-default login`; the ADC identity needs
model access in project `gen-lang-client-0842015305`. Generation uses `global`
and embeddings use `us-central1`, as configured in source. See [policy setup](policy-rag.md#setup).
Do not source `travel_agent/.env`; database settings come from the root `.env`.

Evaluate retrieval:

```sh
travel-policy-evaluate retrieval --live --limit 3
```

Evaluate one answer:

```sh
travel-policy-evaluate answer --live --case review-draft --limit 1
```

Run all cases:

```sh
travel-policy-evaluate answer --live --limit 12
```

Live calls may incur charges and consume quota. Answer mode uses a fresh
session per case, allows read-only tools and limits each invocation to
five model calls. It does not create or submit proposals.

## Results

Each run saves files under `evaluations/results/`:

| File | Purpose |
| --- | --- |
| `run.json` | Dataset and model configuration |
| `results.jsonl` | Answers, evidence, timing, usage and checks |
| `summary.json` | Aggregate metrics and execution failures |
| `human-review.csv` | Evidence and answers for manual review |
| `report.md` | Readable summary, case details and pending human review |

Checks measure expected-evidence recall, citation validity, missing
answers and missing required citations. Service failures and timeouts
are reported separately.

**Valid citations do not prove claim support.** Review each answer
against its evidence. Semantic correctness remains pending
until human assessment. Exit code zero indicates no execution failure,
not that answer quality passed.

Results are excluded from Git. Review them before sharing.

New runs automatically create `report.md`. Regenerate it from an existing run
without API or database calls:

```sh
.venv/bin/python -m travel_agent.evaluation.report evaluations/results/RUN_DIRECTORY
```

This replaces only `report.md`; saved JSON and CSV files remain unchanged.
The report includes saved questions, answers and evidence, so treat it with the
same privacy precautions as the JSON/CSV exports. It does not include tool
arguments, credentials or environment settings, and does not print case content
to stdout or stderr.

Execution completion and citation validity do not establish semantic correctness.
Human review stays pending until a reviewer records a verdict in the
`semantic_correctness` column of `human-review.csv`. Record evidence support and
notes in `evidence_support` and `reviewer_notes`, then regenerate the report.
These recorded verdicts are displayed separately and never alter automated scores.

## Offline Tests

```sh
.venv/bin/python -m unittest discover -s tests -p 'test_policy_evaluation.py' -v
```

Mocks test the evaluation harness; they do not establish real-model quality.
