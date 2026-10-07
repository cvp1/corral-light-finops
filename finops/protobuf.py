"""A minimal protobuf wire-format reader: enough to pick numbered fields out
of Antigravity's step metadata. It never decodes a string it was not asked
for, so message text is not touched."""
import struct


class Malformed(ValueError):
    pass


def _varint(b, i):
    r = s = 0
    while True:
        if i >= len(b) or s > 63:
            raise Malformed("truncated varint")
        c = b[i]
        i += 1
        r |= (c & 0x7F) << s
        s += 7
        if not c & 0x80:
            return r, i


def fields(b):
    """bytes -> [(field, wire, value)]; value is an int for varints and
    fixed ints, bytes for length-delimited fields."""
    i, out = 0, []
    while i < len(b):
        key, i = _varint(b, i)
        f, w = key >> 3, key & 7
        if f == 0:
            raise Malformed("field 0")
        if w == 0:
            v, i = _varint(b, i)
        elif w == 1:
            if i + 8 > len(b):
                raise Malformed("truncated fixed64")
            v = struct.unpack("<Q", b[i:i + 8])[0]
            i += 8
        elif w == 5:
            if i + 4 > len(b):
                raise Malformed("truncated fixed32")
            v = struct.unpack("<I", b[i:i + 4])[0]
            i += 4
        elif w == 2:
            n, i = _varint(b, i)
            if i + n > len(b):
                raise Malformed("truncated bytes")
            v = bytes(b[i:i + n])
            i += n
        else:
            raise Malformed(f"wire type {w}")
        out.append((f, w, v))
    return out


def first(fs, number, wire=None):
    for f, w, v in fs:
        if f == number and (wire is None or w == wire):
            return v
    return None


def sub(fs, number):
    """The first length-delimited field `number`, parsed as a message, or None."""
    v = first(fs, number, 2)
    if v is None:
        return None
    try:
        return fields(v)
    except Malformed:
        return None


def packed_varints(b):
    out, i = [], 0
    while i < len(b):
        v, i = _varint(b, i)
        out.append(v)
    return out


def ints(fs):
    """{field: int} for the varint fields of a message."""
    return {f: v for f, w, v in fs if w == 0}
