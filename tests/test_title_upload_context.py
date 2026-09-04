from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage

from deerflow.agents.middlewares.title_middleware import TitleMiddleware


def _state(user_content: str):
    return {
        "messages": [
            HumanMessage(content=user_content),
            AIMessage(content="I can inspect the report."),
        ]
    }


def test_title_prompt_ignores_legacy_uploaded_files_context() -> None:
    middleware = TitleMiddleware()
    state = _state(
        "<uploaded_files>\n- confidential-report.pdf\n</uploaded_files>\n\n"
        "Summarize the report"
    )

    prompt, user_message = middleware._build_title_prompt(state)

    assert user_message == "Summarize the report"
    assert "confidential-report.pdf" not in prompt


async def test_attachment_only_title_skips_title_model(monkeypatch) -> None:
    middleware = TitleMiddleware()
    state = _state("<uploaded_files>\n- report.pdf\n</uploaded_files>\n")
    monkeypatch.setattr(
        "deerflow.agents.middlewares.title_middleware.create_chat_model",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("attachment-only title must not call a model")
        ),
    )

    assert await middleware._agenerate_title_result(state) == {
        "title": "New Conversation"
    }
