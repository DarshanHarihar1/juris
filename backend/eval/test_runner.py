import asyncio


def test_bundled_golden_claims_cover_required_buckets():
    from eval.runner import load_cases

    cases = load_cases()
    buckets = {case.bucket for case in cases}

    assert len(cases) == 40
    assert buckets == {"static_true", "static_false_hoax", "slow_changing", "breaking"}
    assert all(sum(1 for case in cases if case.bucket == bucket) == 10 for bucket in buckets)


def test_score_cases_reports_exact_and_acceptable_by_bucket():
    from eval.runner import EvalCase, EvalResult, score_results

    cases = [
        EvalCase("s1", "static_true", "A true claim", "TRUE"),
        EvalCase("b1", "breaking", "A fresh claim", "FALSE", acceptable_verdicts=("FALSE", "UNVERIFIABLE")),
    ]
    results = [
        EvalResult(cases[0], verdict="TRUE", normalized_claim="A true claim"),
        EvalResult(cases[1], verdict="UNVERIFIABLE", normalized_claim="A fresh claim"),
    ]

    summary = score_results(results)

    assert summary.total == 2
    assert summary.exact_correct == 1
    assert summary.acceptable_correct == 2
    assert summary.buckets["breaking"].exact_correct == 0
    assert summary.buckets["breaking"].acceptable_correct == 1


def test_live_runner_uses_injected_v2_path_without_http():
    from eval.runner import EvalCase, run_case

    async def fake_path(text):
        assert text == "The Eiffel Tower is in Paris."
        return {
            "verdict": "TRUE",
            "normalized_claim": "The Eiffel Tower is in Paris.",
            "synthesized": {"one_liner_native": "True."},
        }

    result = asyncio.run(run_case(EvalCase("s1", "static_true", "The Eiffel Tower is in Paris.", "TRUE"), fake_path))

    assert result.verdict == "TRUE"
    assert result.normalized_claim == "The Eiffel Tower is in Paris."
    assert result.error is None
