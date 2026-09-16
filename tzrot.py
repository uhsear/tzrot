#!/usr/bin/env python
"""Find date fields reinterpreted as UTC, and count the records whose calendar day moved.

A permit issued at 20:15 on the 5th of July was written to the database as
00:15 on the 6th, because something on the write path converted local time to
UTC and nothing on the read path converted it back. Every value is a valid
datetime, every row looks plausible, and for two years every evening permit was
reported on the following day. It surfaced when a builder disputing a deadline
by one day turned out to be right.

pandas does the conversion properly. One call to tz_localize and one to
tz_convert fixes a whole column, and that is the right tool for the repair. It
has no opinion about whether the values in front of it have already been through
that conversion once, and neither has a validator: nothing is out of range, so
nothing fires. ArcGIS Pro's time zone field property is the setting that was
wrong in the first place, and it describes what the values are meant to be
rather than what they are.

This tool answers the two questions those leave open. Is there a mechanical
shift in this column, and what did it cost. The signature is that every value
lands on local midnight when read as UTC: 04:00:00 at a -04:00 offset, 05:00:00
at -05:00, split at the zone's own daylight saving transitions. The damage is
the number of records whose calendar day moves when the shift is undone.

    python tzrot.py --self-test
    python tzrot.py permits.csv
    python tzrot.py permits.geojson --field ISSUED_DATE --timezone America/New_York
    python tzrot.py permits.csv --json

Nothing is written. It reads a CSV or a GeoJSON and prints what it found.

Exit codes: 0 no shift signature, 1 a shift signature, 2 the file or the
timezone could not be read, 64 usage error.
"""

from __future__ import print_function

import argparse
import csv
import io
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# =============================================================================
# CONFIGURATION. Deliberately not flags. Change here, not at the call site.
# =============================================================================

# The zone the naive values are assumed to mean. Marion County is Eastern, so
# that is the default. It is the only knob that changes an answer, which is why
# it is also a flag.
DEFAULT_TIMEZONE = "America/New_York"

# How many crossing records are printed under the count. The count is the
# finding; the sample is there so somebody can open one record and agree.
DEFAULT_SAMPLE = 5

# A field is auto-detected as a date field only when EVERY non-blank value in it
# parses as a stamp. One unreadable value hides the column from auto-detection
# rather than reporting a count drawn from part of it. --field overrides that
# and says how many values it could not read.
STRICT_AUTODETECT = True

# =============================================================================
# End of CONFIGURATION.
# =============================================================================

# Signature codes.
EMPTY = "EMPTY"
MIXED_AWARENESS = "MIXED_AWARENESS"
ALL_AWARE = "ALL_AWARE"
NO_TIME = "NO_TIME_COMPONENT"
UTC_SHIFT = "UTC_SHIFT"
NO_FINDING = "NO_FINDING"

# The only signature that is a finding. Everything else is either an answer of
# "no" or a refusal to answer.
PROVES_SHIFT = (UTC_SHIFT,)

# A crossing count means something only for these two. An all-midnight column
# crosses on every row and it is not damage, it is a date-only column being a
# date-only column.
COUNTABLE = (UTC_SHIFT, NO_FINDING)

# ISO 8601, and only ISO 8601. The date is required, the time is optional, the
# seconds are optional, a fraction is optional, and an offset is optional. The
# separator may be a T or a space, which is what every database export uses.
#
# m/d/Y is deliberately absent. It cannot be told from d/m/Y without guessing,
# and a tool that guesses the day of the month has no business counting days.
STAMP_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})"
    r"(?:[Tt ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6})\d*)?)?)?"
    r"\s*(Z|z|[+-]\d{2}:?\d{2})?$")

MIDNIGHT = time(0, 0)

# The window a stamp must sit inside to survive an offset conversion at all.
# astimezone() on 9999-12-31 23:59:59 raises OverflowError rather than
# returning a value, and 9999-12-31 is the standard "never expires" sentinel in
# a permit or licensing table, while 0001-01-01 is the standard null one. Both
# arrive in real exports, so this is reached by ordinary files rather than by
# fuzzing. A day of margin covers every offset any zone has ever used; the
# largest on record is under 16 hours.
CONVERTIBLE_MIN = datetime.min + timedelta(days=1)
CONVERTIBLE_MAX = datetime.max - timedelta(days=1)


class Finding(object):
    """What one column's values prove, or refuse to prove, about a shift."""

    def __init__(self, signature, detail, groups=None, transitions=()):
        self.signature = signature
        self.detail = detail
        # utcoffset -> {"count", "time", "name"} for a proved shift. The stored
        # time component is carried per offset because that pairing IS the
        # signature: 04:00:00 at -04:00 and 05:00:00 at -05:00.
        self.groups = groups or {}
        self.transitions = list(transitions)

    @property
    def offsets(self):
        return sorted(self.groups)

    @property
    def proves_shift(self):
        return self.signature in PROVES_SHIFT

    def __repr__(self):
        return "Finding(%s)" % self.signature


class Report(object):
    """One field: what was read, what it proves, and what it cost."""

    def __init__(self, field, zone, finding, values, crossed,
                 unreadable=0, blank=0):
        self.field = field
        self.zone = zone
        self.finding = finding
        self.values = values
        # None when a crossing count would be misleading, a list otherwise.
        self.crossed = crossed
        self.unreadable = unreadable
        self.blank = blank

    @property
    def count(self):
        return len(self.values)

    def __repr__(self):
        return "Report(%s, %s, %d value(s))" % (
            self.field, self.finding.signature, self.count)


# ----------------------------------------------------------------- pure core

def _offset_of(text):
    """The tzinfo an ISO offset suffix names. Z, +00:00, -0400 and -04:00."""
    if text.upper() == "Z":
        return timezone.utc
    body = text[1:].replace(":", "")
    delta = timedelta(hours=int(body[:2]), minutes=int(body[2:]))
    return timezone(-delta if text[0] == "-" else delta)


def parse_stamp(value):
    """One datetime from one cell, or None when the cell is not a stamp.

    Returns an aware datetime when the text carries an offset and a naive one
    when it does not, because that difference is the whole subject here.

    Everything that is not a string is None, epoch milliseconds included. An
    integer column cannot be told from an epoch column without being told, and a
    tool that guesses wrong here invents a shift that was never there. A
    datetime object is refused for the same reason: this reads what a file
    stored, and a value that arrived already parsed came through a path that
    may have converted it once more.

    A stamp at either end of the calendar is refused as well. See
    CONVERTIBLE_MIN.
    """
    if not isinstance(value, str):
        return None
    m = STAMP_RE.match(value.strip())
    if not m:
        return None
    year, month, day, hour, minute, second, frac, offset = m.groups()
    try:
        stamp = datetime(int(year), int(month), int(day), int(hour or 0),
                         int(minute or 0), int(second or 0),
                         int(frac.ljust(6, "0")) if frac else 0)
    except ValueError:
        # Shaped like a date and is not one: 2023-02-30, 2023-13-01.
        return None
    if not CONVERTIBLE_MIN <= stamp <= CONVERTIBLE_MAX:
        # A sentinel at either end of the calendar, which no offset conversion
        # can represent. Counting it as unreadable keeps the rest of the column
        # readable; letting it through ends the run in an OverflowError.
        return None
    if offset:
        stamp = stamp.replace(tzinfo=_offset_of(offset))
    return stamp


def local_of(stamp, zone):
    """The correction, once: read the stamp as UTC, render it in zone.

    This direction is always unambiguous. Going the other way lands on the hour
    that does not exist in March and the hour that happens twice in November,
    and every answer there needs a fold to be chosen for it.
    """
    if zone is None:
        raise ValueError("a timezone is required")
    if stamp.tzinfo is not None:
        raise ValueError("expected a naive datetime, got %r" % (stamp,))
    return stamp.replace(tzinfo=timezone.utc).astimezone(zone)


def time_parts(values):
    """time() -> count over the values. A mechanical column has one or two."""
    parts = {}
    for v in values:
        parts[v.time()] = parts.get(v.time(), 0) + 1
    return parts


def transitions(values, zone):
    """(first local date at the new offset, old offset, new offset) per change.

    Read off the values themselves through zoneinfo, so the dates are the zone's
    own daylight saving transitions rather than a rule written down here. The
    rule moved in 2007 and it will move again.
    """
    out = []
    previous = None
    for v in sorted(values):
        local = local_of(v, zone)
        offset = local.utcoffset()
        if previous is not None and offset != previous:
            out.append((local.date(), previous, offset))
        previous = offset
    return out


def offset_groups(values, zone):
    """utcoffset -> {"count", "time", "name"} across the values."""
    groups = {}
    for v in values:
        local = local_of(v, zone)
        group = groups.setdefault(local.utcoffset(),
                                  {"count": 0, "time": v.time(),
                                   "name": local.tzname()})
        group["count"] += 1
    return groups


def detect(values, zone):
    """The signature a column of parsed values carries. No file, no network.

    The order matters. An all-midnight column is tested before the shift,
    because a date-only column is legitimately all midnight and reading a
    finding out of it is how this tool would cost somebody a day of work over
    data that was never wrong.
    """
    if zone is None:
        raise ValueError("a timezone is required")
    if not isinstance(values, list):
        raise ValueError("values must be a list, got %r" % (type(values),))

    if not values:
        return Finding(EMPTY, "no values to read, so there is nothing to infer")

    aware = [v for v in values if v.tzinfo is not None]
    if aware and len(aware) != len(values):
        return Finding(
            MIXED_AWARENESS,
            "%d value(s) carry an offset and %d do not. Two writers have "
            "touched this column, so no single shift describes it. Split it "
            "and read the halves separately."
            % (len(aware), len(values) - len(aware)))
    if aware:
        return Finding(
            ALL_AWARE,
            "every value carries its own offset, so nothing was left for a "
            "reader to assume. There is no shift to infer.")

    parts = time_parts(values)
    if list(parts) == [MIDNIGHT]:
        return Finding(
            NO_TIME,
            "no time component stored: all %d value(s) are exactly 00:00:00, "
            "which is what a date-only column looks like. No shift is claimed "
            "and no crossing count is given." % len(values))

    if all(local_of(v, zone).time() == MIDNIGHT for v in values):
        groups = offset_groups(values, zone)
        return Finding(
            UTC_SHIFT,
            "every value is local midnight in %s once it is read as UTC, so "
            "the time component is a conversion rather than data"
            % zone.key,
            groups=groups, transitions=transitions(values, zone))

    return Finding(
        NO_FINDING,
        "%d distinct time component(s) and they do not all land on local "
        "midnight, so nothing here proves a mechanical shift" % len(parts))


def crossings(values, zone):
    """(stored, corrected) for every value whose calendar day moves.

    The headline number. At a -04:00 offset it is every record whose real local
    time was 20:00 or later, because those are the ones the conversion pushed
    over midnight and into the next day. That is the 8pm in the story.
    """
    moved = []
    for v in values:
        local = local_of(v, zone)
        if local.date() != v.date():
            moved.append((v, local))
    return moved


def analyse(field, cells, zone):
    """Read one column of raw cells and report on it."""
    values = []
    blank = 0
    unreadable = 0
    for cell in cells:
        if cell is None or (isinstance(cell, str) and not cell.strip()):
            blank += 1
            continue
        stamp = parse_stamp(cell)
        if stamp is None:
            unreadable += 1
        else:
            values.append(stamp)
    finding = detect(values, zone)
    crossed = crossings(values, zone) if finding.signature in COUNTABLE else None
    return Report(field, zone, finding, values, crossed, unreadable, blank)


def fmt_offset(delta):
    """A timedelta as the offset a person reads: -04:00, +00:00, +05:30."""
    total = int(delta.total_seconds())
    sign = "-" if total < 0 else "+"
    total = abs(total)
    return "%s%02d:%02d" % (sign, total // 3600, (total % 3600) // 60)


def verdict(reports):
    """SHIFT when any field proves one, NO SIGNATURE when none does."""
    return "SHIFT" if any(r.finding.proves_shift for r in reports) else \
        "NO SIGNATURE"


def describe(reports, sample=DEFAULT_SAMPLE):
    """Render the reports as the lines the CLI prints.

    A crossing count is stated as damage only when some field in the same run
    carries the signature. On its own a crossing count is just the number of
    records with a late evening timestamp, and printing that as damage is how a
    tool talks somebody into rewriting a column that was fine.
    """
    if sample < 0:
        raise ValueError("--sample cannot be negative")
    proved = any(r.finding.proves_shift for r in reports)
    lines = []
    for r in reports:
        head = "FIELD %s  (%d value(s)" % (r.field, r.count)
        if r.unreadable:
            head += ", %d unreadable" % r.unreadable
        if r.blank:
            head += ", %d blank" % r.blank
        lines.append(head + ")")
        lines.append("  SIGNATURE  %s" % r.finding.signature)
        lines.append("      %s" % r.finding.detail)
        for offset in r.finding.offsets:
            group = r.finding.groups[offset]
            lines.append("      %s at %s (%s), %d value(s)"
                         % (group["time"].isoformat(), fmt_offset(offset),
                            group["name"], group["count"]))
        for date, old, new in r.finding.transitions:
            lines.append("      the offset moves from %s to %s on %s, which is "
                         "a daylight saving transition in %s"
                         % (fmt_offset(old), fmt_offset(new), date.isoformat(),
                            r.zone.key))
        if r.crossed is None:
            lines.append("  CROSSINGS  not counted for this signature")
        else:
            lines.append("  CROSSINGS  %d of %d record(s) %s a different "
                         "calendar day once corrected to %s"
                         % (len(r.crossed), r.count,
                            "land on" if proved else "WOULD land on",
                            r.zone.key))
            if not proved and r.crossed:
                lines.append("      no field in this file carries the "
                             "signature, so that is what a shift WOULD cost, "
                             "not what it did")
            for stored, local in r.crossed[:sample]:
                lines.append("      stored %s  ->  %s %s   day moves %s -> %s"
                             % (stored.isoformat(" "), local.isoformat(" "),
                                local.tzname(), stored.date().isoformat(),
                                local.date().isoformat()))
    lines.append("VERDICT: %s" % verdict(reports))
    return lines


def as_json(reports, sample=DEFAULT_SAMPLE):
    """The same content as describe(), shaped for a script."""
    if sample < 0:
        raise ValueError("--sample cannot be negative")
    out = {"verdict": verdict(reports), "fields": []}
    for r in reports:
        item = {
            "field": r.field,
            "timezone": r.zone.key,
            "signature": r.finding.signature,
            "detail": r.finding.detail,
            "values": r.count,
            "unreadable": r.unreadable,
            "blank": r.blank,
            "offsets": [fmt_offset(o) for o in r.finding.offsets],
            "transitions": [d.isoformat() for d, _o, _n in
                            r.finding.transitions],
            "crossings": None if r.crossed is None else len(r.crossed),
            "sample": [] if r.crossed is None else
                      [{"stored": s.isoformat(" "),
                        "corrected": l.isoformat(" "),
                        "stored_day": s.date().isoformat(),
                        "corrected_day": l.date().isoformat()}
                       for s, l in r.crossed[:sample]],
        }
        out["fields"].append(item)
    return out


# ------------------------------------------------------------------------ io

def load_zone(name):
    """The zone, or a ValueError carrying the remedy.

    On Windows there is no system time zone database, so zoneinfo reads the
    tzdata package instead. ArcGIS Pro's Python has it. A bare python.exe may
    not, and the failure is the same one a misspelt zone gives.
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(
            "unknown timezone %r. Use an IANA name such as America/New_York. "
            "On Windows the zone database comes from the tzdata package: "
            "pip install tzdata." % (name,))


def read_csv_rows(path):
    """(field names, rows) from a CSV.

    utf-8-sig, because a CSV written by Excel opens with a byte order mark and
    reading it as plain utf-8 names the first field OBJECTID with an invisible
    character in front of it, which then matches no --field anybody types.
    """
    with open(path, "r", newline="", encoding="utf-8-sig",
              errors="replace") as fh:
        reader = csv.DictReader(fh)
        fields = list(reader.fieldnames or [])
        rows = [dict(row) for row in reader]
    if not fields:
        raise ValueError("%s has no header row" % path)
    return fields, rows


def read_geojson_rows(path):
    """(field names, rows) from a GeoJSON's feature properties."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        doc = json.load(fh)
    features = doc.get("features") if isinstance(doc, dict) else doc
    if not isinstance(features, list):
        raise ValueError("%s is not a GeoJSON FeatureCollection: no features "
                         "array" % path)
    fields = []
    rows = []
    for feature in features:
        if not isinstance(feature, dict):
            raise ValueError("%s holds a feature that is not an object" % path)
        props = feature.get("properties") or {}
        for key in props:
            if key not in fields:
                fields.append(key)
        rows.append(props)
    if not fields:
        raise ValueError("%s has no feature properties to read" % path)
    return fields, rows


def read_table(path):
    """(field names, rows) from a CSV or a GeoJSON, chosen by what it holds.

    The extension decides, and an unknown extension is sniffed rather than
    refused. A county open data portal serves the same GeoJSON as .json, as
    .geojson and as a download with no extension at all.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        return read_csv_rows(path)
    if ext in (".json", ".geojson"):
        return read_geojson_rows(path)
    with open(path, "rb") as fh:
        head = fh.read(64).lstrip()
    if head.startswith(b"{") or head.startswith(b"["):
        return read_geojson_rows(path)
    return read_csv_rows(path)


def column(rows, field):
    """Every cell of one field, blanks and all, in row order."""
    return [row.get(field) for row in rows]


def date_fields(fields, rows):
    """The fields worth reading: at least one stamp and nothing unreadable.

    Strict on purpose. A NOTES column holding one line that happens to start
    with a date is not a date field, and counting calendar days out of it
    produces a confident number about nothing.
    """
    found = []
    for field in fields:
        parsed = 0
        unreadable = 0
        for cell in column(rows, field):
            if cell is None or (isinstance(cell, str) and not cell.strip()):
                continue
            if parse_stamp(cell) is None:
                unreadable += 1
            else:
                parsed += 1
        if parsed and not (unreadable and STRICT_AUTODETECT):
            found.append(field)
    return found


# ------------------------------------------------------------------ self-test

def self_test():
    """Assertions over the decision core, then over the io layer.

    Nothing here reaches the network, a database or arcpy. The io half writes
    CSV and GeoJSON fixtures into a temporary directory and runs main() over
    them, because a reader that has never read a file has not been tested.

    Every fixture that depends on a daylight saving boundary is built through
    zoneinfo rather than from a date written down here. The 2023 boundaries are
    an output of this test, not an input to it.
    """
    passed = [0]
    failed = []

    def check(cond, label):
        if cond:
            passed[0] += 1
            print("PASS  %s" % label)
        else:
            failed.append(label)
            print("FAIL  %s" % label)

    def raises(fn, label):
        try:
            fn()
        except ValueError:
            check(True, label)
        except Exception as exc:
            check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            check(False, "%s (no error raised)" % label)

    print("tzrot self-test: no network, no database, a temporary directory "
          "for the io layer")
    print("-" * 68)

    # Named outright rather than read from DEFAULT_TIMEZONE, so that the
    # configured default is something these assertions can disagree with.
    ny = ZoneInfo("America/New_York")
    utc = ZoneInfo("UTC")
    india = ZoneInfo("Asia/Kolkata")
    check(DEFAULT_TIMEZONE == "America/New_York",
          "the configured default zone is Eastern, which is what every "
          "assertion below is written against")

    def midnights(zone, start, days):
        """Local midnight on each of `days` days, expressed as naive UTC.

        Exactly what a local-to-UTC conversion leaves behind in a date-only
        column, and the only place the DST boundaries come from.
        """
        out = []
        for i in range(days):
            local = datetime.combine(start, MIDNIGHT) + timedelta(days=i)
            out.append(local.replace(tzinfo=zone).astimezone(timezone.utc)
                       .replace(tzinfo=None))
        return out

    # ---- reading one cell
    check(parse_stamp("2023-07-05 20:15:00") == datetime(2023, 7, 5, 20, 15),
          "a space separated stamp parses")
    check(parse_stamp("2023-07-05T20:15:00") == datetime(2023, 7, 5, 20, 15),
          "a T separated stamp parses")
    check(parse_stamp("2023-07-05 20:15") == datetime(2023, 7, 5, 20, 15),
          "a stamp with no seconds parses")
    check(parse_stamp("2023-07-05") == datetime(2023, 7, 5, 0, 0),
          "a date with no time at all parses to midnight")
    check(parse_stamp("  2023-07-05 20:15:00  ")
          == datetime(2023, 7, 5, 20, 15),
          "surrounding whitespace is ignored")
    check(parse_stamp("2023-07-05 20:15:00.5")
          == datetime(2023, 7, 5, 20, 15, 0, 500000),
          "a one digit fraction is read as tenths, not as microseconds")
    check(parse_stamp("2023-07-05 20:15:00.123456")
          == datetime(2023, 7, 5, 20, 15, 0, 123456),
          "six fractional digits are microseconds")
    check(parse_stamp("2023-07-05 20:15:00.1234567")
          == datetime(2023, 7, 5, 20, 15, 0, 123456),
          "a seventh fractional digit is dropped, not a parse failure")
    check(parse_stamp("2023-07-05 20:15:00").tzinfo is None,
          "a stamp with no offset is NAIVE, which is the whole subject here")
    check(parse_stamp("2023-07-05T20:15:00Z").tzinfo == timezone.utc,
          "a trailing Z is an offset of zero")
    check(parse_stamp("2023-07-05t20:15:00z").tzinfo == timezone.utc,
          "a lower case z is read the same way")
    check(parse_stamp("2023-07-05T20:15:00+00:00").tzinfo == timezone.utc,
          "an explicit +00:00 is UTC too")
    check(parse_stamp("2023-07-05T20:15:00-04:00").utcoffset()
          == timedelta(hours=-4), "a negative offset is read")
    check(parse_stamp("2023-07-05T20:15:00-0400").utcoffset()
          == timedelta(hours=-4), "an offset without a colon is read")
    check(parse_stamp("2023-07-05T20:15:00+05:30").utcoffset()
          == timedelta(hours=5, minutes=30),
          "an offset with minutes in it is read")
    check(parse_stamp("") is None, "an empty cell is not a stamp")
    check(parse_stamp("   ") is None, "a whitespace cell is not a stamp")
    check(parse_stamp(None) is None, "a missing cell is not a stamp")
    check(parse_stamp("N/A") is None, "a placeholder is not a stamp")
    check(parse_stamp("2023-02-30") is None,
          "a date shaped string that is not a date is rejected, not raised")
    check(parse_stamp("2023-13-01 00:00:00") is None,
          "a thirteenth month is rejected")
    check(parse_stamp("2023-07-05 25:00:00") is None,
          "a twenty fifth hour is rejected")
    check(parse_stamp("2023-7-5") is None,
          "an unpadded date is rejected, because guessing widens what counts "
          "as a date field")
    check(parse_stamp("07/05/2023") is None,
          "m/d/Y is refused: it cannot be told from d/m/Y  <-- pinned defect")
    check(parse_stamp("2023-07-05 20:15:00 and then some") is None,
          "a stamp with prose after it is not a stamp")
    check(parse_stamp("permit 2023-07-05") is None,
          "a stamp with prose before it is not a stamp")
    check(parse_stamp(1688529600000) is None,
          "epoch milliseconds are NOT read: an integer column would become a "
          "date column  <-- pinned defect")
    check(parse_stamp(20230705) is None, "a bare integer is not a stamp")
    check(parse_stamp(datetime(2023, 7, 5, 20, 15)) is None,
          "a datetime OBJECT is not a stamp either, although printing it "
          "would give a string this parser accepts  <-- pinned defect")
    check(parse_stamp(datetime(2023, 7, 5).date()) is None,
          "and neither is a date object")
    check(parse_stamp(1688529600.0) is None, "nor a float")
    check(parse_stamp(float("nan")) is None and parse_stamp(float("inf")) is None,
          "nor NaN, nor infinity")
    check(parse_stamp("9999-12-31 23:59:59") is None,
          "a 'never expires' sentinel at the end of the calendar is refused: "
          "no offset conversion of it can be represented  <-- pinned defect")
    check(parse_stamp("0001-01-01 02:00:00") is None,
          "and so is a null sentinel at the start of it  <-- pinned defect")
    check(parse_stamp("9999-12-30 12:00:00") is not None,
          "a day inside the boundary still parses, so the margin is a day and "
          "not a decade")
    check(parse_stamp("9999-12-31") is None,
          "the refusal is by value, not by whether a time was written")
    check(_offset_of("Z") is timezone.utc, "Z maps to the utc singleton")
    check(_offset_of("+00:00") is timezone.utc,
          "and so does an explicit zero offset")

    # ---- the correction, applied once
    check(local_of(datetime(2023, 7, 6, 0, 15), ny)
          == datetime(2023, 7, 5, 20, 15, tzinfo=ny),
          "a stamp read as UTC comes back as the local time it meant")
    check(local_of(datetime(2023, 7, 5, 4, 0), ny).time() == MIDNIGHT,
          "04:00 in July is local midnight")
    check(local_of(datetime(2023, 1, 5, 5, 0), ny).time() == MIDNIGHT,
          "05:00 in January is local midnight")
    check(local_of(datetime(2023, 1, 5, 4, 0), ny).time() != MIDNIGHT,
          "04:00 in JANUARY is not, which is what makes the split evidence")
    check(local_of(datetime(2023, 7, 5, 4, 0), ny).tzname() == "EDT",
          "the July offset names itself EDT")
    check(local_of(datetime(2023, 1, 5, 5, 0), ny).tzname() == "EST",
          "the January offset names itself EST")
    check(local_of(datetime(2023, 7, 5, 4, 0), utc).time() == time(4, 0),
          "under UTC the correction changes nothing, so no signature exists")
    raises(lambda: local_of(parse_stamp("2023-07-05T20:15:00Z"), ny),
           "correcting an already aware value raises rather than shifting it "
           "twice  <-- pinned defect")
    raises(lambda: local_of(datetime(2023, 7, 5), None),
           "correcting with no zone raises")

    # ---- THE PINNED DEFECT: a date-only column is legitimately all midnight
    plain = [datetime(2023, 7, 1) + timedelta(days=i) for i in range(30)]
    f = detect(plain, ny)
    check(f.signature == NO_TIME,
          "an all midnight column reports NO_TIME_COMPONENT, not a shift  "
          "<-- pinned defect")
    check("no time component stored" in f.detail,
          "and says so in those words  <-- pinned defect")
    check(f.proves_shift is False, "so it is not a finding")
    check(f.offsets == [] and f.transitions == [],
          "no offset is inferred from a column that stores no time")
    r = analyse("PLAN_DATE", [d.isoformat(" ") for d in plain], ny)
    check(r.crossed is None,
          "and NO crossing count is given for it, although every one of those "
          "rows would cross  <-- pinned defect")
    check(len(crossings(plain, ny)) == len(plain),
          "which is the trap: counted blindly, a date-only column reports 100% "
          "damage  <-- pinned defect")
    check(detect([datetime(2023, 7, 5)], ny).signature == NO_TIME,
          "one midnight value is a date-only column too")
    check(detect(plain + [datetime(2023, 8, 1, 4, 0)], ny).signature
          != NO_TIME,
          "one value with a time in it is enough to stop being date-only")
    check(parse_stamp("2023-07-05") == parse_stamp("2023-07-05 00:00:00"),
          "a bare date and an explicit midnight are the same value, so both "
          "shapes of a date-only column read alike")

    # ---- the fixed offset signature, one side of the year only
    summer = midnights(ny, datetime(2023, 7, 1).date(), 40)
    f = detect(summer, ny)
    check(f.signature == UTC_SHIFT, "40 summer midnights read as a UTC shift")
    check(list(time_parts(summer)) == [time(4, 0)],
          "every one of them is stored as exactly 04:00:00")
    check(f.offsets == [timedelta(hours=-4)], "the inferred offset is -04:00")
    check(fmt_offset(f.offsets[0]) == "-04:00", "which renders as -04:00")
    check(f.groups[timedelta(hours=-4)]["count"] == 40,
          "and all 40 values sit at it")
    check(f.groups[timedelta(hours=-4)]["name"] == "EDT",
          "named EDT by the zone, not by this file")
    check(f.transitions == [],
          "one side of the year crosses no daylight saving boundary")
    check(f.proves_shift is True, "a UTC shift is a finding")
    winter = midnights(ny, datetime(2023, 1, 5).date(), 40)
    f = detect(winter, ny)
    check(f.signature == UTC_SHIFT, "40 winter midnights read as a UTC shift")
    check(list(time_parts(winter)) == [time(5, 0)],
          "stored as exactly 05:00:00 instead")
    check(f.offsets == [timedelta(hours=-5)], "the inferred offset is -05:00")
    check(f.groups[timedelta(hours=-5)]["name"] == "EST", "named EST")
    check(detect(midnights(india, datetime(2023, 7, 1).date(), 10),
                 india).offsets == [timedelta(hours=5, minutes=30)],
          "a half hour zone is inferred as +05:30, so the rule is not four and "
          "five hours")
    check(fmt_offset(timedelta(hours=5, minutes=30)) == "+05:30",
          "and renders with its minutes")
    check(fmt_offset(timedelta(0)) == "+00:00", "a zero offset renders signed")

    # ---- the split signature, with both boundaries inside the column
    year = midnights(ny, datetime(2023, 1, 1).date(), 365)
    f = detect(year, ny)
    check(f.signature == UTC_SHIFT, "a whole year of midnights is a UTC shift")
    check(sorted(time_parts(year)) == [time(4, 0), time(5, 0)],
          "carrying 04:00 AND 05:00 and nothing else")
    check(f.offsets == [timedelta(hours=-5), timedelta(hours=-4)],
          "both offsets are inferred")
    check(f.groups[timedelta(hours=-4)]["time"] == time(4, 0),
          "04:00 is the stored time at -04:00")
    check(f.groups[timedelta(hours=-5)]["time"] == time(5, 0),
          "05:00 is the stored time at -05:00")
    check(sum(g["count"] for g in f.groups.values()) == 365,
          "every value is accounted for by one offset or the other")
    check(len(f.transitions) == 2,
          "the year holds exactly two daylight saving transitions")

    spring, autumn = f.transitions
    check(spring[0].month == 3, "the first is in March")
    check(autumn[0].month == 11, "the second is in November")
    check(spring[2] - spring[1] == timedelta(hours=1),
          "March moves the offset forward one hour")
    check(autumn[2] - autumn[1] == timedelta(hours=-1),
          "November moves it back one hour")

    # The transition dates, recomputed by a different route: walk the year and
    # ask the zone itself when local midnight changes offset. If transitions()
    # ever reports a date this walk does not, one of the two is wrong.
    walked = []
    previous = None
    for i in range(365):
        day = (datetime(2023, 1, 1) + timedelta(days=i)).replace(tzinfo=ny)
        if previous is not None and day.utcoffset() != previous:
            walked.append(day.date())
        previous = day.utcoffset()
    check([d for d, _o, _n in f.transitions] == walked,
          "the transition dates match the zone's own, walked day by day  "
          "<-- pinned defect")
    check(len(walked) == 2 and walked[0].month == 3 and walked[1].month == 11,
          "and that independent walk really found two of them")

    # A CSV arrives in whatever order somebody exported it, which is usually
    # permit number and never date. transitions() sorts for that reason: read
    # in row order, an unsorted column changes offset on nearly every row.
    shuffled = year[200:] + year[:100] + year[100:200]
    check(shuffled != year, "the shuffled copy really is in a different order")
    check(transitions(shuffled, ny) == transitions(year, ny),
          "a column read out of date order reports the same two transitions  "
          "<-- pinned defect")
    check(detect(shuffled, ny).signature == UTC_SHIFT,
          "and the same signature")
    check(len(transitions(list(reversed(year)), ny)) == 2,
          "a column exported newest first reports two as well, not 363")

    by_date = dict((v.date(), v.time()) for v in year)
    check(by_date[walked[0]] == time(4, 0),
          "the March transition day itself is stored at 04:00")
    check(by_date[walked[0] - timedelta(days=1)] == time(5, 0),
          "the day before it is stored at 05:00")
    check(by_date[walked[1] - timedelta(days=1)] == time(4, 0),
          "the day before the November transition is stored at 04:00")
    check(by_date[walked[1]] == time(5, 0),
          "the November transition day itself is back at 05:00")
    check(all(local_of(v, ny).time() == MIDNIGHT for v in year),
          "and all 365 land on local midnight, which is what proves the split "
          "is mechanical")

    # The same two time components, put on the WRONG side of the year. This is
    # a column that carries 04:00 and 05:00 for a reason of its own, and the
    # split is the only thing that tells it from a shift.
    wrong = [datetime(2023, 1, 10, 4, 0), datetime(2023, 1, 11, 4, 0),
             datetime(2023, 7, 10, 5, 0), datetime(2023, 7, 11, 5, 0)]
    check(sorted(time_parts(wrong)) == [time(4, 0), time(5, 0)],
          "it carries the same two time components as the real signature")
    check(detect(wrong, ny).signature == NO_FINDING,
          "but on the wrong side of each boundary, so no shift is claimed  "
          "<-- pinned defect")
    half = [datetime(2023, 7, 10, 4, 0), datetime(2023, 1, 10, 4, 0)]
    check(detect(half, ny).signature == NO_FINDING,
          "one value out of place is enough to withhold the finding")
    check(detect([datetime(2023, 7, 10, 4, 0)], ny).signature == UTC_SHIFT,
          "while the same column without it is a shift, so that one value is "
          "what changed the answer")

    # ---- real times of day prove nothing
    varied = [datetime(2023, 7, 5, 8, 12), datetime(2023, 7, 5, 9, 47),
              datetime(2023, 7, 5, 14, 3), datetime(2023, 7, 6, 16, 55),
              datetime(2023, 7, 6, 20, 1)]
    f = detect(varied, ny)
    check(f.signature == NO_FINDING, "a column of real times proves nothing")
    check(f.proves_shift is False, "so it is not a finding")
    check("5 distinct time component" in f.detail,
          "and the report says how varied the column is")
    check(f.offsets == [], "no offset is inferred from it")
    check(detect([datetime(2023, 7, 5, 9, 0)] * 6, ny).signature == NO_FINDING,
          "six identical 09:00 values are not a shift either: 09:00 UTC is not "
          "local midnight anywhere in this zone")
    check(detect([datetime(2023, 7, 5, 4, 0), datetime(2023, 7, 5, 4, 0, 1)],
                 ny).signature == NO_FINDING,
          "one second past 04:00 does not land on midnight, so the column is "
          "not mechanical")

    # ---- awareness
    aware = [parse_stamp("2023-07-05T20:15:00-04:00"),
             parse_stamp("2023-07-06T09:00:00-04:00")]
    f = detect(aware, ny)
    check(f.signature == ALL_AWARE, "a fully aware column has nothing to infer")
    check(f.proves_shift is False, "and is not a finding")
    mixed = aware + [datetime(2023, 7, 7, 4, 0)]
    f = detect(mixed, ny)
    check(f.signature == MIXED_AWARENESS,
          "a column mixing aware and naive values refuses to answer")
    check("2 value(s) carry an offset and 1 do not" in f.detail,
          "and counts both halves")
    check(f.proves_shift is False, "a refusal is not a finding")
    r = analyse("D", ["2023-07-05T20:15:00-04:00", "2023-07-07 04:00:00"], ny)
    check(r.crossed is None,
          "no crossing count is offered for a column it refused to read  "
          "<-- pinned defect")
    check(detect([datetime(2023, 7, 5, 4, 0, tzinfo=ny)], ny).signature
          == ALL_AWARE,
          "a value that is aware of the target zone itself is still aware")

    # ---- an empty column and a single row
    f = detect([], ny)
    check(f.signature == EMPTY, "an empty column reports EMPTY")
    check(f.proves_shift is False, "which is not a finding")
    check(analyse("D", [], ny).crossed is None,
          "and gets no crossing count")
    check(analyse("D", ["", "   ", None], ny).finding.signature == EMPTY,
          "a column of blanks is empty too")
    check(analyse("D", ["", "   ", None], ny).blank == 3,
          "and all three blanks are counted")
    f = detect([datetime(2023, 7, 5, 4, 0)], ny)
    check(f.signature == UTC_SHIFT,
          "a single 04:00 row is reported as a shift: the rule has no minimum")
    check(f.groups[timedelta(hours=-4)]["count"] == 1,
          "with a count of one, which is what says how much to trust it")
    check(len(crossings([datetime(2023, 7, 5, 4, 0)], ny)) == 0,
          "and it cost nothing, because 04:00 is still the fifth of July")

    # ---- THE HEADLINE: what the shift cost
    permits = [datetime(2023, 7, 6, 0, 15),    # issued 20:15 on the 5th
               datetime(2023, 7, 6, 3, 59),    # issued 23:59 on the 5th
               datetime(2023, 7, 6, 4, 0),     # issued 00:00 on the 6th
               datetime(2023, 7, 6, 13, 30),   # issued 09:30 on the 6th
               datetime(2023, 7, 6, 23, 59)]   # issued 19:59 on the 6th
    moved = crossings(permits, ny)
    check(len(moved) == 2, "two of those five permits moved a calendar day")
    check(moved[0][0] == datetime(2023, 7, 6, 0, 15),
          "the first is the one stored at 00:15")
    check(moved[0][1].isoformat(" ") == "2023-07-05 20:15:00-04:00",
          "which really happened at 20:15 the day before")
    check(moved[0][0].date().isoformat() == "2023-07-06"
          and moved[0][1].date().isoformat() == "2023-07-05",
          "so the reported day is one LATER than the real one, which is the "
          "builder's disputed deadline")
    check(all(s.date() != l.date() for s, l in moved),
          "every record returned really did move")
    check(crossings([datetime(2023, 7, 6, 4, 0)], ny) == [],
          "a permit at exactly 04:00 stored is local midnight and does not "
          "move, so the boundary is inclusive the right way")
    check(len(crossings([datetime(2023, 7, 6, 3, 59, 59)], ny)) == 1,
          "one second before it does move")
    check(len(crossings(midnights(ny, datetime(2023, 1, 1).date(), 365),
                        ny)) == 0,
          "a whole year of shifted date-only values crosses NOTHING, which is "
          "why nobody noticed for two years  <-- pinned defect")

    # The 8pm in the story is 24:00 minus the offset, and it is derived from
    # the zone rather than written down: every local time at or after it lands
    # on the next calendar day when it is stored as UTC.
    evening = [datetime(2023, 7, 5, h, 0).replace(tzinfo=ny)
               .astimezone(timezone.utc).replace(tzinfo=None)
               for h in range(24)]
    crossed_hours = [local_of(s, ny).hour for s, _l in crossings(evening, ny)]
    check(crossed_hours == [20, 21, 22, 23],
          "at -04:00 exactly the permits issued from 8pm on are reported a day "
          "late, and no others")
    winter_evening = [datetime(2023, 1, 5, h, 0).replace(tzinfo=ny)
                      .astimezone(timezone.utc).replace(tzinfo=None)
                      for h in range(24)]
    check([local_of(s, ny).hour for s, _l in crossings(winter_evening, ny)]
          == [19, 20, 21, 22, 23],
          "and from 7pm on in January, because the offset is an hour bigger")
    check(crossings([datetime(2023, 7, 5, 12, 0)], utc) == [],
          "under UTC nothing crosses at all, so the count is a property of the "
          "zone and not of the data")
    check(len(crossings([datetime(2023, 7, 5, 2, 0)], india)) == 0,
          "a positive offset zone moves the day the other way, so an early "
          "morning value does not cross")
    check(len(crossings([datetime(2023, 7, 5, 20, 0)], india)) == 1,
          "there it is the late EVENING stored value that lands on the next "
          "day")

    # ---- reading a column of cells
    r = analyse("ISSUED_DATE", ["2023-07-06 00:15:00", "", "not a date",
                                "2023-07-06 13:30:00", None], ny)
    check(r.count == 2, "two readable values out of five cells")
    check(r.blank == 2, "two blanks")
    check(r.unreadable == 1, "one unreadable")
    check(len(r.crossed) == 1, "and one of the two crossed a calendar day")
    check(r.field == "ISSUED_DATE", "the report carries the field name")
    check(r.zone is ny, "and the zone it was read against")
    check(repr(r) == "Report(ISSUED_DATE, NO_FINDING, 2 value(s))",
          "a report reprs as its field, its signature and its size")
    check(repr(Finding(EMPTY, "")) == "Finding(EMPTY)",
          "a finding reprs as its signature")
    r = analyse(u"FECHA_EMISI\u00d3N", ["2023-07-06 00:15:00", u"sin\u00a0fecha"],
                ny)
    check(r.field == u"FECHA_EMISI\u00d3N" and r.count == 1
          and r.unreadable == 1,
          "a field name outside ASCII is carried through, and a value outside "
          "it is unreadable rather than fatal")
    check(any(u"FECHA_EMISI\u00d3N" in l for l in describe([r])),
          "and it renders")
    check(as_json([r])["fields"][0]["field"] == u"FECHA_EMISI\u00d3N",
          "and reaches the json")

    # ---- input validation
    raises(lambda: detect([], None), "detecting with no zone raises")
    raises(lambda: detect("2023-07-05", ny),
           "a string where a list of values belongs raises")
    raises(lambda: describe([], sample=-1), "a negative sample raises")
    raises(lambda: as_json([], sample=-1), "a negative sample raises in json too")
    raises(lambda: load_zone("Mars/Olympus"), "an unknown timezone raises")
    raises(lambda: load_zone("/etc/passwd"),
           "a timezone given as an absolute path raises")
    raises(lambda: load_zone(""), "an empty timezone raises")
    try:
        load_zone("Mars/Olympus")
    except ValueError as exc:
        check("pip install tzdata" in str(exc),
              "and the message names the remedy a Windows box needs")
    check(load_zone("America/New_York").key == "America/New_York",
          "a real zone loads")
    check(load_zone("UTC").key == "UTC", "and so does UTC")

    # ---- rendering
    shift = analyse("INSPECTION_DATE", [v.isoformat(" ") for v in year], ny)
    lines = describe([shift])
    check(lines[-1] == "VERDICT: SHIFT", "a shift renders VERDICT: SHIFT last")
    check(verdict([shift]) == "SHIFT", "and the verdict agrees")
    check(any("FIELD INSPECTION_DATE  (365 value(s))" in l for l in lines),
          "the field heading carries the field name and the value count")
    check(any("SIGNATURE  UTC_SHIFT" in l for l in lines),
          "the signature is on its own line")
    edt_days = (walked[1] - walked[0]).days
    check(any("04:00:00 at -04:00 (EDT), %d value(s)" % edt_days in l
              for l in lines),
          "each offset is reported with its stored time and its count, and the "
          "count is the number of days the zone spent on that offset")
    check(any("daylight saving transition in America/New_York" in l
              for l in lines), "and the transitions are named")
    check(any("CROSSINGS  0 of 365 record(s) land on" in l for l in lines),
          "the crossing count is printed even when it is zero")

    plain_report = analyse("PLAN_DATE", [d.isoformat(" ") for d in plain], ny)
    lines = describe([plain_report])
    check(any("no time component stored" in l for l in lines),
          "a date-only column says so  <-- pinned defect")
    check(any("CROSSINGS  not counted" in l for l in lines),
          "and prints no count  <-- pinned defect")
    check(lines[-1] == "VERDICT: NO SIGNATURE",
          "and is not a finding")

    issued = analyse("ISSUED_DATE", [p.isoformat(" ") for p in permits], ny)
    lines = describe([issued])
    check(any("WOULD land on a different calendar day" in l for l in lines),
          "a crossing count with no signature anywhere is conditional  "
          "<-- pinned defect")
    check(any("not what it did" in l for l in lines),
          "and says plainly that nothing proved the shift  <-- pinned defect")
    lines = describe([issued, shift])
    check(any("2 of 5 record(s) land on a different" in l for l in lines),
          "the same count is stated as damage once another field carries the "
          "signature  <-- pinned defect")
    check(not any("WOULD land on" in l for l in lines),
          "and the conditional wording is gone")
    check(len([l for l in lines if l.startswith("      stored ")]) == 2,
          "both crossing records are sampled")
    check(len([l for l in describe([issued, shift], sample=1)
               if l.startswith("      stored ")]) == 1,
          "--sample 1 prints one of them")
    check(len([l for l in describe([issued, shift], sample=0)
               if l.startswith("      stored ")]) == 0,
          "--sample 0 prints none")
    check(any("day moves 2023-07-06 -> 2023-07-05" in l for l in lines),
          "a sampled record shows the day it moved from and to")
    check(describe([])[-1] == "VERDICT: NO SIGNATURE",
          "no fields at all is not a finding")

    mixed_report = analyse("D", ["2023-07-05T20:15:00-04:00",
                                 "2023-07-07 04:00:00"], ny)
    check(any("CROSSINGS  not counted" in l
              for l in describe([mixed_report])),
          "a refused column prints no count either")
    unreadable_report = analyse("D", ["2023-07-06 00:15:00", "N/A", ""], ny)
    check(any("(1 value(s), 1 unreadable, 1 blank)" in l
              for l in describe([unreadable_report])),
          "the heading counts what it could not read and what was blank")

    # ---- the json rendering
    doc = as_json([issued, shift])
    check(doc["verdict"] == "SHIFT", "the json carries the verdict")
    check(len(doc["fields"]) == 2, "and one entry per field")
    check(doc["fields"][0]["crossings"] == 2, "the crossing count is a number")
    check(doc["fields"][1]["offsets"] == ["-05:00", "-04:00"],
          "the offsets are strings a script can read")
    check(len(doc["fields"][1]["transitions"]) == 2,
          "the transition dates are there")
    check(doc["fields"][0]["sample"][0]["stored_day"] == "2023-07-06"
          and doc["fields"][0]["sample"][0]["corrected_day"] == "2023-07-05",
          "and a sample record names both calendar days")
    check(as_json([plain_report])["fields"][0]["crossings"] is None,
          "a date-only column reports a null crossing count, never a zero  "
          "<-- pinned defect")
    check(as_json([plain_report])["fields"][0]["sample"] == [],
          "and an empty sample")
    check(json.loads(json.dumps(doc)) == doc,
          "the whole document survives a round trip through json")
    check(len(as_json([issued, shift], sample=1)["fields"][0]["sample"]) == 1,
          "--sample reaches the json as well")

    # ---- the harness itself. A check() that cannot record a failure would
    # report every defect above as a pass, which is the one failure no other
    # assertion here could see.
    quiet = sys.stdout
    sys.stdout = io.StringIO()
    mark = len(failed)
    try:
        check(False, "probe: a false condition must be recorded as a failure")
        raises(lambda: None, "probe: a call that raises nothing must fail")
        raises(lambda: [][0], "probe: the wrong exception must fail")
    finally:
        sys.stdout = quiet
    probe = failed[mark:]
    del failed[mark:]
    check(len(probe) == 3,
          "check() and raises() really do record a failure  <-- pinned defect")
    check("no error raised" in probe[1] and "wrong exception" in probe[2],
          "and say which way the call under test went wrong")

    # ---- the io layer, against real files in a temporary directory.
    #
    # Everything above this line is pure. Everything below writes a file and
    # reads it back, because a CSV reader that has never opened a CSV, and a
    # GeoJSON reader that has never met a feature without properties, are the
    # parts that break.
    def capture(fn):
        """(what fn returned, everything it printed to either stream)."""
        buf = io.StringIO()
        real_out, real_err = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = buf
        try:
            result = fn()
        finally:
            sys.stdout, sys.stderr = real_out, real_err
        return result, buf.getvalue()

    def write(path, data):
        with open(path, "wb") as fh:
            fh.write(data)

    tmp = tempfile.mkdtemp(prefix="tzrot-selftest-")
    try:
        # One file that holds the whole story: a timestamp column carrying the
        # damage, a date-only column that is the fingerprint, a date-only
        # column that is innocent, and a text column that is not a date at all.
        header = "PERMIT,ISSUED_DATE,INSPECTION_DATE,PLAN_DATE,STATUS\n"
        body = []
        for i in range(20):
            issued_local = datetime(2023, 7, 3) + timedelta(days=i, hours=20,
                                                            minutes=15)
            issued_stored = (issued_local.replace(tzinfo=ny)
                             .astimezone(timezone.utc).replace(tzinfo=None))
            inspect_stored = midnights(
                ny, (datetime(2023, 3, 5) + timedelta(days=i)).date(), 1)[0]
            body.append("P%04d,%s,%s,%s,ISSUED\n"
                        % (i, issued_stored.isoformat(" "),
                           inspect_stored.isoformat(" "),
                           (datetime(2023, 7, 3) + timedelta(days=i))
                           .date().isoformat()))
        permits_csv = os.path.join(tmp, "permits.csv")
        write(permits_csv, (header + "".join(body)).encode("utf-8"))

        fields, rows = read_csv_rows(permits_csv)
        check(fields == ["PERMIT", "ISSUED_DATE", "INSPECTION_DATE",
                         "PLAN_DATE", "STATUS"],
              "a CSV's header row becomes the field list, in file order")
        check(len(rows) == 20, "and every data row is read")
        check(date_fields(fields, rows)
              == ["ISSUED_DATE", "INSPECTION_DATE", "PLAN_DATE"],
              "three of the five fields auto-detect as date fields")
        check("PERMIT" not in date_fields(fields, rows),
              "an id column that is not a date is not read as one")
        check("STATUS" not in date_fields(fields, rows),
              "and neither is a text column")
        check(column(rows, "STATUS") == ["ISSUED"] * 20,
              "a column comes back in row order")
        check(column(rows, "NO_SUCH_FIELD") == [None] * 20,
              "asking for a field that is not there gives blanks, not a "
              "KeyError")

        r = analyse("INSPECTION_DATE", column(rows, "INSPECTION_DATE"), ny)
        check(r.finding.signature == UTC_SHIFT,
              "the inspection column read off disk carries the signature")
        check(len(r.finding.transitions) == 1,
              "and the March boundary really falls inside those 20 days")
        check(r.finding.transitions[0][0].month == 3,
              "in March, found in a file rather than in a literal")
        check(len(r.crossed) == 0, "it cost nothing")
        r = analyse("ISSUED_DATE", column(rows, "ISSUED_DATE"), ny)
        check(r.finding.signature == NO_FINDING,
              "the issued column proves nothing on its own")
        check(len(r.crossed) == 20,
              "and every one of its 20 evening permits is a day late")
        r = analyse("PLAN_DATE", column(rows, "PLAN_DATE"), ny)
        check(r.finding.signature == NO_TIME,
              "the plan column stores no time at all  <-- pinned defect")
        check(r.crossed is None, "so no count is drawn from it")

        # The same content as GeoJSON. The two readers must agree, because the
        # answer must not depend on which export somebody downloaded.
        features = []
        for row in rows:
            features.append({"type": "Feature",
                             "geometry": {"type": "Point",
                                          "coordinates": [-82.1, 29.2]},
                             "properties": dict(row)})
        permits_geojson = os.path.join(tmp, "permits.geojson")
        write(permits_geojson,
              json.dumps({"type": "FeatureCollection",
                          "features": features}).encode("utf-8"))
        gfields, grows = read_geojson_rows(permits_geojson)
        check(gfields == fields,
              "a GeoJSON's properties give the same field list as the CSV")
        check(grows == rows, "and the same rows")
        check(read_table(permits_geojson) == (gfields, grows),
              "read_table dispatches on the .geojson extension")
        check(read_table(permits_csv) == (fields, rows),
              "and on the .csv extension")

        # A download with no extension, which is what a portal serves.
        no_ext_json = os.path.join(tmp, "download")
        shutil.copy(permits_geojson, no_ext_json)
        check(read_table(no_ext_json) == (gfields, grows),
              "a file with no extension that opens with a brace is sniffed as "
              "GeoJSON")
        no_ext_csv = os.path.join(tmp, "download2")
        shutil.copy(permits_csv, no_ext_csv)
        check(read_table(no_ext_csv) == (fields, rows),
              "and one that does not is read as a CSV")
        # The extension is checked before the content is sniffed, and a CSV
        # whose first field name is bracketed is why. A SQL export writes
        # [OBJECTID] and sniffing would send the file to the json parser.
        bracket_csv = os.path.join(tmp, "bracket.csv")
        write(bracket_csv,
              b"[OBJECTID],[ISSUED_DATE]\n1,2023-07-06 00:15:00\n")
        check(read_table(bracket_csv)[0] == ["[OBJECTID]", "[ISSUED_DATE]"],
              "a .csv that opens with a bracket is read by its extension, "
              "not sniffed into the json reader  <-- pinned defect")
        bare_list = os.path.join(tmp, "bare.geojson")
        write(bare_list, json.dumps(features).encode("utf-8"))
        check(read_geojson_rows(bare_list) == (gfields, grows),
              "a bare array of features is read too")

        # A byte order mark, which is what Excel writes.
        bom_csv = os.path.join(tmp, "bom.csv")
        write(bom_csv, b"\xef\xbb\xbf" + (header + body[0]).encode("utf-8"))
        check(read_csv_rows(bom_csv)[0][0] == "PERMIT",
              "a byte order mark does not become part of the first field "
              "name  <-- pinned defect")
        latin = os.path.join(tmp, "latin.csv")
        write(latin, b"PERMIT,ISSUED_DATE\nP\xe9,2023-07-06 00:15:00\n")
        check(len(read_csv_rows(latin)[1]) == 1,
              "a byte that is not UTF-8 is replaced, not fatal")

        # Files that cannot be read as a table at all.
        missing = None
        try:
            read_csv_rows(os.path.join(tmp, "empty.csv"))
        except OSError as exc:
            missing = exc
        check(isinstance(missing, OSError),
              "a CSV that does not exist raises the OSError main turns into "
              "exit 2")
        write(os.path.join(tmp, "empty.csv"), b"")
        raises(lambda: read_csv_rows(os.path.join(tmp, "empty.csv")),
               "a CSV with no header row raises rather than reporting nothing")
        write(os.path.join(tmp, "notjson.geojson"), b"{not json")
        raises(lambda: read_geojson_rows(os.path.join(tmp, "notjson.geojson")),
               "a GeoJSON that is not json raises")
        write(os.path.join(tmp, "plainobj.geojson"), b'{"a": 1}')
        raises(lambda: read_geojson_rows(os.path.join(tmp, "plainobj.geojson")),
               "json with no features array raises")
        write(os.path.join(tmp, "oddfeat.geojson"), b'{"features": [3]}')
        raises(lambda: read_geojson_rows(os.path.join(tmp, "oddfeat.geojson")),
               "a feature that is not an object raises")
        write(os.path.join(tmp, "noprops.geojson"),
              b'{"features": [{"type": "Feature"}]}')
        raises(lambda: read_geojson_rows(os.path.join(tmp, "noprops.geojson")),
               "features with no properties at all raise")
        headers_only = os.path.join(tmp, "headers.csv")
        write(headers_only, header.encode("utf-8"))
        hf, hr = read_csv_rows(headers_only)
        check(hr == [], "a CSV of headers and nothing else reads as no rows")
        check(date_fields(hf, hr) == [],
              "and no field auto-detects out of no rows")

        # Auto-detection is strict, and --field is the way past it.
        messy = os.path.join(tmp, "messy.csv")
        write(messy, b"D,S\n2023-07-06 00:15:00,a\nN/A,b\n,c\n"
                     b"2023-07-07 00:15:00,d\n")
        mf, mr = read_csv_rows(messy)
        check(date_fields(mf, mr) == [],
              "one unreadable value hides a column from auto-detection")
        r = analyse("D", column(mr, "D"), ny)
        check(r.count == 2 and r.unreadable == 1 and r.blank == 1,
              "while --field reads it anyway and says what it could not read")

        # A "never expires" sentinel, read under a zone whose offset is
        # positive. Before the CONVERTIBLE_MAX guard this ended the whole run
        # in an OverflowError out of astimezone(), several frames below
        # anything main() catches.
        sentinel = os.path.join(tmp, "sentinel.csv")
        write(sentinel, b"PERMIT,EXPIRES\nP1,2023-07-06 04:00:00\n"
                        b"P2,9999-12-31 23:59:59\nP3,0001-01-01 00:00:00\n")
        sf, sr = read_csv_rows(sentinel)
        r = analyse("EXPIRES", column(sr, "EXPIRES"), india)
        check(r.count == 1 and r.unreadable == 2,
              "both calendar-end sentinels are counted as unreadable rather "
              "than converted  <-- pinned defect")
        check(date_fields(sf, sr) == [],
              "and they hide the column from auto-detection, like any other "
              "value that could not be read")
        rc, out = capture(lambda: main([sentinel, "--field", "EXPIRES",
                                        "--timezone", "Asia/Kolkata"]))
        check(rc == 0 and "2 unreadable" in out,
              "so the run finishes and says so, instead of ending in an "
              "OverflowError  <-- pinned defect")

        # ---- main(), end to end
        rc, out = capture(lambda: main([permits_csv]))
        check(rc == 1, "a file carrying the signature exits 1")
        check("VERDICT: SHIFT" in out, "and says SHIFT")
        check("SIGNATURE  UTC_SHIFT" in out, "naming the signature it found")
        check("FIELD INSPECTION_DATE" in out and "FIELD ISSUED_DATE" in out
              and "FIELD PLAN_DATE" in out,
              "all three date fields are reported without being named")
        check("FIELD STATUS" not in out, "and the text field is not")
        check("20 of 20 record(s) land on a different calendar day" in out,
              "the headline count is the damage to the issued column")
        check("no time component stored" in out,
              "the date-only column is reported as date-only  <-- pinned defect")
        check("WOULD land on" not in out,
              "and nothing is stated conditionally, because the signature is "
              "here")
        check("stored 2023-07-04 00:15:00  ->  2023-07-03 20:15:00-04:00 EDT"
              in out,
              "a sampled record is printed with both spellings of the moment")
        check(out.count("      stored ") == 5,
              "five records are sampled by default, and five is the number "
              "whatever DEFAULT_SAMPLE is set to")
        check(DEFAULT_SAMPLE == 5, "which is what DEFAULT_SAMPLE holds")

        rc, geo_out = capture(lambda: main([permits_geojson]))
        check(rc == 1, "the GeoJSON of the same content exits 1 as well")
        check(geo_out.replace("permits.geojson", "permits.csv") == out,
              "and prints byte for byte the same report  <-- pinned defect")

        rc, out = capture(lambda: main([permits_csv, "--field",
                                        "ISSUED_DATE"]))
        check(rc == 0,
              "--field ISSUED_DATE alone exits 0: nothing in that column "
              "proves a shift")
        check("WOULD land on a different calendar day" in out,
              "so its count is stated conditionally  <-- pinned defect")
        check("FIELD INSPECTION_DATE" not in out,
              "and no other field is read")
        rc, out = capture(lambda: main([permits_csv, "--field", "STATUS"]))
        check(rc == 0 and "0 value(s)" in out,
              "--field on a text column reads it and finds nothing")
        rc, out = capture(lambda: main([permits_csv, "--field", "NOPE"]))
        check(rc == 64, "--field naming a field that is not there is a usage "
                        "error, not an empty report  <-- pinned defect")
        check("PERMIT" in out and "STATUS" in out,
              "and the error lists the fields the file does have")

        rc, out = capture(lambda: main([permits_csv, "--timezone", "UTC"]))
        check(rc == 0,
              "under --timezone UTC the same file carries no signature at all")
        check("VERDICT: NO SIGNATURE" in out, "and says so")
        check("no time component stored" in out,
              "the date-only column is still date-only, which is a property of "
              "the data rather than of the zone")
        rc, out = capture(lambda: main([permits_csv, "--timezone",
                                        "Mars/Olympus"]))
        check(rc == 2, "an unknown timezone exits 2")
        check("pip install tzdata" in out, "and names the remedy")

        rc, out = capture(lambda: main([permits_csv, "--sample", "1"]))
        check(out.count("      stored ") == 1, "--sample 1 prints one record")
        rc, out = capture(lambda: main([permits_csv, "--sample", "0"]))
        check(out.count("      stored ") == 0, "--sample 0 prints none")
        check("20 of 20 record(s)" in out,
              "while the count itself is still there")
        rc, out = capture(lambda: main([permits_csv, "--sample", "-1"]))
        check(rc == 64, "a negative --sample is a usage error")

        rc, out = capture(lambda: main([permits_csv, "--json"]))
        check(rc == 1, "--json exits the same way")
        doc = json.loads(out)
        check(doc["verdict"] == "SHIFT",
              "and stdout is json and nothing else, so a script can pipe it")
        names = [f["field"] for f in doc["fields"]]
        check(names == ["ISSUED_DATE", "INSPECTION_DATE", "PLAN_DATE"],
              "carrying the three date fields in file order")
        check(doc["fields"][0]["crossings"] == 20, "the damage is a number")
        check(doc["fields"][2]["crossings"] is None,
              "and the date-only column's is null  <-- pinned defect")

        rc, out = capture(lambda: main([os.path.join(tmp, "no-such.csv")]))
        check(rc == 2, "a file that does not exist exits 2")
        check("could not read" in out, "and says which step failed")
        rc, out = capture(lambda: main([os.path.join(tmp, "plainobj.geojson")]))
        check(rc == 2, "json that is not a FeatureCollection exits 2")
        rc, out = capture(lambda: main([headers_only]))
        check(rc == 0 and "no date field" in out,
              "a file with no date field in it says so and exits 0")
        rc, out = capture(lambda: main([]))
        check(rc == 64, "no path at all is a usage error")
        rc, out = capture(lambda: main(["--self-test", "--json"]))
        check(rc == 64,
              "--self-test with another flag is a usage error, so a green run "
              "can never be a green run of something else")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        check(not os.path.isdir(tmp),
              "the self-test leaves no temporary directory behind")

    # ---- argument handling
    a = _parse([])
    check(a.path is None, "no path parses to None rather than failing")
    check(a.field is None, "--field defaults to every date field, not one")
    check(a.json is False, "--json defaults to OFF")
    check(a.self_test is False, "--self-test defaults to OFF")
    check(a.timezone == DEFAULT_TIMEZONE,
          "--timezone defaults to the configured zone")
    check(a.sample == DEFAULT_SAMPLE, "--sample defaults to the configured size")
    check(_parse(["permits.csv"]).path == "permits.csv", "a path is read")
    check(_parse(["p.csv", "--field", "ISSUED"]).field == "ISSUED",
          "--field is read")
    check(_parse(["p.csv", "--timezone", "Asia/Kolkata"]).timezone
          == "Asia/Kolkata", "--timezone is read")
    check(_parse(["p.csv", "--sample", "3"]).sample == 3, "--sample is read")
    check(_parse(["p.csv", "--json"]).json is True, "--json is read")
    check(_parse(["--self-test"]).self_test is True, "--self-test is read")

    # ---- nothing here opens a socket, and the source says so
    with open(os.path.abspath(__file__), "r", encoding="utf-8") as fh:
        source = fh.read()
    check(re.search(r"^\s*import\s+(urllib|socket|http|ftplib|requests)",
                    source, re.M) is None,
          "tzrot imports no network module: there is no upload path to audit")
    check(re.search(r"^\s*(def|class)\s", source, re.M) is not None,
          "and that search really was run over this file's own source")

    print("-" * 68)
    total = passed[0] + len(failed)
    if failed:
        print("%d assertions, %d failed" % (total, len(failed)))
        for f in failed:
            print("  FAILED: %s" % f)
        return 1
    print("%d assertions, 0 failed" % total)
    return 0


# ----------------------------------------------------------------------- cli

def _parse(argv):
    ap = argparse.ArgumentParser(
        prog="tzrot.py",
        description="Find date fields reinterpreted as UTC, and count the "
                    "records whose calendar day moved.",
        epilog="Nothing is written. The crossing count is stated as damage "
               "only when some field in the same file carries the signature.",
    )
    ap.add_argument("path", nargs="?", help="a CSV or a GeoJSON to read")
    ap.add_argument("--field", help="read only this field, whether or not it "
                                    "auto-detects as a date field")
    ap.add_argument("--timezone", default=DEFAULT_TIMEZONE,
                    help="IANA zone the naive values are assumed to mean "
                         "(default %s)" % DEFAULT_TIMEZONE)
    ap.add_argument("--sample", type=int, default=DEFAULT_SAMPLE,
                    help="how many crossing records to print under the count "
                         "(default %d, 0 prints none)" % DEFAULT_SAMPLE)
    ap.add_argument("--json", action="store_true",
                    help="write the report as json on stdout instead of text")
    ap.add_argument("--self-test", dest="self_test", action="store_true",
                    help="run the assertions and exit")
    return ap.parse_args(argv)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    args = _parse(argv)

    if args.self_test:
        # A self-test run that also carried --field or --timezone would report
        # a pass for a configuration nobody asked about.
        if len(argv) != 1:
            print("error: --self-test takes no other flags.", file=sys.stderr)
            return 64
        return self_test()

    if not args.path:
        print("error: give a CSV or a GeoJSON to read. Use --self-test to "
              "verify the tool without one.", file=sys.stderr)
        return 64
    if args.sample < 0:
        print("error: --sample cannot be negative.", file=sys.stderr)
        return 64

    try:
        zone = load_zone(args.timezone)
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2

    try:
        fields, rows = read_table(args.path)
    except (OSError, IOError, ValueError) as exc:
        print("error: could not read %s: %s" % (args.path, exc),
              file=sys.stderr)
        return 2

    if args.field:
        if args.field not in fields:
            print("error: %s has no field %r. It has: %s"
                  % (args.path, args.field, ", ".join(fields)),
                  file=sys.stderr)
            return 64
        chosen = [args.field]
    else:
        chosen = date_fields(fields, rows)

    reports = [analyse(field, column(rows, field), zone) for field in chosen]

    if args.json:
        print(json.dumps(as_json(reports, args.sample), indent=2,
                         sort_keys=True))
        return 1 if verdict(reports) == "SHIFT" else 0

    print("tzrot: %s, %d record(s), timezone %s"
          % (args.path, len(rows), zone.key))
    if not chosen:
        print("no date field in this file, so there is nothing to read. Name "
              "one with --field if you disagree.")
        return 0
    print("")
    for line in describe(reports, args.sample):
        print(line)
    return 1 if verdict(reports) == "SHIFT" else 0


if __name__ == "__main__":
    sys.exit(main())
