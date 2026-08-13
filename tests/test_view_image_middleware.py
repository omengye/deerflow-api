import base64
from pathlib import Path
from types import SimpleNamespace

from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from deerflow.agents.middlewares.view_image_middleware import ViewImageMiddleware
from deerflow.tools.builtins.view_image_tool import _sanitize_image_error, view_image_tool


IMAGE_PATH = "/mnt/user-data/uploads/test.png"


def _state(tmp_path: Path) -> dict:
    image_bytes = b"\x89PNG\r\n\x1a\ntransient-image"
    tmp_path.joinpath("test.png").write_bytes(image_bytes)
    return {
        "messages": [
            HumanMessage(content="Describe this image"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "view_image",
                        "args": {"image_path": IMAGE_PATH},
                        "id": "call-1",
                        "type": "tool_call",
                    }
                ],
            ),
            ToolMessage(content="Successfully read image", tool_call_id="call-1"),
        ],
        "viewed_images": {IMAGE_PATH: {"mime_type": "image/png"}},
        "thread_data": {"uploads_path": str(tmp_path)},
    }


def _request(state: dict) -> ModelRequest:
    return ModelRequest(model=None, messages=state["messages"], state=state, runtime=None)


def test_view_image_middleware_injects_image_only_for_model_call(tmp_path: Path) -> None:
    middleware = ViewImageMiddleware()
    state = _state(tmp_path)
    persisted_messages = state["messages"]
    seen_request: ModelRequest | None = None

    def handler(request: ModelRequest) -> AIMessage:
        nonlocal seen_request
        seen_request = request
        return AIMessage(content="analysis")

    result = middleware.wrap_model_call(_request(state), handler)

    assert isinstance(result, AIMessage)
    assert seen_request is not None
    assert len(seen_request.messages) == len(persisted_messages) + 1
    injected = seen_request.messages[-1]
    assert isinstance(injected, HumanMessage)
    assert isinstance(injected.content, list)
    assert injected.content[0]["type"] == "text"
    assert "Use the attached image(s) to answer" in injected.content[0]["text"]
    assert "Do not merely confirm" in injected.content[0]["text"]
    assert IMAGE_PATH in injected.content[1]["text"]
    assert injected.content[2] == {
        "type": "image_url",
        "image_url": {
            "url": f"data:image/png;base64,{base64.b64encode(tmp_path.joinpath('test.png').read_bytes()).decode('ascii')}"
        },
    }
    assert injected.additional_kwargs.get("_view_image_injection") is True

    # The request override must not mutate data that will be checkpointed.
    assert state["messages"] is persisted_messages
    assert len(persisted_messages) == 3
    assert "base64" not in state["viewed_images"][IMAGE_PATH]


def test_view_image_middleware_clears_lightweight_state_after_model(tmp_path: Path) -> None:
    middleware = ViewImageMiddleware()
    state = _state(tmp_path)

    assert middleware.after_model(state, None) == {"viewed_images": {}}  # type: ignore[arg-type]


def test_view_image_middleware_does_not_inject_duplicate_legacy_message(tmp_path: Path) -> None:
    middleware = ViewImageMiddleware()
    state = _state(tmp_path)
    state["messages"].append(
        HumanMessage(
            content=[
                {
                    "type": "text",
                    "text": "Image input for analysis. Use the attached image(s) to answer the user's most recent request.",
                }
            ],
            additional_kwargs={"_view_image_injection": True},
        )
    )
    seen_messages: list = []

    def handler(request: ModelRequest) -> AIMessage:
        seen_messages.extend(request.messages)
        return AIMessage(content="ok")

    middleware.wrap_model_call(_request(state), handler)

    assert seen_messages == state["messages"]


def test_view_image_tool_persists_metadata_without_base64(tmp_path: Path) -> None:
    tmp_path.joinpath("test.png").write_bytes(b"\x89PNG\r\n\x1a\nlightweight-state")
    runtime = SimpleNamespace(state={"thread_data": {"uploads_path": str(tmp_path)}})

    command = view_image_tool.func(
        runtime=runtime,
        image_path=IMAGE_PATH,
        tool_call_id="call-1",
    )

    assert command.update["viewed_images"] == {
        IMAGE_PATH: {"mime_type": "image/png"},
    }


def test_view_image_tool_accepts_gif87a_and_gif89a(tmp_path: Path) -> None:
    for index, header in enumerate((b"GIF87a", b"GIF89a"), start=1):
        filename = f"animation-{index}.gif"
        virtual_path = f"/mnt/user-data/uploads/{filename}"
        tmp_path.joinpath(filename).write_bytes(header + b"minimal-gif-payload")
        runtime = SimpleNamespace(state={"thread_data": {"uploads_path": str(tmp_path)}})

        command = view_image_tool.func(
            runtime=runtime,
            image_path=virtual_path,
            tool_call_id=f"call-gif-{index}",
        )

        assert command.update["viewed_images"] == {virtual_path: {"mime_type": "image/gif"}}


def test_view_image_error_masks_runtime_scoped_skill_projection(tmp_path: Path) -> None:
    projection = tmp_path / "projection"
    projection.mkdir()
    runtime = SimpleNamespace(state={"sandbox": {"skills_path": str(projection)}})

    sanitized = _sanitize_image_error(
        OSError(f"cannot read {projection / 'public' / 'example' / 'image.png'}"),
        {},
        runtime,
    )

    assert str(projection) not in sanitized
    assert "/mnt/skills/public/example/image.png" in sanitized
