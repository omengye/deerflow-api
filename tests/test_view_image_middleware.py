from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from deerflow.agents.middlewares.view_image_middleware import ViewImageMiddleware


def test_view_image_middleware_injects_image_with_analysis_instruction() -> None:
    middleware = ViewImageMiddleware()
    state = {
        "messages": [
            HumanMessage(content="Describe this image"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "view_image",
                        "args": {"image_path": "/mnt/user-data/uploads/test.png"},
                        "id": "call-1",
                        "type": "tool_call",
                    }
                ],
            ),
            ToolMessage(content="Successfully read image", tool_call_id="call-1"),
        ],
        "viewed_images": {
            "/mnt/user-data/uploads/test.png": {
                "base64": "aW1hZ2U=",
                "mime_type": "image/png",
            }
        },
    }

    update = middleware.before_model(state, None)  # type: ignore[arg-type]

    assert update is not None
    injected = update["messages"][0]
    assert isinstance(injected, HumanMessage)
    assert isinstance(injected.content, list)
    assert injected.content[0]["type"] == "text"
    assert "Use the attached image(s) to answer" in injected.content[0]["text"]
    assert "Do not merely confirm" in injected.content[0]["text"]
    assert injected.content[1]["type"] == "text"
    assert "/mnt/user-data/uploads/test.png" in injected.content[1]["text"]
    assert injected.content[2] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,aW1hZ2U="},
    }


def test_view_image_middleware_does_not_inject_duplicate_image_message() -> None:
    middleware = ViewImageMiddleware()
    image_message = HumanMessage(
        content=[
            {
                "type": "text",
                "text": "Image input for analysis. Use the attached image(s) to answer the user's most recent request.",
            }
        ]
    )
    state = {
        "messages": [
            HumanMessage(content="Describe this image"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "view_image",
                        "args": {"image_path": "/mnt/user-data/uploads/test.png"},
                        "id": "call-1",
                        "type": "tool_call",
                    }
                ],
            ),
            ToolMessage(content="Successfully read image", tool_call_id="call-1"),
            image_message,
        ],
        "viewed_images": {
            "/mnt/user-data/uploads/test.png": {
                "base64": "aW1hZ2U=",
                "mime_type": "image/png",
            }
        },
    }

    assert middleware.before_model(state, None) is None  # type: ignore[arg-type]
