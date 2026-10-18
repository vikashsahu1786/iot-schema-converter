#!/usr/bin/env python3
"""Convert IoT JSON records from one device/vendor schema to another.

Example:
    python iot_schema_converter.py examples/iot_vendor_payload.json \
        --mapping examples/mapping.json \
        --output converted.json

The mapping file defines target fields and their source paths. See
examples/mapping.json for supported transformations.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class ConversionError(Exception):
    """An input record or mapping could not be converted."""


MISSING = object()
PATH_TOKEN = re.compile(r"([^[\]]+)|\[(\d+)\]")


def source_path_tokens(path: str) -> list[str | int]:
    """Parse paths such as ``telemetry.temperature`` or ``sensors[0].value``."""
    if not isinstance(path, str) or not path:
        raise ConversionError("source paths must be non-empty strings")

    tokens: list[str | int] = []
    for segment in path.split("."):
        if not segment:
            raise ConversionError(f"invalid source path: {path!r}")
        position = 0
        previous_was_index = False
        for match in PATH_TOKEN.finditer(segment):
            if match.start() != position:
                raise ConversionError(f"invalid source path: {path!r}")
            if previous_was_index and match.group(1) is not None:
                raise ConversionError(f"invalid source path: {path!r}")
            tokens.append(match.group(1) if match.group(1) is not None else int(match.group(2)))
            previous_was_index = match.group(2) is not None
            position = match.end()
        if position != len(segment):
            raise ConversionError(f"invalid source path: {path!r}")
    return tokens


def get_source_value(record: dict[str, Any], path: str) -> Any:
    current: Any = record
    for token in source_path_tokens(path):
        if isinstance(token, int):
            if not isinstance(current, list) or token >= len(current):
                return MISSING
            current = current[token]
        else:
            if not isinstance(current, dict) or token not in current:
                return MISSING
            current = current[token]
    return current


def set_target_value(target: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    if any(not part for part in parts):
        raise ConversionError(f"invalid target path: {path!r}")

    current = target
    for part in parts[:-1]:
        if part not in current:
            existing = {}
            current[part] = existing
        else:
            existing = current[part]
        if not isinstance(existing, dict):
            raise ConversionError(
                f"target path collision at {part!r} while setting {path!r}"
            )
        current = existing

    leaf = parts[-1]
    if leaf in current:
        raise ConversionError(f"target path collision at {path!r}")
    current[leaf] = value


def require_number(value: Any, operation: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConversionError(f"{operation} requires a numeric value")
    try:
        number = float(value)
    except (OverflowError, ValueError) as error:
        raise ConversionError(f"{operation} requires a finite numeric value") from error
    if not math.isfinite(number):
        raise ConversionError(f"{operation} requires a finite numeric value")
    return number


def require_finite_result(value: float, operation: str) -> float:
    if not math.isfinite(value):
        raise ConversionError(f"{operation} produced a non-finite result")
    return value


def datetime_from_unix_timestamp(value: int | float) -> datetime:
    error_message = "timestamp_iso requires a finite, in-range Unix timestamp"
    try:
        epoch_value = float(value)
    except (OverflowError, ValueError) as error:
        raise ConversionError(error_message) from error
    if not math.isfinite(epoch_value):
        raise ConversionError(error_message)

    # IoT systems commonly send either Unix seconds or Unix milliseconds.
    seconds = epoch_value / 1000 if abs(epoch_value) >= 100_000_000_000 else epoch_value
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as error:
        raise ConversionError(error_message) from error


def to_iso_timestamp(value: Any) -> str:
    if isinstance(value, bool):
        raise ConversionError("timestamp_iso does not accept boolean values")

    if isinstance(value, (int, float)):
        result = datetime_from_unix_timestamp(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ConversionError("timestamp_iso received an empty string")
        try:
            numeric = float(text)
        except ValueError:
            normalized = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
            try:
                result = datetime.fromisoformat(normalized)
            except ValueError as error:
                raise ConversionError(f"invalid timestamp: {value!r}") from error
            if result.tzinfo is None:
                result = result.replace(tzinfo=timezone.utc)
        else:
            result = datetime_from_unix_timestamp(numeric)
    else:
        raise ConversionError("timestamp_iso requires a Unix timestamp or ISO string")

    return result.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def get_location_options(
    options: dict[str, Any],
    operation: str,
) -> tuple[str, list[str], bool]:
    separator = options.get("separator", "/")
    levels = options.get(
        "levels",
        ["country", "city", "area", "factory", "section"],
    )
    escape_separator = options.get("escape_separator", False)
    if not isinstance(separator, str) or not separator:
        raise ConversionError(f"{operation} 'separator' must be a non-empty string")
    if not isinstance(escape_separator, bool):
        raise ConversionError(f"{operation} 'escape_separator' must be a boolean")
    if (
        not isinstance(levels, list)
        or not levels
        or any(not isinstance(level, str) or not level.strip() for level in levels)
        or len(set(levels)) != len(levels)
    ):
        raise ConversionError(
            f"{operation} 'levels' must be a non-empty list of unique field names"
        )
    return separator, levels, escape_separator


def get_optional_location_levels(
    options: dict[str, Any],
    operation: str,
    levels: list[str],
) -> set[str]:
    optional_levels = options.get("optional_levels", [])
    if (
        not isinstance(optional_levels, list)
        or any(not isinstance(level, str) or not level.strip() for level in optional_levels)
        or len(set(optional_levels)) != len(optional_levels)
    ):
        raise ConversionError(
            f"{operation} 'optional_levels' must be a list of unique non-empty field names"
        )

    unknown_optional_levels = [
        level for level in optional_levels if level not in levels
    ]
    if unknown_optional_levels:
        raise ConversionError(
            f"{operation} 'optional_levels' must be included in 'levels': "
            + ", ".join(unknown_optional_levels)
        )

    optional_set = set(optional_levels)
    if optional_set:
        trailing_levels = levels[len(levels) - len(optional_set) :]
        if optional_set != set(trailing_levels):
            raise ConversionError(
                f"{operation} 'optional_levels' must form a trailing suffix of 'levels'"
            )
    return optional_set


def validate_location_path_settings(fields: dict[str, Any]) -> None:
    """Validate location_path options without needing a record to transform."""
    for target_path, specification in fields.items():
        if not isinstance(specification, dict):
            continue
        transforms = specification.get("transforms", [])
        if not isinstance(transforms, list):
            continue
        for transform in transforms:
            if not isinstance(transform, dict) or transform.get("op") != "location_path":
                continue
            try:
                _, levels, _ = get_location_options(transform, "location_path")
                get_optional_location_levels(transform, "location_path", levels)
            except ConversionError as error:
                raise ConversionError(f"field {target_path!r}: {error}") from error


def get_location_escape_characters(separator: str) -> tuple[str, str | None]:
    """Choose escape symbols that cannot be confused with the configured separator."""
    if "\\" not in separator:
        # Preserve the original wire format for all existing separators.
        return "\\", None

    candidates = "~^|!%#@;=+?"
    used = set(separator)
    escape_character = next((char for char in candidates if char not in used), None)
    backslash_code = next(
        (
            char
            for char in candidates
            if char not in used and char != escape_character
        ),
        None,
    )

    # Fall back to Unicode scalar values if the separator contains every
    # preferred printable symbol. These symbols are escaped in location names.
    if escape_character is None or backslash_code is None:
        for codepoint in range(0x110000):
            if 0xD800 <= codepoint <= 0xDFFF:
                continue
            char = chr(codepoint)
            if char in used or char in (escape_character, backslash_code):
                continue
            if escape_character is None:
                escape_character = char
            else:
                backslash_code = char
                if escape_character is not None:
                    break

    if escape_character is None or backslash_code is None:
        raise ConversionError("separator leaves no available location escape symbols")
    return escape_character, backslash_code


def split_escaped_location_path(
    value: str,
    separator: str,
    operation: str,
    escape_character: str = "\\",
    backslash_code: str | None = None,
) -> list[str]:
    """Split a path using separator-safe escapes for delimiters and backslashes."""
    parts: list[str] = []
    part: list[str] = []
    position = 0

    while position < len(value):
        if value[position] == escape_character:
            if value.startswith(escape_character, position + 1):
                part.append(escape_character)
                position += 2
            elif value.startswith(separator, position + 1):
                part.append(separator)
                position += len(separator) + 1
            elif (
                backslash_code is not None
                and value.startswith(backslash_code, position + 1)
            ):
                part.append("\\")
                position += 2
            else:
                raise ConversionError(
                    f"{operation} contains an invalid escape sequence"
                )
        elif value.startswith(separator, position):
            parts.append("".join(part))
            part = []
            position += len(separator)
        else:
            part.append(value[position])
            position += 1

    parts.append("".join(part).strip())
    return parts


def split_location_hierarchy(value: Any, options: dict[str, Any]) -> dict[str, str]:
    """Convert a delimited location path into named hierarchy fields."""
    if not isinstance(value, str):
        raise ConversionError("location_hierarchy requires a slash-delimited string")

    separator, levels, escape_separator = get_location_options(
        options, "location_hierarchy"
    )
    optional_levels = get_optional_location_levels(
        options, "location_hierarchy", levels
    )

    if escape_separator:
        escape_character, backslash_code = get_location_escape_characters(separator)
        parts = split_escaped_location_path(
            value,
            separator,
            "location_hierarchy",
            escape_character,
            backslash_code,
        )
    else:
        parts = [part.strip() for part in value.split(separator)]
    if optional_levels:
        minimum_parts = len(levels) - len(optional_levels)
        if not minimum_parts <= len(parts) <= len(levels):
            raise ConversionError(
                f"location_hierarchy expected between {minimum_parts} and "
                f"{len(levels)} parts with optional trailing levels "
                f"({', '.join(levels)}), received {len(parts)}"
            )
    elif len(parts) != len(levels):
        raise ConversionError(
            f"location_hierarchy expected {len(levels)} parts "
            f"({', '.join(levels)}), received {len(parts)}"
        )
    if any(not part.strip() for part in parts):
        raise ConversionError("location_hierarchy does not allow empty location parts")
    return dict(zip(levels, parts))


def join_location_path(value: Any, options: dict[str, Any]) -> str:
    """Convert named hierarchy fields into a delimited location path."""
    if not isinstance(value, dict):
        raise ConversionError("location_path requires an object of named location fields")

    separator, levels, escape_separator = get_location_options(options, "location_path")
    optional_levels = get_optional_location_levels(options, "location_path", levels)
    missing = [
        level
        for level in levels
        if level not in value and level not in optional_levels
    ]
    if missing:
        raise ConversionError(
            f"location_path is missing required location fields: {', '.join(missing)}"
        )

    parts: list[str] = []
    if escape_separator:
        escape_character, backslash_code = get_location_escape_characters(separator)
    for level in levels:
        if level not in value:
            continue
        part = value[level]
        if not isinstance(part, str):
            raise ConversionError(f"location_path field {level!r} must be a non-empty string")
        if not part.strip():
            raise ConversionError(f"location_path field {level!r} must be a non-empty string")
        if escape_separator:
            encoded: list[str] = []
            position = 0
            while position < len(part):
                if part.startswith(separator, position):
                    encoded.extend((escape_character, separator))
                    position += len(separator)
                elif part[position] == escape_character:
                    encoded.extend((escape_character, escape_character))
                    position += 1
                elif part[position] == "\\":
                    if backslash_code is None:
                        encoded.extend((escape_character, escape_character))
                    else:
                        encoded.extend((escape_character, backslash_code))
                    position += 1
                else:
                    encoded.append(part[position])
                    position += 1
            part = "".join(encoded)
        else:
            part = part.strip()
        parts.append(part)
    if not parts:
        raise ConversionError("location_path must contain at least one location field")
    return separator.join(parts)


def apply_transform(value: Any, transform: Any) -> Any:
    if isinstance(transform, str):
        operation = transform
        options: dict[str, Any] = {}
    elif isinstance(transform, dict):
        operation = transform.get("op")
        options = transform
    else:
        raise ConversionError("each transform must be a name or an object with an 'op'")

    if not isinstance(operation, str):
        raise ConversionError("transform objects must include a string 'op'")

    if operation == "c_to_f":
        number = require_number(value, operation)
        return require_finite_result(number * (9 / 5) + 32, operation)
    if operation == "f_to_c":
        number = require_number(value, operation)
        return require_finite_result((number - 32) * (5 / 9), operation)
    if operation == "scale":
        number = require_number(value, operation)
        factor = require_number(options.get("factor"), operation)
        offset = require_number(options.get("offset", 0), operation)
        return require_finite_result(number * factor + offset, operation)
    if operation == "round":
        number = require_number(value, operation)
        digits = options.get("digits", 2)
        if isinstance(digits, bool) or not isinstance(digits, int):
            raise ConversionError("round 'digits' must be an integer")
        return round(number, digits)
    if operation == "to_number":
        if isinstance(value, bool):
            raise ConversionError("to_number does not accept boolean values")
        try:
            number = float(value)
        except (TypeError, ValueError) as error:
            raise ConversionError(f"cannot convert {value!r} to a number") from error
        if not math.isfinite(number):
            raise ConversionError("to_number requires a finite numeric value")
        return int(number) if number.is_integer() else number
    if operation == "to_string":
        return str(value)
    if operation == "to_bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "yes", "1", "on"}:
                return True
            if normalized in {"false", "no", "0", "off"}:
                return False
        raise ConversionError(f"cannot convert {value!r} to a boolean")
    if operation == "timestamp_iso":
        return to_iso_timestamp(value)
    if operation == "location_hierarchy":
        return split_location_hierarchy(value, options)
    if operation == "location_path":
        return join_location_path(value, options)

    raise ConversionError(f"unsupported transform: {operation!r}")


def convert_field(
    record: dict[str, Any],
    target_path: str,
    specification: Any,
    strict: bool,
) -> Any:
    if isinstance(specification, str):
        source = specification
        transforms: Any = []
        has_default = False
        default = None
        required = False
    elif isinstance(specification, dict):
        has_source = "source" in specification
        has_value = "value" in specification
        if has_source == has_value:
            raise ConversionError(
                f"mapping for {target_path!r} must contain exactly one of 'source' or 'value'"
            )
        if has_value:
            source = None
            value = specification["value"]
        else:
            source = specification["source"]
            if not isinstance(source, str) or not source:
                raise ConversionError(f"mapping for {target_path!r} has an invalid source")
        transforms = specification.get("transforms", [])
        has_default = "default" in specification
        default = specification.get("default")
        required = specification.get("required", False)
        if not isinstance(required, bool):
            raise ConversionError(f"mapping for {target_path!r}: 'required' must be boolean")
        if has_value:
            transforms = specification.get("transforms", [])
            value = specification["value"]
    else:
        raise ConversionError(
            f"mapping for {target_path!r} must be a source path or an object"
        )

    if not isinstance(transforms, list):
        raise ConversionError(f"mapping for {target_path!r}: 'transforms' must be a list")

    if isinstance(specification, dict) and "value" in specification:
        result = value
    else:
        result = get_source_value(record, source)
        if result is MISSING:
            if has_default:
                result = default
            elif strict or required:
                raise ConversionError(
                    f"missing source field {source!r} for target {target_path!r}"
                )
            else:
                return MISSING

    for transform in transforms:
        result = apply_transform(result, transform)
    return result


def load_mapping(path: Path) -> dict[str, Any]:
    try:
        mapping = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ConversionError(f"cannot read mapping file {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise ConversionError(
            f"invalid JSON in mapping file {path} at line {error.lineno}, "
            f"column {error.colno}: {error.msg}"
        ) from error

    if not isinstance(mapping, dict) or not isinstance(mapping.get("fields"), dict):
        raise ConversionError("mapping file must be a JSON object with a 'fields' object")
    if not mapping["fields"]:
        raise ConversionError("mapping file 'fields' object cannot be empty")
    return mapping["fields"]


def convert_record(
    record: dict[str, Any],
    fields: dict[str, Any],
    record_number: int,
    strict: bool,
) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for target_path, specification in fields.items():
        if not isinstance(target_path, str) or not target_path:
            raise ConversionError("target field names must be non-empty strings")
        try:
            value = convert_field(record, target_path, specification, strict)
            if value is not MISSING:
                set_target_value(output, target_path, value)
        except ConversionError as error:
            raise ConversionError(f"record {record_number}, field {target_path!r}: {error}") from error
    return output


def read_json_input(source: str) -> Any:
    try:
        text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    except OSError as error:
        raise ConversionError(f"cannot read input {source!r}: {error}") from error

    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise ConversionError(
            f"invalid JSON input at line {error.lineno}, column {error.colno}: {error.msg}"
        ) from error


def write_json_output(destination: str, value: Any, compact: bool) -> None:
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":") if compact else None,
            indent=None if compact else 2,
        )
        if destination == "-":
            sys.stdout.write(text + "\n")
        else:
            Path(destination).write_text(text + "\n", encoding="utf-8")
    except (OSError, TypeError, ValueError) as error:
        raise ConversionError(f"cannot write output {destination!r}: {error}") from error


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert IoT JSON records using a configurable source-to-target field mapping."
    )
    parser.add_argument(
        "input",
        help="input JSON file containing one record or a list of records; use '-' for stdin",
    )
    parser.add_argument(
        "--mapping",
        required=True,
        type=Path,
        help="JSON mapping file (see examples/mapping.json)",
    )
    parser.add_argument(
        "--output",
        "-o",
        default="-",
        help="output JSON file; defaults to stdout, and '-' also means stdout",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="fail when a mapped source field is missing instead of omitting that target field",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="write compact JSON instead of indented JSON",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        fields = load_mapping(args.mapping)
        data = read_json_input(args.input)
        validate_location_path_settings(fields)
        if isinstance(data, dict):
            result = convert_record(data, fields, 1, args.strict)
        elif isinstance(data, list):
            result = []
            for index, record in enumerate(data, start=1):
                if not isinstance(record, dict):
                    raise ConversionError(f"record {index} must be a JSON object")
                result.append(convert_record(record, fields, index, args.strict))
        else:
            raise ConversionError("input JSON must be an object or a list of objects")
        write_json_output(args.output, result, args.compact)
    except ConversionError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
