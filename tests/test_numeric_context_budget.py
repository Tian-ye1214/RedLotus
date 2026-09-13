from pydantic_ai.messages import ModelRequest, UserPromptPart

from redlotus.ModelGateway.ModelChecker import estimate_context_tokens


def test_numeric_csv_is_not_estimated_as_english_prose():
    # Numeric-heavy real references exhausted the gateway's reserved input budget.
    # These tokenizers can split decimal digits independently.
    content = "0123456789,9876543210,1234567890\n" * 100
    request = ModelRequest(parts=[UserPromptPart(content)])
    assert estimate_context_tokens([request]) >= sum(char.isdigit() for char in content)


def test_ordinary_prose_keeps_its_existing_budget_scale():
    content = "Plain text, spacing, and punctuation.\n" * 100
    request = ModelRequest(parts=[UserPromptPart(content)])
    assert estimate_context_tokens([request]) < len(content) / 2
