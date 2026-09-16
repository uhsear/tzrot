# tzrot

Find date fields reinterpreted as UTC, and count the records whose calendar day moved.

A builder disputed a deadline by one day and turned out to be right. His permit was issued at
20:15 on the 5th of July and the county's record said the 6th. So did the next 220 permits issued
after eight in the evening, going back two years, because something on the write path converted
local time to UTC and nothing on the read path converted it back.

Nothing was ever invalid. Every value is a real datetime, every row looks plausible, and a
validator has nothing to fail on. The only sign is a fingerprint left in a different column: an
inspection date that should be a bare date is stored at exactly 04:00:00 in the summer and
exactly 05:00:00 in the winter.

```
$ python tzrot.py --self-test
tzrot self-test: no network, no database, a temporary directory for the io layer
--------------------------------------------------------------------
PASS  a stamp with no offset is NAIVE, which is the whole subject here
PASS  m/d/Y is refused: it cannot be told from d/m/Y  <-- pinned defect
PASS  epoch milliseconds are NOT read: an integer column would become a date column  <-- pinned defect
PASS  a datetime OBJECT is not a stamp either, although printing it would give a string this parser accepts  <-- pinned defect
PASS  a 'never expires' sentinel at the end of the calendar is refused: no offset conversion of it can be represented  <-- pinned defect
PASS  04:00 in JANUARY is not, which is what makes the split evidence
PASS  an all midnight column reports NO_TIME_COMPONENT, not a shift  <-- pinned defect
PASS  and NO crossing count is given for it, although every one of those rows would cross  <-- pinned defect
PASS  which is the trap: counted blindly, a date-only column reports 100% damage  <-- pinned defect
...
PASS  the year holds exactly two daylight saving transitions
PASS  the transition dates match the zone's own, walked day by day  <-- pinned defect
PASS  a column read out of date order reports the same two transitions  <-- pinned defect
PASS  but on the wrong side of each boundary, so no shift is claimed  <-- pinned defect
...
PASS  two of those five permits moved a calendar day
PASS  so the reported day is one LATER than the real one, which is the builder's disputed deadline
PASS  a whole year of shifted date-only values crosses NOTHING, which is why nobody noticed for two years  <-- pinned defect
PASS  at -04:00 exactly the permits issued from 8pm on are reported a day late, and no others
PASS  and from 7pm on in January, because the offset is an hour bigger
...
PASS  a crossing count with no signature anywhere is conditional  <-- pinned defect
PASS  the same count is stated as damage once another field carries the signature  <-- pinned defect
PASS  a date-only column reports a null crossing count, never a zero  <-- pinned defect
PASS  check() and raises() really do record a failure  <-- pinned defect
...
PASS  a .csv that opens with a bracket is read by its extension, not sniffed into the json reader  <-- pinned defect
PASS  a byte order mark does not become part of the first field name  <-- pinned defect
PASS  both calendar-end sentinels are counted as unreadable rather than converted  <-- pinned defect
PASS  so the run finishes and says so, instead of ending in an OverflowError  <-- pinned defect
PASS  and prints byte for byte the same report  <-- pinned defect
PASS  --field naming a field that is not there is a usage error, not an empty report  <-- pinned defect
PASS  tzrot imports no network module: there is no upload path to audit
--------------------------------------------------------------------
278 assertions, 0 failed
```

## Requirements

Python 3.9 or newer, for `zoneinfo`. Nothing to install, no `arcpy`, no third-party package. It
runs on ArcGIS Pro's Python and on a plain `python3` equally: 278 assertions on Windows, 278 on
Ubuntu, and the same 278 on the Pro interpreter.

On Windows there is no system time zone database, so `zoneinfo` reads the `tzdata` package
instead. ArcGIS Pro's Python has it. A bare `python.exe` may not, and the failure reads the same
as a misspelt zone, so the error message names the remedy:

```
pip install tzdata
```

```
git clone https://github.com/uhsear/tzrot.git
```

## Quick start

```
python tzrot.py --self-test
python tzrot.py permits.csv
```

## Usage

Point it at a CSV or a GeoJSON. Every field whose values all parse as stamps is read, so you do
not have to know which column is the broken one.

```
python tzrot.py permits.csv
python tzrot.py permits.geojson --timezone America/New_York
python tzrot.py permits.csv --field ISSUED_DATE --sample 20
python tzrot.py permits.csv --json > tzrot.json
```

| Flag | Default | What it does |
|---|---|---|
| `PATH` | none | A CSV or a GeoJSON. Required unless `--self-test`. |
| `--field` | every date field | Read only this field, whether or not it auto-detects as one. |
| `--timezone` | `America/New_York` | IANA zone the naive values are assumed to mean. |
| `--sample` | `5` | How many crossing records to print under the count. `0` prints none. |
| `--json` | off | Write the report as JSON on stdout instead of text. |
| `--self-test` | off | Run the assertions and exit. Takes no other flag. |

Exit codes: 0 no shift signature, 1 a shift signature, 2 the file or the timezone could not be
read, 64 usage error.

Nothing is written. There is no `--apply`, because there is nothing to apply: this tool decides
whether a column was shifted and what that cost. `pandas.Series.dt.tz_localize` does the repair
properly once you know the answer.

## What one run says

```
$ python tzrot.py permits.csv
tzrot: permits.csv, 1200 record(s), timezone America/New_York

FIELD ISSUED_DATE  (1200 value(s))
  SIGNATURE  NO_FINDING
      1191 distinct time component(s) and they do not all land on local midnight, so nothing here proves a mechanical shift
  CROSSINGS  221 of 1200 record(s) land on a different calendar day once corrected to America/New_York
      stored 2023-01-04 01:18:06  ->  2023-01-03 20:18:06-05:00 EST   day moves 2023-01-04 -> 2023-01-03
      stored 2023-01-06 04:31:40  ->  2023-01-05 23:31:40-05:00 EST   day moves 2023-01-06 -> 2023-01-05
      stored 2023-01-06 01:26:50  ->  2023-01-05 20:26:50-05:00 EST   day moves 2023-01-06 -> 2023-01-05
      stored 2023-01-07 02:15:43  ->  2023-01-06 21:15:43-05:00 EST   day moves 2023-01-07 -> 2023-01-06
      stored 2023-01-10 01:14:10  ->  2023-01-09 20:14:10-05:00 EST   day moves 2023-01-10 -> 2023-01-09
FIELD INSPECTION_DATE  (1200 value(s))
  SIGNATURE  UTC_SHIFT
      every value is local midnight in America/New_York once it is read as UTC, so the time component is a conversion rather than data
      05:00:00 at -05:00 (EST), 406 value(s)
      04:00:00 at -04:00 (EDT), 794 value(s)
      the offset moves from -05:00 to -04:00 on 2023-03-13, which is a daylight saving transition in America/New_York
      the offset moves from -04:00 to -05:00 on 2023-11-06, which is a daylight saving transition in America/New_York
  CROSSINGS  0 of 1200 record(s) land on a different calendar day once corrected to America/New_York
FIELD PLAN_DATE  (1200 value(s))
  SIGNATURE  NO_TIME_COMPONENT
      no time component stored: all 1200 value(s) are exactly 00:00:00, which is what a date-only column looks like. No shift is claimed and no crossing count is given.
  CROSSINGS  not counted for this signature
VERDICT: SHIFT
```

Three columns, three different answers, and they only mean anything together.

`INSPECTION_DATE` is the proof. An inspection is booked for a day, not for a moment, so that
column should hold bare dates. It holds 04:00:00 through the summer and 05:00:00 through the
winter, and the day it changes over is the day this zone changes over. No human types that.

`ISSUED_DATE` is the damage. Its times of day are real, so it carries no fingerprint of its own,
and on its own the tool refuses to call the 221 crossings anything but hypothetical. The
signature next door is what turns them into 221 permits whose issue date was reported a day late.

`PLAN_DATE` is the one that was fine, and it is the reason this tool exists rather than a
three-line script. Every one of its 1200 values is exactly midnight, so a naive correction moves
every single one of them into the previous day and reports 100% damage on a column nobody ever
touched. A date-only column is legitimately all midnight. The tool says `no time component
stored` and declines to produce a number.

## The signature

The check is one line, and `zoneinfo` does the work:

```python
stamp.replace(tzinfo=timezone.utc).astimezone(zone).time() == time(0, 0)
```

Read the stored value as UTC, render it in the zone, and ask whether it lands on local midnight.
Every value doing that is a column of midnights that was converted once and never converted back.

That formulation is why the daylight saving split needs no calendar written down here. A value of
`2023-07-05 04:00:00` lands on midnight; the same 04:00 in January lands on 23:00 the day before
and the signature is withheld. The self-test builds a whole year of shifted midnights through
`zoneinfo`, finds the two transitions in it, and checks them against an independent day-by-day
walk of the zone. The 2023 dates, 13 March and 6 November, are an output of that test rather than
an input to it. The rule moved in 2007 and it will move again.

Correcting in this direction is also the only unambiguous one. Going the other way lands on the
hour that does not exist in March and the hour that happens twice in November, and every answer
there needs a fold chosen for it.

## The crossing count

A count of how many records land on a different calendar day when the shift is undone. It is the
headline because it is the only output a manager can act on. "The inspection column carries a
four hour offset" is a sentence about a database; "221 permits carry the wrong issue date, here
are five of them" is a sentence about the public.

At a -04:00 offset it is exactly the records whose real local time was 20:00 or later, because
those are the ones the conversion pushed over midnight. That is the 8pm in the story, and it is
7pm from November to March. The self-test derives both from the zone rather than asserting them.

The count is stated as damage only when some field in the same file carries the signature. On its
own it is just the number of records with a late evening timestamp, which is a perfectly ordinary
thing for a permit table to have. Run against one field with no signature anywhere, the same
number is printed as what a shift **would** cost:

```
$ python tzrot.py permits.csv --field ISSUED_DATE
  CROSSINGS  221 of 1200 record(s) WOULD land on a different calendar day once corrected to America/New_York
      no field in this file carries the signature, so that is what a shift WOULD cost, not what it did
```

## Why no validator finds it

Because nothing is out of range. A schema check wants a datetime and has one. A null check wants
a value and has one. A domain check wants the value inside a set and it is. Every row is
plausible on its own, and the column is only wrong relative to what it was supposed to mean,
which is not written down anywhere the validator can read.

`pandas` is not competing with this either. `tz_localize` and `tz_convert` do the conversion
exactly right and are the correct tool for the repair. Neither has an opinion about whether the
values in front of it have already been through that conversion once, and that is the whole
question here. ArcGIS Pro's time zone field property, set when the service was published, is the
setting that was wrong in the first place: it says what the values are meant to be, not what
they are.

## Limits

- ISO 8601 only: `2023-07-05`, `2023-07-05 20:15:00`, `2023-07-05T20:15:00.123Z`,
  `2023-07-05T20:15:00-04:00`. `07/05/2023` is refused, because it cannot be told from
  `05/07/2023` and a tool that guesses the day of the month has no business counting days.
  Export ISO, or name the column with `--field` and read the count of values it could not parse.
- Epoch milliseconds are not read. An integer column cannot be told from an epoch column without
  being told, and reading one wrong invents a shift that was never there. Convert first.
- One value that happens to sit at 04:00 is reported as a shift. The rule has no minimum, and the
  report gives the count at each offset, which is what says how much to trust it. A whole year
  that splits at both boundaries is evidence. Four rows in July are not.
- The signature needs **every** value to land on local midnight. A column that was shifted after
  somebody had already fixed a few hundred rows by hand carries no signature, and reads as
  `NO_FINDING` with a crossing count that is then stated conditionally.
- Auto-detection is strict: one unreadable value hides a column, so that a `NOTES` field holding
  one line that starts with a date is never counted as a date field. `--field` reads it anyway
  and says how many values it could not read.
- A column mixing offset-bearing and naive values is refused rather than guessed at, and gets no
  crossing count. Two writers have touched it and no single shift describes it.
- A stamp at either end of the calendar is refused and counted as unreadable. `9999-12-31` is the
  standard "never expires" sentinel and `0001-01-01` the standard null one, and no offset
  conversion of either can be represented: `astimezone` raises rather than returning a value. They
  are sentinels rather than data, so refusing them leaves the rest of the column readable.
- CSV and GeoJSON only. No geodatabase, no shapefile, no service. Export the table.
- It reads the whole file into memory. A 1.2 million row, 85 MB CSV takes about 25 seconds and
  reports the same numbers as the 1200 row one it was built from. Something an order of magnitude
  bigger than that is not what this is for.
- It writes nothing, opens no socket and imports no network module. The self-test asserts that
  last one against its own source, so a future edit that adds an upload has to delete an
  assertion to do it.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

Other single-file tools in this portfolio that pair with this one:

- [geocodesift](https://github.com/uhsear/geocodesift) - the same lesson for a geocoder: a high score is not accuracy
- [arcpy-nullscan](https://github.com/uhsear/arcpy-nullscan) - the values that are missing rather than shifted
