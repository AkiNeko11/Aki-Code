"""A deliberately small, strict JSON Schema subset for workflow results.

Unsupported keywords fail at registration, rather than silently accepting an
output that has never been validated.
"""
from __future__ import annotations

import json
import math

from aki_code.workflow.model import WorkflowInputError


_TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}
_KEYS = {
    "type", "properties", "required", "additionalProperties", "items", "enum",
    "minItems", "maxItems", "minLength", "maxLength", "minimum", "maximum",
    "description",
}


def _error(path, message):
    raise WorkflowInputError(f"{path}: {message}")


def _number(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and (not isinstance(value, float) or math.isfinite(value)))


def _json_value(value, path):
    if value is None or isinstance(value, (str, bool)):
        return
    if _number(value):
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _json_value(item, f"{path}[{index}]")
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for key, item in value.items():
            _json_value(item, f"{path}.{key}")
        return
    _error(path, "expected a finite JSON value")


def _equal(left, right):
    # JSON booleans are not numbers, despite bool being a Python int subclass.
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if _number(left) and _number(right):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, list):
        return len(left) == len(right) and all(_equal(a, b) for a, b in zip(left, right))
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_equal(left[k], right[k]) for k in left)
    return left == right


def check_schema(schema):
    """Validate a schema definition; return it unchanged on success."""
    _check_schema(schema, "$schema")
    return schema


def _check_schema(schema, path):
    if isinstance(schema, bool):
        return
    if not isinstance(schema, dict):
        _error(path, "schema must be an object or boolean")
    unknown = set(schema) - _KEYS
    if unknown:
        _error(path, f"unsupported keyword(s): {', '.join(sorted(map(str, unknown)))}")
    if "type" in schema:
        kinds = schema["type"]
        kinds = [kinds] if isinstance(kinds, str) else kinds
        if (not isinstance(kinds, list) or not kinds
                or not all(isinstance(kind, str) and kind in _TYPES for kind in kinds)
                or len(set(kinds)) != len(kinds)):
            _error(path, "type must name supported, unique JSON types")
    if "description" in schema and not isinstance(schema["description"], str):
        _error(path, "description must be a string")
    if "properties" in schema:
        props = schema["properties"]
        if not isinstance(props, dict) or not all(isinstance(k, str) for k in props):
            _error(path, "properties must be an object with string keys")
        for key, child in props.items():
            _check_schema(child, f"{path}.properties.{key}")
    if "required" in schema:
        required = schema["required"]
        if (not isinstance(required, list) or not all(isinstance(k, str) for k in required)
                or len(set(required)) != len(required)):
            _error(path, "required must be an array of unique strings")
    for key in ("additionalProperties", "items"):
        if key in schema:
            _check_schema(schema[key], f"{path}.{key}")
    for low, high in (("minItems", "maxItems"), ("minLength", "maxLength")):
        for key in (low, high):
            if key in schema:
                value = schema[key]
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    _error(path, f"{key} must be a non-negative integer")
        if low in schema and high in schema and schema[low] > schema[high]:
            _error(path, f"{low} cannot exceed {high}")
    for key in ("minimum", "maximum"):
        if key in schema and not _number(schema[key]):
            _error(path, f"{key} must be a finite number")
    if "minimum" in schema and "maximum" in schema and schema["minimum"] > schema["maximum"]:
        _error(path, "minimum cannot exceed maximum")
    if "enum" in schema:
        values = schema["enum"]
        if not isinstance(values, list) or not values:
            _error(path, "enum must be a non-empty array")
        _json_value(values, f"{path}.enum")
        for index, value in enumerate(values):
            if any(_equal(value, old) for old in values[:index]):
                _error(path, "enum values must be unique")


def _matches(value, kind):
    return {
        "object": lambda: isinstance(value, dict),
        "array": lambda: isinstance(value, list),
        "string": lambda: isinstance(value, str),
        "integer": lambda: _number(value) and (isinstance(value, int) or value.is_integer()),
        "number": lambda: _number(value),
        "boolean": lambda: isinstance(value, bool),
        "null": lambda: value is None,
    }[kind]()


def validate_value(value, schema):
    """Validate both definition and JSON value, raising WorkflowInputError."""
    check_schema(schema)
    _json_value(value, "$")
    _validate(value, schema, "$")


def _validate(value, schema, path):
    if schema is True:
        return
    if schema is False:
        _error(path, "value is forbidden by schema")
    kinds = schema.get("type")
    if kinds is not None:
        kinds = [kinds] if isinstance(kinds, str) else kinds
        if not any(_matches(value, kind) for kind in kinds):
            _error(path, f"expected {' or '.join(kinds)}")
    if "enum" in schema and not any(_equal(value, item) for item in schema["enum"]):
        _error(path, "value is not in enum")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                _error(path, f"missing required property {key!r}")
        properties = schema.get("properties", {})
        for key, child in value.items():
            _validate(child, properties.get(key, schema.get("additionalProperties", True)), f"{path}.{key}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", math.inf):
            _error(path, "array length is outside schema limits")
        if "items" in schema:
            for index, child in enumerate(value):
                _validate(child, schema["items"], f"{path}[{index}]")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", math.inf):
            _error(path, "string length is outside schema limits")
    if _number(value):
        if value < schema.get("minimum", -math.inf) or value > schema.get("maximum", math.inf):
            _error(path, "number is outside schema limits")


def parse_output(text, schema):
    """Parse a JSON-only answer; reject NaN, infinity and duplicate properties."""
    def reject_constant(value):
        raise WorkflowInputError(f"Invalid JSON constant: {value}")

    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise WorkflowInputError(f"Duplicate JSON property: {key}")
            result[key] = value
        return result

    if not isinstance(text, str):
        raise WorkflowInputError("Agent output must be JSON text")
    try:
        value = json.loads(text, parse_constant=reject_constant, object_pairs_hook=unique_pairs)
    except (ValueError, TypeError, RecursionError) as exc:
        raise WorkflowInputError(f"Agent output is not valid JSON: {exc}") from exc
    validate_value(value, schema)
    return value
