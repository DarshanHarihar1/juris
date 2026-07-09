# Juris v2 Golden-Claim Eval

Run from `backend/`:

```bash
python -m eval.runner --dry-run
python -m eval.runner --bucket slow_changing --limit 3
```

The runner loads `golden_claims.json`, calls the in-process v2 path
`normalize -> verify -> synthesize`, and scores verdict class by bucket.

The v2 Verify stage is expected at `app.pipeline.verify`, matching
`design/v2-rearchitecture.md`. If that module is not present, live evals fail
with a wiring error instead of falling back to the retired v1 pipeline.

## Breaking Bucket

The `breaking` cases are placeholders on purpose. Before using this bucket as a
release gate, replace all `REFRESH REQUIRED` examples with claims from the last
7 days and set the expected verdicts from current evidence. For breaking-news
claims, `UNVERIFIABLE` can be acceptable when evidence is still thin; a
confident stale verdict should not be accepted.
