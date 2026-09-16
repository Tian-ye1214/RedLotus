from pydantic_ai.usage import RequestUsage

from redlotus.core.history import BillableTokens
from redlotus.core.history import UsageTotals


def test_cache_totals_use_provider_counts_and_keep_unknown_input_separate():
    totals = UsageTotals()
    for usage in (
        RequestUsage(
            input_tokens=100,
            output_tokens=10,
            details={"prompt_cache_hit_tokens": 90, "prompt_cache_miss_tokens": 10},
        ),
        RequestUsage(input_tokens=50, output_tokens=5, cache_read_tokens=40),
        RequestUsage(input_tokens=20, output_tokens=2),
    ):
        totals.add_usage(usage, BillableTokens(usage.input_tokens, usage.output_tokens))
    assert totals.cache_hit_tokens == 130
    assert totals.cache_miss_tokens == 20
    assert (
        totals.input_tokens - totals.cache_hit_tokens - totals.cache_miss_tokens == 20
    )
    assert totals.input_tokens == 170


def test_known_zero_cache_hits_are_not_treated_as_missing_usage():
    totals = UsageTotals()
    usage = RequestUsage(
        input_tokens=100,
        details={"prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 100},
    )
    totals.add_usage(usage, BillableTokens(100, 0))
    assert totals.cache_hit_tokens == 0 and totals.cache_miss_tokens == 100


def test_sdk_extracts_usage_when_response_contains_reasoning_tokens():
    from pydantic_ai.usage import RequestUsage

    usage = RequestUsage.extract(
        {
            "model": "deepseek-flash",
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "prompt_tokens_details": {"cached_tokens": 80},
                "completion_tokens_details": {"reasoning_tokens": 12},
            },
        },
        provider="openai",
        provider_url="https://api.deepseek.com/v1/",
        provider_fallback="openai",
        api_flavor="chat",
    )
    assert (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens) == (
        100,
        20,
        80,
    )
