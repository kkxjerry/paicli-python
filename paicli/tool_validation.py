"""Dependency-free runtime validation for the JSON Schema subset used by tools.

Schemas are still sent to the model, but they are no longer merely prompt
hints.  The gateway validates the actual arguments immediately before policy
checks and execution.  Unknown keywords are intentionally ignored so external
MCP schemas remain forwards-compatible; the commonly used structural and
constraint keywords below are enforced.
"""

from __future__ import annotations

import math
import re
from typing import Any


class SchemaValidationError(ValueError):
    """One deterministic, user-readable argument validation failure."""


def validate_json_schema(value: Any, schema: dict[str, Any], path: str = "$") -> None:
    """Validate ``value`` against the supported JSON Schema subset.

    Supported keywords: ``type``, ``enum``, ``const``, ``required``,
    ``properties``, ``additionalProperties``, ``items``, ``minItems``,
    ``maxItems``, ``uniqueItems``, ``minLength``, ``maxLength``, ``pattern``,
    ``minimum``, ``maximum``, ``exclusiveMinimum``, ``exclusiveMaximum``,
    ``allOf``, ``anyOf``, and ``oneOf``.
    """

    if not isinstance(schema, dict):
        raise SchemaValidationError(f"{path}: schema must be an object")

    _validate_combinators(value, schema, path)

    expected_type = schema.get("type")
    if expected_type is not None:
        expected = (
            tuple(expected_type)
            if isinstance(expected_type, list)
            else (expected_type,)
        )
        if not all(isinstance(item, str) for item in expected):
            raise SchemaValidationError(f"{path}: schema type must be a string or list")
        if not any(_matches_type(value, item) for item in expected):
            label = " | ".join(expected)
            raise SchemaValidationError(
                f"{path}: expected {label}, got {_json_type(value)}"
            )

    if "const" in schema and value != schema["const"]:
        raise SchemaValidationError(f"{path}: value must equal {schema['const']!r}")

    if "enum" in schema:
        choices = schema["enum"]
        if not isinstance(choices, list):
            raise SchemaValidationError(f"{path}: schema enum must be an array")
        if value not in choices:
            custom = schema.get("errorMessage")
            prefix = f"{custom}; " if isinstance(custom, str) and custom else ""
            raise SchemaValidationError(
                f"{path}: {prefix}value {value!r} is not one of {choices!r}"
            )

    if isinstance(value, dict):
        _validate_object(value, schema, path)
    elif isinstance(value, list):
        _validate_array(value, schema, path)
    elif isinstance(value, str):
        _validate_string(value, schema, path)
    elif _is_number(value):
        _validate_number(value, schema, path)


def _validate_combinators(value: Any, schema: dict[str, Any], path: str) -> None:
    all_of = schema.get("allOf")
    if all_of is not None:
        if not isinstance(all_of, list):
            raise SchemaValidationError(f"{path}: schema allOf must be an array")
        for child in all_of:
            validate_json_schema(value, child, path)

    any_of = schema.get("anyOf")
    if any_of is not None:
        if not isinstance(any_of, list) or not any_of:
            raise SchemaValidationError(f"{path}: schema anyOf must be a non-empty array")
        if not _matching_schema_count(value, any_of, path):
            raise SchemaValidationError(f"{path}: value does not match any allowed schema")

    one_of = schema.get("oneOf")
    if one_of is not None:
        if not isinstance(one_of, list) or not one_of:
            raise SchemaValidationError(f"{path}: schema oneOf must be a non-empty array")
        matched = _matching_schema_count(value, one_of, path)
        if matched != 1:
            raise SchemaValidationError(
                f"{path}: value must match exactly one schema, matched {matched}"
            )


def _matching_schema_count(value: Any, schemas: list[Any], path: str) -> int:
    matched = 0
    for child in schemas:
        try:
            validate_json_schema(value, child, path)
        except SchemaValidationError:
            continue
        matched += 1
    return matched


def _validate_object(value: dict[str, Any], schema: dict[str, Any], path: str) -> None:
    required = schema.get("required", [])
    if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
        raise SchemaValidationError(f"{path}: schema required must be a string array")
    missing = [name for name in required if name not in value]
    if missing:
        raise SchemaValidationError(
            f"{path}: missing required properties: {', '.join(sorted(missing))}"
        )

    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        raise SchemaValidationError(f"{path}: schema properties must be an object")

    additional = schema.get("additionalProperties", True)
    for name, item in value.items():
        child_path = _property_path(path, str(name))
        if name in properties:
            validate_json_schema(item, properties[name], child_path)
            continue
        if additional is False:
            raise SchemaValidationError(f"{child_path}: additional property is not allowed")
        if isinstance(additional, dict):
            validate_json_schema(item, additional, child_path)

    _validate_size(value, schema, path, "minProperties", "maxProperties", "properties")


def _validate_array(value: list[Any], schema: dict[str, Any], path: str) -> None:
    _validate_size(value, schema, path, "minItems", "maxItems", "items")
    if schema.get("uniqueItems") is True:
        for index, item in enumerate(value):
            if any(item == previous for previous in value[:index]):
                raise SchemaValidationError(f"{path}[{index}]: duplicate item is not allowed")

    items = schema.get("items")
    if isinstance(items, dict):
        for index, item in enumerate(value):
            validate_json_schema(item, items, f"{path}[{index}]")
    elif isinstance(items, list):
        for index, child_schema in enumerate(items):
            if index < len(value):
                validate_json_schema(value[index], child_schema, f"{path}[{index}]")


def _validate_string(value: str, schema: dict[str, Any], path: str) -> None:
    minimum = schema.get("minLength")
    maximum = schema.get("maxLength")
    if minimum is not None and len(value) < _non_negative_int(minimum, path, "minLength"):
        raise SchemaValidationError(f"{path}: string is shorter than minLength {minimum}")
    if maximum is not None and len(value) > _non_negative_int(maximum, path, "maxLength"):
        raise SchemaValidationError(f"{path}: string is longer than maxLength {maximum}")

    pattern = schema.get("pattern")
    if pattern is not None:
        if not isinstance(pattern, str):
            raise SchemaValidationError(f"{path}: schema pattern must be a string")
        try:
            matched = re.search(pattern, value)
        except re.error as exc:
            raise SchemaValidationError(f"{path}: invalid schema pattern: {exc}") from exc
        if matched is None:
            raise SchemaValidationError(f"{path}: string does not match pattern {pattern!r}")


def _validate_number(value: int | float, schema: dict[str, Any], path: str) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise SchemaValidationError(f"{path}: number must be finite")

    constraints = (
        ("minimum", lambda actual, boundary: actual >= boundary, ">="),
        ("maximum", lambda actual, boundary: actual <= boundary, "<="),
        ("exclusiveMinimum", lambda actual, boundary: actual > boundary, ">"),
        ("exclusiveMaximum", lambda actual, boundary: actual < boundary, "<"),
    )
    for key, predicate, operator in constraints:
        if key not in schema:
            continue
        boundary = schema[key]
        if not _is_number(boundary):
            raise SchemaValidationError(f"{path}: schema {key} must be numeric")
        if not predicate(value, boundary):
            raise SchemaValidationError(f"{path}: number must be {operator} {boundary}")


def _validate_size(
    value: list[Any] | dict[str, Any],
    schema: dict[str, Any],
    path: str,
    min_key: str,
    max_key: str,
    noun: str,
) -> None:
    if min_key in schema:
        minimum = _non_negative_int(schema[min_key], path, min_key)
        if len(value) < minimum:
            raise SchemaValidationError(f"{path}: fewer than {minimum} {noun}")
    if max_key in schema:
        maximum = _non_negative_int(schema[max_key], path, max_key)
        if len(value) > maximum:
            raise SchemaValidationError(f"{path}: more than {maximum} {noun}")


def _non_negative_int(value: Any, path: str, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SchemaValidationError(f"{path}: schema {key} must be a non-negative integer")
    return value


def _matches_type(value: Any, expected: str) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": _is_number(value),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }.get(expected, False)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return type(value).__name__


def _property_path(path: str, name: str) -> str:
    return f"{path}.{name}" if name.isidentifier() else f"{path}[{name!r}]"
