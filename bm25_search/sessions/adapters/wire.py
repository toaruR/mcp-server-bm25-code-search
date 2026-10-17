"""Bounded decoder for the specific protobuf fields characterized in T0."""
from datetime import datetime, timezone


def varint(data, offset):
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            raise ValueError("truncated_varint")
        byte = data[offset]
        offset += 1
        if shift == 63 and byte > 1:
            raise ValueError("overflow_varint")
        value |= (byte & 127) << shift
        if byte < 128:
            return value, offset
    raise ValueError("overflow_varint")


def fields(data):
    if not isinstance(data, bytes) or len(data) > 16 * 1024 * 1024:
        raise ValueError("invalid_blob")
    output, offset = [], 0
    while offset < len(data):
        key, offset = varint(data, offset)
        number, wire = key >> 3, key & 7
        if not 0 < number < (1 << 29):
            raise ValueError("invalid_field")
        if wire == 0:
            value, offset = varint(data, offset)
        elif wire in (1, 2, 5):
            if wire == 2:
                size, offset = varint(data, offset)
            else:
                size = 8 if wire == 1 else 4
            if offset + size > len(data):
                raise ValueError("truncated_field")
            value, offset = data[offset:offset + size], offset + size
        else:
            raise ValueError("unsupported_wire")
        output.append((number, wire, value))
        if len(output) > 100000:
            raise ValueError("field_budget_exceeded")
    return output


def blobs(data, number):
    return [value for field, wire, value in fields(data) if field == number and wire == 2]


def strings(data, number):
    return [value.decode("utf-8") for value in blobs(data, number)]


def string(data, number):
    return "\n".join(strings(data, number))


def integer(data, number, default=None):
    return next((value for field, wire, value in fields(data) if field == number and wire == 0), default)


def stamp(metadata):
    values = blobs(metadata, 1)
    if not values:
        return None
    seconds = integer(values[0], 1)
    if seconds is None:
        return None
    nanos = integer(values[0], 2, 0)
    if not 0 <= nanos < 1000000000:
        raise ValueError("invalid_timestamp")
    return datetime.fromtimestamp(seconds, timezone.utc).replace(microsecond=nanos // 1000).isoformat(timespec="microseconds")
