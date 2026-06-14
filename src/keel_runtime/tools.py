"""Tool definition, schema generation, and execution primitives."""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from types import NoneType, UnionType
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

ToolHandler = Callable[..., Awaitable[Any] | Any]


class _ToolTimedOut(Exception):
    pass


@dataclass(slots=True)
class ToolError:
    code: str
    message: str
    retryable: bool = False
    safe_to_retry: bool = False

    def __post_init__(self) -> None:
        if not self.code.strip():
            raise ValueError("ToolError.code cannot be empty")
        if not self.message.strip():
            raise ValueError("ToolError.message cannot be empty")

    def __str__(self) -> str:
        return self.message

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "safe_to_retry": self.safe_to_retry,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolError:
        return cls(
            code=str(data["code"]),
            message=str(data["message"]),
            retryable=bool(data.get("retryable", False)),
            safe_to_retry=bool(data.get("safe_to_retry", False)),
        )

    @classmethod
    def unknown_tool(cls, name: str) -> ToolError:
        return cls(
            code="unknown_tool",
            message=f"Unknown tool: {name}",
            retryable=False,
            safe_to_retry=False,
        )

    @classmethod
    def validation(cls, message: str) -> ToolError:
        return cls(
            code="validation_error",
            message=message,
            retryable=False,
            safe_to_retry=False,
        )

    @classmethod
    def execution(cls, message: str) -> ToolError:
        return cls(
            code="execution_error",
            message=message,
            retryable=False,
            safe_to_retry=False,
        )

    @classmethod
    def timeout(cls, seconds: float, *, safe_to_retry: bool) -> ToolError:
        return cls(
            code="timeout",
            message=f"Tool timed out after {seconds:g} seconds",
            retryable=True,
            safe_to_retry=safe_to_retry,
        )


@dataclass(slots=True)
class ToolCall:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    call_id: str | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("ToolCall.name cannot be empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "arguments": dict(self.arguments),
            "call_id": self.call_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolCall:
        return cls(
            name=str(data["name"]),
            arguments=dict(data.get("arguments") or {}),
            call_id=str(data["call_id"]) if data.get("call_id") is not None else None,
        )


@dataclass(slots=True)
class ToolResult:
    name: str
    ok: bool
    output: Any = None
    error: str | None = None
    call_id: str | None = None
    error_details: ToolError | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "output": self.output,
            "error": (
                self.error_details.to_dict()
                if self.error_details is not None
                else self.error
            ),
            "call_id": self.call_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolResult:
        error_data = data.get("error")
        error_details = (
            ToolError.from_dict(error_data) if isinstance(error_data, dict) else None
        )
        return cls(
            name=str(data["name"]),
            ok=bool(data["ok"]),
            output=data.get("output"),
            error=error_details.message if error_details is not None else error_data,
            call_id=str(data["call_id"]) if data.get("call_id") is not None else None,
            error_details=error_details,
        )

    @classmethod
    def success(cls, name: str, output: Any, *, call_id: str | None = None) -> ToolResult:
        return cls(name=name, ok=True, output=output, call_id=call_id)

    @classmethod
    def failure(
        cls,
        name: str,
        error: str | ToolError,
        *,
        call_id: str | None = None,
    ) -> ToolResult:
        if isinstance(error, ToolError):
            return cls(
                name=name,
                ok=False,
                error=error.message,
                call_id=call_id,
                error_details=error,
            )
        return cls(name=name, ok=False, error=error, call_id=call_id)


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    timeout_seconds: float | None = None
    side_effect: bool = False
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("ToolSpec.name cannot be empty")
        if not callable(self.handler):
            raise TypeError("ToolSpec.handler must be callable")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("ToolSpec.timeout_seconds must be positive")
        if self.idempotency_key is not None and not self.idempotency_key.strip():
            raise ValueError("ToolSpec.idempotency_key cannot be empty")

    @classmethod
    def from_callable(
        cls,
        handler: ToolHandler,
        *,
        name: str | None = None,
        description: str | None = None,
        timeout_seconds: float | None = None,
        side_effect: bool = False,
        idempotency_key: str | None = None,
    ) -> ToolSpec:
        tool_name = name or handler.__name__
        tool_description = description if description is not None else inspect.getdoc(handler) or ""
        return cls(
            name=tool_name,
            description=tool_description,
            parameters=_schema_from_signature(handler),
            handler=handler,
            timeout_seconds=timeout_seconds,
            side_effect=side_effect,
            idempotency_key=idempotency_key,
        )

    def to_dict(self) -> dict[str, Any]:
        data = {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }
        if self.timeout_seconds is not None:
            data["timeout_seconds"] = self.timeout_seconds
        if self.side_effect:
            data["side_effect"] = self.side_effect
        if self.idempotency_key is not None:
            data["idempotency_key"] = self.idempotency_key
        return data

    async def execute(
        self,
        arguments: dict[str, Any] | None = None,
        *,
        persisted: bool = False,
    ) -> ToolResult:
        if persisted and self.side_effect and self.idempotency_key is None:
            return ToolResult.failure(
                self.name,
                ToolError.validation(
                    "Side-effect tool requires idempotency_key for persisted execution"
                ),
            )
        arguments = dict(arguments or {})
        try:
            bound = _bind_and_validate(self.handler, arguments)
            result = self.handler(*bound.args, **bound.kwargs)
            if inspect.isawaitable(result):
                if self.timeout_seconds is not None:
                    try:
                        result = await _await_with_timeout(result, self.timeout_seconds)
                    except _ToolTimedOut:
                        return self._timeout_result()
                    return ToolResult.success(self.name, result)
                result = await result
            return ToolResult.success(self.name, result)
        except TypeError as exc:
            return ToolResult.failure(self.name, ToolError.validation(str(exc)))
        except Exception as exc:
            return ToolResult.failure(self.name, ToolError.execution(str(exc)))

    def _timeout_result(self) -> ToolResult:
        return ToolResult.failure(
            self.name,
            ToolError.timeout(
                self.timeout_seconds or 0,
                safe_to_retry=not self.side_effect or self.idempotency_key is not None,
            ),
        )


async def _await_with_timeout(awaitable: Awaitable[Any], timeout_seconds: float) -> Any:
    task = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=timeout_seconds)
    except TimeoutError as exc:
        if task.done():
            return await task
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise _ToolTimedOut from exc


class ToolRegistry:
    def __init__(self, tools: Iterable[ToolSpec | ToolHandler] | None = None) -> None:
        self._tools: dict[str, ToolSpec] = {}
        for item in tools or []:
            self.register(item)

    def register(self, item: ToolSpec | ToolHandler) -> ToolSpec:
        spec = ensure_tool_spec(item)
        self._tools[spec.name] = spec
        return spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def list(self) -> list[ToolSpec]:
        return [self._tools[name] for name in sorted(self._tools)]

    def to_list(self) -> list[dict[str, Any]]:
        return [spec.to_dict() for spec in self.list()]

    async def execute(
        self,
        call: ToolCall | dict[str, Any] | str,
        arguments: dict[str, Any] | None = None,
        *,
        persisted: bool = False,
    ) -> ToolResult:
        tool_call = self._normalize_call(call, arguments)
        spec = self.get(tool_call.name)
        if spec is None:
            return ToolResult.failure(
                tool_call.name,
                f"Unknown tool: {tool_call.name}",
                call_id=tool_call.call_id,
            )
        result = await spec.execute(tool_call.arguments, persisted=persisted)
        result.call_id = tool_call.call_id
        return result

    @staticmethod
    def _normalize_call(
        call: ToolCall | dict[str, Any] | str,
        arguments: dict[str, Any] | None,
    ) -> ToolCall:
        if isinstance(call, ToolCall):
            return call
        if isinstance(call, dict):
            return ToolCall.from_dict(call)
        return ToolCall(name=call, arguments=dict(arguments or {}))


def tool(
    name: str | None = None,
    description: str | None = None,
    timeout_seconds: float | None = None,
    side_effect: bool = False,
    idempotency_key: str | None = None,
) -> Callable[[ToolHandler], ToolHandler]:
    def decorator(handler: ToolHandler) -> ToolHandler:
        spec = ToolSpec.from_callable(
            handler,
            name=name,
            description=description,
            timeout_seconds=timeout_seconds,
            side_effect=side_effect,
            idempotency_key=idempotency_key,
        )
        handler.__keel_tool_spec__ = spec  # type: ignore[attr-defined]
        return handler

    return decorator


def ensure_tool_spec(item: ToolSpec | ToolHandler) -> ToolSpec:
    if isinstance(item, ToolSpec):
        return item
    spec = getattr(item, "__keel_tool_spec__", None)
    if isinstance(spec, ToolSpec):
        return spec
    return ToolSpec.from_callable(item)


def _schema_from_signature(handler: ToolHandler) -> dict[str, Any]:
    signature = inspect.signature(handler)
    type_hints = _resolved_type_hints(handler)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, parameter in signature.parameters.items():
        if parameter.kind in {
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        }:
            raise ValueError("Tool functions cannot use *args or **kwargs")
        schema = _annotation_to_schema(type_hints.get(name, parameter.annotation))
        if parameter.default is not inspect.Parameter.empty:
            schema["default"] = _json_safe_default(parameter.default)
        else:
            required.append(name)
        properties[name] = schema
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _bind_and_validate(
    handler: ToolHandler,
    arguments: dict[str, Any],
) -> inspect.BoundArguments:
    signature = inspect.signature(handler)
    type_hints = _resolved_type_hints(handler)
    bound = signature.bind(**arguments)
    bound.apply_defaults()
    for name, value in bound.arguments.items():
        annotation = type_hints.get(name, signature.parameters[name].annotation)
        if not _matches_annotation(value, annotation):
            expected = _annotation_name(annotation)
            actual = type(value).__name__
            raise TypeError(f"Argument {name!r} expected {expected}, got {actual}")
    return bound


def _resolved_type_hints(handler: ToolHandler) -> dict[str, Any]:
    try:
        return get_type_hints(handler)
    except (AttributeError, NameError, TypeError):
        return {}


def _annotation_to_schema(annotation: Any) -> dict[str, Any]:
    if annotation is inspect.Parameter.empty or annotation is Any:
        return {}
    origin = get_origin(annotation)
    args = get_args(annotation)

    if origin in {Union, UnionType}:
        non_none = [arg for arg in args if arg is not NoneType]
        if len(non_none) == 1 and len(non_none) != len(args):
            schema = _annotation_to_schema(non_none[0])
            schema["nullable"] = True
            return schema
        return {"anyOf": [_annotation_to_schema(arg) for arg in args]}
    if origin is Literal:
        return {"enum": list(args)}
    if origin in {list, tuple, set}:
        item_type = args[0] if args else Any
        return {"type": "array", "items": _annotation_to_schema(item_type)}
    if origin is dict:
        return {"type": "object"}

    mapping = {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
        list: "array",
        tuple: "array",
        set: "array",
        dict: "object",
    }
    schema_type = mapping.get(annotation)
    if schema_type is not None:
        return {"type": schema_type}
    return {}


def _matches_annotation(value: Any, annotation: Any) -> bool:
    if annotation is inspect.Parameter.empty or annotation is Any:
        return True
    origin = get_origin(annotation)
    args = get_args(annotation)

    if origin in {Union, UnionType}:
        return any(_matches_annotation(value, arg) for arg in args)
    if origin is Literal:
        return value in args
    if origin in {list, tuple, set}:
        return isinstance(value, origin)
    if origin is dict:
        return isinstance(value, dict)
    if annotation is NoneType:
        return value is None
    if annotation is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if annotation is float:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if annotation is bool:
        return isinstance(value, bool)
    if annotation in {str, list, tuple, set, dict}:
        return isinstance(value, annotation)
    return True


def _annotation_name(annotation: Any) -> str:
    if annotation is inspect.Parameter.empty:
        return "Any"
    return getattr(annotation, "__name__", str(annotation))


def _json_safe_default(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        return repr(value)
