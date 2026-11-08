#!/usr/bin/env python3
"""Read and write Bitwig's plain document format (presets, projects).

Worked out from presets Bitwig 6.1 wrote, not from any documentation, and
checked by reading a preset and writing it back byte for byte.

    header   42 ASCII bytes: "BtWg", then hex fields; [16:24] is where the
             document object starts, [32:40] where the appended zip starts
             (zero when there is none)
    meta     between the header and the document: a key/value block padded
             with spaces, copied through unchanged here
    document one object: a u32 class id, then fields up to a u32 zero; a field
             is a u32 id, a type byte and a value
    zip      files the document embeds, e.g. the .multisample a Sampler loaded

Objects are numbered from 1 in the order they are written. A reference to an
earlier one is either a field of type REF holding its number, or, where an
object is expected, the class id 1 followed by the number.
"""

import struct

INT8, INT16, INT32, BOOL, FLOAT, DOUBLE, STRING = 0x01, 0x02, 0x03, 0x05, 0x06, 0x07, 0x08
OBJECT, NULL, REF, BLOB, LIST, UUID, COLOR = 0x09, 0x0A, 0x0B, 0x0D, 0x12, 0x15, 0x16

FIXED = {INT16: 2, INT32: 4, FLOAT: 4, DOUBLE: 8, UUID: 16, COLOR: 16}


class Obj:
    __slots__ = ("cls", "fields")

    def __init__(self, cls, fields):
        self.cls, self.fields = cls, fields  # [[field id, type, value], ...]

    def field(self, fid):
        for f in self.fields:
            if f[0] == fid:
                return f
        return None

    def get(self, fid, default=None):
        f = self.field(fid)
        return default if f is None else f[2]

    def set(self, fid, value):
        f = self.field(fid)
        if f is None:
            raise KeyError(f"no field {fid:#06x} in class {self.cls:#06x}")
        f[2] = value

    def name(self):
        """The object's own name (field 0x2b9), which most of them carry."""
        f = self.field(0x2B9)
        return f[2][1].decode("utf-8", "replace") if f and f[1] == STRING else ""


class Ref:
    """A pointer to an object written earlier."""
    __slots__ = ("target",)

    def __init__(self, target):
        self.target = target


def string(text):
    raw = text.encode("utf-8")
    return (len(raw), raw)


def text(value):
    return value[1].decode("utf-8", "replace")


class Reader:
    def __init__(self, data, pos=0):
        self.d, self.p = data, pos
        self.objects = []

    def u32(self):
        v = struct.unpack_from(">I", self.d, self.p)[0]
        self.p += 4
        return v

    def value(self, t):
        d = self.d
        if t in (INT8, BOOL):
            v = d[self.p]
            self.p += 1
            return v
        if t in FIXED:
            n = FIXED[t]
            v = d[self.p:self.p + n]
            self.p += n
            return v
        if t == BLOB:
            n = self.u32()
            v = d[self.p:self.p + n]
            self.p += n
            return v
        if t == STRING:
            n = self.u32()
            size = (n & 0x7FFFFFFF) * (2 if n & 0x80000000 else 1)
            v = (n, d[self.p:self.p + size])
            self.p += size
            return v
        if t == OBJECT:
            return self.obj()
        if t == NULL:
            return None
        if t == REF:
            return Ref(self.objects[self.u32() - 1])
        if t == LIST:
            items = []
            while True:
                cls = self.u32()
                if cls == 3:
                    return items
                items.append(self.obj(cls))
        raise ValueError(f"unknown type 0x{t:02x} at {self.p - 1}")

    def obj(self, cls=None):
        if cls is None:
            cls = self.u32()
            if cls == 0:
                return None
        if cls == 1:
            return Ref(self.objects[self.u32() - 1])
        o = Obj(cls, [])
        self.objects.append(o)
        while True:
            fid = self.u32()
            if fid == 0:
                return o
            t = self.d[self.p]
            self.p += 1
            o.fields.append([fid, t, self.value(t)])


class Writer:
    def __init__(self):
        self.out = bytearray()
        self.index = {}

    def u32(self, v):
        self.out += struct.pack(">I", v)

    def value(self, t, v):
        if t in (INT8, BOOL):
            self.out.append(v)
        elif t in FIXED:
            self.out += v
        elif t == BLOB:
            self.u32(len(v))
            self.out += v
        elif t == STRING:
            n, raw = v
            self.u32(n)
            self.out += raw
        elif t == OBJECT:
            if v is None:
                self.u32(0)
            else:
                self.obj(v)
        elif t == NULL:
            pass
        elif t == REF:
            self.u32(self.index[id(v.target)])
        elif t == LIST:
            for item in v:
                self.obj(item)
            self.u32(3)
        else:
            raise ValueError(f"cannot write type 0x{t:02x}")

    def obj(self, o):
        if isinstance(o, Ref):
            self.u32(1)
            self.u32(self.index[id(o.target)])
            return
        self.index[id(o)] = len(self.index) + 1
        self.u32(o.cls)
        for fid, t, v in o.fields:
            self.u32(fid)
            self.out.append(t)
            self.value(t, v)
        self.u32(0)


class Document:
    """A whole file: header, meta block, document object, appended zip."""

    def __init__(self, data):
        self.header = bytearray(data[:42])
        if self.header[:4] != b"BtWg":
            raise ValueError("not a Bitwig document")
        start = int(self.header[16:24], 16)
        zip_at = int(self.header[32:40], 16) or len(data)
        self.meta = bytearray(data[42:start])
        reader = Reader(data, start)
        self.root = reader.obj()
        self.objects = reader.objects
        if reader.p != zip_at:
            raise ValueError(f"document ends at {reader.p}, zip at {zip_at}")
        self.zip = data[zip_at:]

    def to_bytes(self, zip_bytes=None):
        zip_bytes = self.zip if zip_bytes is None else zip_bytes
        w = Writer()
        w.obj(self.root)
        header = bytearray(self.header)
        start = 42 + len(self.meta)
        header[16:24] = b"%08x" % start
        header[32:40] = b"%08x" % (start + len(w.out)) if zip_bytes else b"0" * 8
        return bytes(header) + bytes(self.meta) + bytes(w.out) + zip_bytes


def walk(o, seen=None):
    """Every object under `o`, each once, in writing order."""
    seen = set() if seen is None else seen
    if not isinstance(o, Obj) or id(o) in seen:
        return
    seen.add(id(o))
    yield o
    for _, t, v in o.fields:
        if t == OBJECT:
            yield from walk(v, seen)
        elif t == LIST:
            for item in v:
                yield from walk(item, seen)


def clone(o, memo=None):
    """A deep copy of `o`. References into the copied tree follow the copy;
    references out of it keep pointing where they did."""
    memo = {} if memo is None else memo
    if isinstance(o, Ref):
        return Ref(memo.get(id(o.target), o.target))
    if not isinstance(o, Obj):
        return o
    n = Obj(o.cls, [])
    memo[id(o)] = n
    for fid, t, v in o.fields:
        if t in (OBJECT, REF):
            v = clone(v, memo) if v is not None else None
        elif t == LIST:
            v = [clone(item, memo) for item in v]
        n.fields.append([fid, t, v])
    return n
