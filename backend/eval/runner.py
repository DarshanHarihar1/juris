"""Golden-claim eval runner for the Juris v2 pipeline.

Usage:
    python -m eval.runner --dry-run
    python -m eval.runner --bucket slow_changing --limit 3
"""
from __future__ import annotations

import argparse
import asyncio
import inspect
import json
from collections import defaultdict
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

VERDICTS = {"TRUE", "FALSE", "MOSTLY_TRUE", "MISLEADING", "UNVERIFIABLE", "CONFLICTING"}
BUCKETS = ("static_true", "static_false_hoax", "slow_changing", "breaking")
DEFAULT_DATASET = Path(__file__).with_name("golden_claims.json")

LivePath = Callable[[str], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class EvalCase:
    id: str
    bucket: str
    claim: str
    expected_verdict: str
    acceptable_verdicts: tuple[str, ...] = field(default_factory=tuple)
    refresh_required: bool = False
    note: str = ""

    @property
    def acceptable(self) -> set[str]:
        return set(self.acceptable_verdicts or (self.expected_verdict,))


@dataclass(frozen=True)
class EvalResult:
    case: EvalCase
    verdict: str | None
    normalized_claim: str = ""
    error: str | None = None
    synthesized: Any = None

    @property
    def exact(self) -> bool:
        return self.verdict == self.case.expected_verdict

    @property
    def acceptable(self) -> bool:
        return self.verdict in self.case.acceptable


@dataclass(frozen=True)
class BucketScore:
    total: int
    exact_correct: int
    acceptable_correct: int

    @property
    def exact_rate(self) -> float:
        return self.exact_correct / self.total if self.total else 0.0

    @property
    def acceptable_rate(self) -> float:
        return self.acceptable_correct / self.total if self.total else 0.0


@dataclass(frozen=True)
class EvalSummary(BucketScore):
    buckets: dict[str, BucketScore]


def load_cases(path: str | Path = DEFAULT_DATASET) -> list[EvalCase]:
    raw = json.loads(Path(path).read_text())
    cases = [_case_from_json(item) for item in raw["cases"]]
    _validate_cases(cases)
    return cases


def _case_from_json(item: dict[str, Any]) -> EvalCase:
    expected = item["expected_verdict"]
    acceptable = tuple(item.get("acceptable_verdicts") or (expected,))
    return EvalCase(
        id=item["id"],
        bucket=item["bucket"],
        claim=item["claim"],
        expected_verdict=expected,
        acceptable_verdicts=acceptable,
        refresh_required=bool(item.get("refresh_required", False)),
        note=item.get("note", ""),
    )


def _validate_cases(cases: list[EvalCase]) -> None:
    seen = set()
    counts = defaultdict(int)
    for case in cases:
        if case.id in seen:
            raise ValueError(f"duplicate case id: {case.id}")
        seen.add(case.id)
        if case.bucket not in BUCKETS:
            raise ValueError(f"{case.id}: unknown bucket {case.bucket!r}")
        if case.expected_verdict not in VERDICTS:
            raise ValueError(f"{case.id}: unknown expected verdict {case.expected_verdict!r}")
        unknown_acceptable = set(case.acceptable_verdicts) - VERDICTS
        if unknown_acceptable:
            raise ValueError(f"{case.id}: unknown acceptable verdicts {sorted(unknown_acceptable)}")
        counts[case.bucket] += 1
    missing = [bucket for bucket in BUCKETS if counts[bucket] == 0]
    if missing:
        raise ValueError(f"missing buckets: {', '.join(missing)}")


def filter_cases(
    cases: Iterable[EvalCase],
    *,
    bucket: str | None = None,
    include_refresh_required: bool = True,
    limit: int | None = None,
) -> list[EvalCase]:
    selected = [
        case for case in cases
        if (bucket is None or case.bucket == bucket)
        and (include_refresh_required or not case.refresh_required)
    ]
    return selected[:limit] if limit else selected


async def run_case(case: EvalCase, live_path: LivePath = None) -> EvalResult:
    live_path = live_path or run_live_v2_path
    try:
        payload = await live_path(case.claim)
        verdict = _extract_verdict(payload)
        return EvalResult(
            case=case,
            verdict=verdict,
            normalized_claim=str(payload.get("normalized_claim", "")),
            synthesized=payload.get("synthesized"),
        )
    except Exception as exc:
        return EvalResult(case=case, verdict=None, error=f"{type(exc).__name__}: {exc}")


async def run_live_v2_path(text: str) -> dict[str, Any]:
    """Run normalize -> verify -> synthesize in-process, using the v2 backend modules.

    The v2 plan names `app.pipeline.verify` as the new Verify stage. This adapter is
    intentionally thin: when that module is unavailable, the eval fails fast with a
    wiring message instead of silently falling back to the retired v1 path.
    """
    from app.pipeline import s1_normalize

    norm = await s1_normalize.normalize(text)
    if not norm.sub_claims:
        return {"verdict": "UNVERIFIABLE", "normalized_claim": "", "synthesized": None}

    claim_text = norm.sub_claims[0]
    verify_module = _import_v2_verify_module()
    verify_fn = _find_callable(verify_module, ("verify", "verify_claim", "run", "verify_with_evidence"))
    verdict_payload = await _call_with_supported_kwargs(
        verify_fn,
        claim=claim_text,
        normalized_claim=claim_text,
        claim_text=claim_text,
        text_norm=claim_text,
        claim_native=claim_text,
        text_norm_native=claim_text,
        lang=norm.language,
        detected_lang=norm.language,
        job_id="eval",
        claim_id="eval",
    )
    verdict_dict = _as_dict(verdict_payload)
    # SubClaimVerdict uses lowercase; normalize for eval scoring.
    if isinstance(verdict_dict.get("verdict"), str):
        verdict_dict["verdict"] = verdict_dict["verdict"].upper()
    verdict_dict.setdefault("normalized_claim", claim_text)
    verdict_dict["synthesized"] = await _try_synthesize(norm.language, claim_text, verdict_dict, text)
    return verdict_dict


def _import_v2_verify_module() -> Any:
    try:
        return import_module("app.pipeline.verify")
    except ModuleNotFoundError as exc:
        if exc.name == "app.pipeline.verify":
            raise RuntimeError(
                "v2 Verify module not found. Expected app.pipeline.verify per "
                "design/v2-rearchitecture.md migration step 1."
            ) from exc
        raise


def _find_callable(module: Any, names: tuple[str, ...]) -> Callable[..., Any]:
    for name in names:
        fn = getattr(module, name, None)
        if callable(fn):
            return fn
    raise RuntimeError(f"{module.__name__} has none of: {', '.join(names)}")


async def _call_with_supported_kwargs(fn: Callable[..., Any], **available: Any) -> Any:
    sig = inspect.signature(fn)
    kwargs = {}
    positional = []
    accepts_kwargs = any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values())

    for name, param in sig.parameters.items():
        if param.kind == param.VAR_POSITIONAL:
            continue
        if name in available and param.kind == param.POSITIONAL_ONLY:
            positional.append(available[name])
        elif name in available:
            kwargs[name] = available[name]
        elif param.default is inspect.Parameter.empty and param.kind in (param.POSITIONAL_ONLY, param.POSITIONAL_OR_KEYWORD):
            if name in ("text", "claim_en", "message"):
                positional.append(available["claim_text"])
            else:
                raise RuntimeError(f"cannot call {fn.__name__}: unsupported required parameter {name!r}")

    if accepts_kwargs:
        kwargs.update(available)

    result = fn(*positional, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


async def _try_synthesize(lang: str, claim: Any, verdict: dict[str, Any], original: str) -> Any:
    claim_text = claim if isinstance(claim, str) else getattr(claim, "text_norm", str(claim))
    claim_native = claim_text if isinstance(claim, str) else getattr(claim, "text_norm_native", claim_text)
    for module_name in ("app.pipeline.synthesize_v2", "app.pipeline.v2_synthesize", "app.pipeline.synthesize"):
        try:
            module = import_module(module_name)
        except ModuleNotFoundError:
            continue
        for fn_name in ("synthesize_eval", "synthesize_claim", "synthesize_text", "synthesize"):
            fn = getattr(module, fn_name, None)
            if callable(fn) and _can_call_without_db(fn):
                return await _call_with_supported_kwargs(
                    fn,
                    claim_en=claim_text,
                    claim_native=claim_native,
                    lang=lang,
                    verdict=verdict.get("verdict"),
                    confidence=verdict.get("confidence", 0),
                    path=verdict.get("path", "verify"),
                    evidence=verdict.get("evidence", []),
                    original=original,
                )
    return None


def _can_call_without_db(fn: Callable[..., Any]) -> bool:
    sig = inspect.signature(fn)
    required = {
        name for name, param in sig.parameters.items()
        if param.default is inspect.Parameter.empty
        and param.kind in (param.POSITIONAL_ONLY, param.POSITIONAL_OR_KEYWORD, param.KEYWORD_ONLY)
    }
    return not ({"con", "connection", "db"} & required)


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    raise RuntimeError(f"cannot read verdict payload from {type(value).__name__}")


def _extract_verdict(payload: dict[str, Any]) -> str | None:
    verdict = payload.get("verdict")
    return verdict if verdict in VERDICTS else None


def score_results(results: Iterable[EvalResult]) -> EvalSummary:
    rows = list(results)
    by_bucket: dict[str, list[EvalResult]] = defaultdict(list)
    for result in rows:
        by_bucket[result.case.bucket].append(result)
    bucket_scores = {bucket: _score(group) for bucket, group in sorted(by_bucket.items())}
    total_score = _score(rows)
    return EvalSummary(
        total=total_score.total,
        exact_correct=total_score.exact_correct,
        acceptable_correct=total_score.acceptable_correct,
        buckets=bucket_scores,
    )


def _score(results: list[EvalResult]) -> BucketScore:
    return BucketScore(
        total=len(results),
        exact_correct=sum(result.exact for result in results),
        acceptable_correct=sum(result.acceptable for result in results),
    )


async def run_eval(cases: list[EvalCase]) -> list[EvalResult]:
    results = []
    for case in cases:
        result = await run_case(case)
        results.append(result)
        print(_format_result(result), flush=True)
    return results


def _format_result(result: EvalResult) -> str:
    if result.error:
        return f"{result.case.id} [{result.case.bucket}] ERROR expected={result.case.expected_verdict} {result.error}"
    marker = "OK" if result.exact else ("ACCEPT" if result.acceptable else "FAIL")
    return (
        f"{result.case.id} [{result.case.bucket}] {marker} "
        f"expected={result.case.expected_verdict} got={result.verdict}"
    )


def print_summary(summary: EvalSummary) -> None:
    print("\nSummary")
    print(f"overall exact: {summary.exact_correct}/{summary.total} ({summary.exact_rate:.0%})")
    print(f"overall acceptable: {summary.acceptable_correct}/{summary.total} ({summary.acceptable_rate:.0%})")
    for bucket in BUCKETS:
        score = summary.buckets.get(bucket, BucketScore(0, 0, 0))
        print(
            f"{bucket}: exact {score.exact_correct}/{score.total} ({score.exact_rate:.0%}), "
            f"acceptable {score.acceptable_correct}/{score.total} ({score.acceptable_rate:.0%})"
        )


def print_dataset_summary(cases: list[EvalCase]) -> None:
    print(f"Loaded {len(cases)} eval cases.")
    for bucket in BUCKETS:
        bucket_cases = [case for case in cases if case.bucket == bucket]
        refresh_count = sum(case.refresh_required for case in bucket_cases)
        suffix = f", {refresh_count} refresh-required" if refresh_count else ""
        print(f"{bucket}: {len(bucket_cases)} cases{suffix}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Juris v2 golden-claim verdict-class evals.")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET), help="Path to golden_claims.json")
    parser.add_argument("--bucket", choices=BUCKETS, help="Run one bucket only")
    parser.add_argument("--limit", type=int, help="Run at most N selected cases")
    parser.add_argument(
        "--skip-refresh-required",
        action="store_true",
        help="Skip breaking placeholders that must be refreshed before live gating",
    )
    parser.add_argument("--dry-run", action="store_true", help="Load and summarize the dataset without live model calls")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cases = filter_cases(
        load_cases(args.dataset),
        bucket=args.bucket,
        include_refresh_required=not args.skip_refresh_required,
        limit=args.limit,
    )
    if args.dry_run:
        print_dataset_summary(cases)
        refresh_count = sum(case.refresh_required for case in cases)
        if refresh_count:
            print(f"\nNote: {refresh_count} breaking cases are refresh-required placeholders.")
        return 0

    results = asyncio.run(run_eval(cases))
    print_summary(score_results(results))
    return 0 if all(result.acceptable for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
