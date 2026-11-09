"""Small, byte-preserving three-way merge for text files."""

from __future__ import annotations

from dataclasses import dataclass
import difflib


@dataclass(frozen=True)
class Merge:
    status: str
    content: bytes | None
    conflicts: int


def _changes(base: list[bytes], other: list[bytes]) -> list[tuple[int, int, list[bytes]]]:
    matcher = difflib.SequenceMatcher(None, base, other, autojunk=False)
    return [
        (start, end, other[other_start:other_end])
        for tag, start, end, other_start, other_end in matcher.get_opcodes()
        if tag != "equal"
    ]


def _overlap(left: tuple[int, int, list[bytes]], right: tuple[int, int, list[bytes]]) -> bool:
    """Whether two edits claim the same part of the base.

    Insertions at a replacement boundary are independent, while two insertions
    at exactly the same boundary compete for the same zero-width region.
    """
    start, end, _ = left
    other_start, other_end, _ = right
    if start == end and other_start == other_end:
        return start == other_start
    if start == end:
        return other_start <= start <= other_end
    if other_start == other_end:
        return start <= other_start <= end
    return start <= other_end and other_start <= end


def _apply(base: list[bytes], start: int, end: int,
           changes: list[tuple[int, int, list[bytes]]]) -> list[bytes]:
    result: list[bytes] = []
    cursor = start
    for change_start, change_end, replacement in sorted(changes):
        result.extend(base[cursor:change_start])
        result.extend(replacement)
        cursor = change_end
    result.extend(base[cursor:end])
    return result


def merge3(base: bytes, ours: bytes, theirs: bytes) -> Merge:
    """Merge two text revisions of *base* without manufacturing markers."""
    if any(b"\x00" in value for value in (base, ours, theirs)):
        return Merge("binary", None, 0)
    try:
        for value in (base, ours, theirs):
            value.decode("utf-8")
    except UnicodeDecodeError:
        return Merge("binary", None, 0)

    if ours == theirs:
        return Merge("clean", ours, 0)
    if base == ours:
        return Merge("clean", theirs, 0)
    if base == theirs:
        return Merge("clean", ours, 0)

    base_lines = base.splitlines(keepends=True)
    ours_changes = _changes(base_lines, ours.splitlines(keepends=True))
    theirs_changes = _changes(base_lines, theirs.splitlines(keepends=True))
    tagged = [("ours", change) for change in ours_changes] + [("theirs", change) for change in theirs_changes]
    # Connected components of overlapping edit regions are the diff3 regions.
    pending = set(range(len(tagged)))
    regions: list[list[tuple[str, tuple[int, int, list[bytes]]]]] = []
    while pending:
        component = [pending.pop()]
        expanded = True
        while expanded:
            expanded = False
            for index in list(pending):
                if any(_overlap(tagged[index][1], tagged[member][1]) for member in component):
                    pending.remove(index)
                    component.append(index)
                    expanded = True
        regions.append([tagged[index] for index in component])
    regions.sort(key=lambda region: (min(change[0] for _, change in region),
                                     max(change[1] for _, change in region)))

    result: list[bytes] = []
    cursor = 0
    conflicts = 0
    for region in regions:
        start = min(change[0] for _, change in region)
        end = max(change[1] for _, change in region)
        if start < cursor:
            # This can only arise from a malformed overlap grouping.
            return Merge("conflict", None, conflicts + 1)
        result.extend(base_lines[cursor:start])
        ours_region = [change for side, change in region if side == "ours"]
        theirs_region = [change for side, change in region if side == "theirs"]
        if not ours_region:
            result.extend(_apply(base_lines, start, end, theirs_region))
        elif not theirs_region:
            result.extend(_apply(base_lines, start, end, ours_region))
        else:
            ours_output = _apply(base_lines, start, end, ours_region)
            theirs_output = _apply(base_lines, start, end, theirs_region)
            if ours_output != theirs_output:
                conflicts += 1
            else:
                result.extend(ours_output)
        cursor = end
    if conflicts:
        return Merge("conflict", None, conflicts)
    result.extend(base_lines[cursor:])
    return Merge("clean", b"".join(result), 0)
