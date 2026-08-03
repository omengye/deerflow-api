from deerflow.agents.middlewares.clarification_middleware import ClarificationMiddleware


def test_clarification_flattens_cleans_and_deduplicates_dict_options() -> None:
    middleware = ClarificationMiddleware()
    options = {
        "item": {
            "item": "Move the section earlier</item>",
            "$text": ["Merge shared patterns</item>", "Move the section earlier"],
        },
        "$text": 2,
        "ignored": None,
    }

    assert middleware._normalize_options(options) == [
        "Move the section earlier",
        "Merge shared patterns",
        "2",
    ]


def test_clarification_formats_json_encoded_dict_options() -> None:
    middleware = ClarificationMiddleware()
    message = middleware._format_clarification_message(
        {
            "question": "Choose",
            "clarification_type": "approach_choice",
            "options": '{"item": ["A</item>", "B", "A"]}',
        }
    )

    assert "1. A" in message
    assert "2. B" in message
    assert "3." not in message
