#!/usr/bin/env python3
"""Build a flat schedule list from if2026-schedule.ods + cfp-info.csv.

Each element: {id, title, session_type, day, hall, start_time, end_time}

Reads the ODS with odfpy directly (no pandas dependency). Columns in the
sheet are: day, hall, start_time, duration, Session Type, Session Title
(General Track also has a leading "exclude" column).
- day/hall are only written on the first row of a block; blank cells mean
  "same as the row above".
- start_time is only written when it isn't a straight continuation of the
  previous session; when blank, start_time = previous end_time.
- end_time = start_time + duration (minutes).
- Session Title may instead hold a CFP id from cfp-info.csv; if it matches
  one exactly, it's resolved to that talk's title before anything else
  (title-map lookup, display) uses it.
- Session Type is usually blank for CFP talks (looked up from cfp-info.csv
  by title, or by id per above) and explicitly set (e.g. "other") for
  non-CFP items like "Welcome Note", which have no CFP id.
- exclude == "yes" (General Track only) drops the talk from the output but
  its duration still occupies its slot, so later continuation rows are
  unaffected.
- title-map.ods provides {title: replacement} overrides, used both to
  shorten over-long titles for display and as the title fed into the
  cfp-info.csv lookup.
"""
import argparse
import csv
import re
import sys
from collections import Counter
from datetime import datetime, timedelta

from odf.opendocument import load
from odf.table import Table, TableRow, TableCell
from odf.text import P

ODS_PATH = "if2026-schedule.ods"
CFP_CSV_PATH = "cfp-info.csv"
FRAPPE_INSERT_PATH = "frappe-insert.py"
TITLE_MAP_ODS_PATH = "title-map.ods"

TIME_FMT = "%H:%M"

CFP_URL_FMT = "https://fossunited.org/c/indiafoss/2026/cfp/{id}"

DAY_MAP = {
    "day_1": "2026-09-26",
    "day_2": "2026-09-27",
}

HALL_MAP = {
    "hall_1": "Hall 1",
    "hall_2": "Hall 2",
    "hall_3": "Hall 3",
    "room_1": "Room 1",
    "room_2": "Room 2",
    "room_3": "Room 3",
}

# cfp-info.csv's "Response (Custom Answers)" field holds the track the
# proposal was submitted under; map it to the matching if2026-schedule.ods
# sheet name (case-insensitive on the CFP side).
CFP_TRACK_TO_SHEET = {
    "main track": "General Track",
    "android open source project (aosp)": "AOSP",
    "security": "Security",
    "cloud & devops": "Cloud & DevOps",
    "real time operating systems (rtos)": "RTOS",
    "open hardware": "Hardware",
    "documentation & technical writing": "Documentation & Technical Writing",
    "open design": "Design",
    "compilers, programming languages and systems": "Compilers",
}


def normalize_title(title):
    title = title.strip().lower()
    title = title.replace("’", "'").replace("‘", "'")
    title = title.replace("“", '"').replace("”", '"')
    title = title.replace("–", "-").replace("—", "-")
    title = re.sub(r"\s+", " ", title)
    return title


def load_cfp_info(path):
    """Return ({normalized_title: (id, session_type, title)}, {id: (session_type, title, status, track)})."""
    by_norm_title = {}
    by_id = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            entry = (row["ID"], row["Session Type"], row["Session Title"])
            by_norm_title[normalize_title(row["Session Title"])] = entry
            by_id[row["ID"]] = (
                row["Session Type"],
                row["Session Title"],
                row.get("Status", ""),
                row.get("Response (Custom Answers)", ""),
            )
    return by_norm_title, by_id


def cell_text(cell):
    paragraphs = cell.getElementsByType(P)
    lines = []
    for p in paragraphs:
        parts = []
        for node in p.childNodes:
            if node.nodeType == 3:  # text node
                parts.append(node.data)
            else:  # e.g. <text:span>
                parts.append("".join(n.data for n in node.childNodes if n.nodeType == 3))
        lines.append("".join(parts))
    return "\n".join(lines)


def read_sheets(path):
    """Yield (sheet_name, rows) for every sheet in the ODS file.

    Each row is a list of cell strings (trailing blanks trimmed).
    """
    doc = load(path)
    for table in doc.spreadsheet.getElementsByType(Table):
        rows = []
        for row in table.getElementsByType(TableRow):
            values = []
            for cell in row.getElementsByType(TableCell):
                repeat = int(cell.getAttribute("numbercolumnsrepeated") or 1)
                values.extend([cell_text(cell)] * repeat)
            while values and values[-1] == "":
                values.pop()
            rows.append(values)
        yield table.getAttribute("name"), rows


def load_title_map(path, cfp_by_id):
    """Return {normalized_title: replacement} from title-map.ods.

    Used both to shorten over-long titles for display and as the title
    override fed into the cfp-info.csv lookup (replaces the old hardcoded
    TITLE_OVERRIDES dict).

    Either column may instead hold a CFP id from cfp-info.csv, which is
    resolved to that talk's title before use.
    """
    title_map = {}
    for sheet_name, rows in read_sheets(path):
        header, data_rows = rows[0], rows[1:]
        assert header[:2] == ["title", "replacement"], (sheet_name, header)
        for row in data_rows:
            if not row or not row[0]:
                continue
            title, replacement = (row + [""] * 2)[:2]
            id_match = cfp_by_id.get(title)
            if id_match:
                title = id_match[1]
            id_match = cfp_by_id.get(replacement)
            if id_match:
                replacement = id_match[1]
            title_map[normalize_title(title)] = replacement
    return title_map


def build_sheet_schedule(sheet_name, header, data_rows, cfp_by_title, cfp_by_id, title_map, warnings):
    col = {name: i for i, name in enumerate(header)}
    has_exclude = "exclude" in col

    schedule = []
    day = hall = None
    prev_end = None

    for row_num, row in enumerate(data_rows, start=2):
        if not row:
            # blank separator row between blocks
            prev_end = None
            continue

        row = (row + [""] * len(header))[:len(header)]
        r_day = row[col["day"]]
        r_hall = row[col["hall"]]
        r_start = row[col["start_time"]]
        r_duration = row[col["duration"]]
        r_type = row[col["Session Type"]]
        r_title = row[col["Session Title"]]
        r_exclude = row[col["exclude"]] if has_exclude else ""

        if r_day:
            day = r_day
        if r_hall:
            hall = r_hall
        if not r_title:
            continue

        if r_start:
            start_time = datetime.strptime(r_start, TIME_FMT)
        elif prev_end is not None:
            start_time = prev_end
        else:
            warnings.append(
                f"[{sheet_name}] row {row_num}: no start_time and no prior session "
                f"to continue from ({r_title!r})"
            )
            continue

        if not r_duration:
            warnings.append(f"[{sheet_name}] row {row_num}: missing duration for {r_title!r}, skipping")
            continue
        duration = int(r_duration)
        end_time = start_time + timedelta(minutes=duration)

        if r_exclude.strip().lower() == "yes":
            # excluded talk still occupies its slot, just isn't included in the output
            prev_end = end_time
            continue

        id_match = cfp_by_id.get(r_title)
        title = id_match[1] if id_match else r_title
        display_title = title_map.get(normalize_title(title), title)

        if r_type:
            # explicitly typed, non-CFP item (Welcome Note, GB Elections, ...)
            talk_id = ""
            session_type = "Other" if r_type.lower() == "other" else r_type
        elif id_match:
            talk_id, session_type = r_title, id_match[0]
        else:
            match = cfp_by_title.get(normalize_title(display_title))
            if match is None:
                warnings.append(f"[{sheet_name}] row {row_num}: no cfp-info.csv match for title {r_title!r}")
                talk_id, session_type = "", None
            else:
                talk_id, session_type, _ = match

        schedule.append({
            "linked_cfp": talk_id,
            "title": display_title,
            "category": session_type,
            "other_category": "Other" if session_type == "Other" else "",
            "scheduled_date": DAY_MAP.get(day, day),
            "hall": HALL_MAP.get(hall, hall),
            "start_time": start_time.strftime(TIME_FMT),
            "end_time": end_time.strftime(TIME_FMT),
        })

        prev_end = end_time

    return schedule


def build_schedule(ods_path=ODS_PATH, cfp_csv_path=CFP_CSV_PATH, title_map_path=TITLE_MAP_ODS_PATH):
    """Return {sheet_name: [session, ...]} for every sheet in the ODS file."""
    cfp_by_title, cfp_by_id = load_cfp_info(cfp_csv_path)
    title_map = load_title_map(title_map_path, cfp_by_id)
    warnings = []

    required_columns = {"day", "hall", "start_time", "duration", "Session Type", "Session Title"}

    schedule_by_sheet = {}
    for sheet_name, rows in read_sheets(ods_path):
        header, data_rows = rows[0], rows[1:]
        assert required_columns.issubset(header), (sheet_name, header)
        schedule_by_sheet[sheet_name] = build_sheet_schedule(
            sheet_name, header, data_rows, cfp_by_title, cfp_by_id, title_map, warnings
        )

    scheduled_id_counts = Counter(
        s["linked_cfp"]
        for sessions in schedule_by_sheet.values()
        for s in sessions
        if s["linked_cfp"]
    )
    scheduled_ids = set(scheduled_id_counts)

    for talk_id, count in scheduled_id_counts.items():
        if count > 1:
            _, title, status, _ = cfp_by_id[talk_id]
            if status in ("Approved", "Screening"):
                warnings.append(
                    f"{status} session listed {count} times on schedule: {title!r} ({talk_id})"
                )

    for sheet_name, sessions in schedule_by_sheet.items():
        for s in sessions:
            if not s["linked_cfp"]:
                continue
            _, title, _, track = cfp_by_id[s["linked_cfp"]]
            expected_sheet = CFP_TRACK_TO_SHEET.get(track.strip().lower())
            if expected_sheet and expected_sheet != sheet_name:
                warnings.append(
                    f"[{sheet_name}] {title!r} ({s['linked_cfp']}) was submitted for track "
                    f"{track!r} (expected on {expected_sheet!r})"
                )

    screening_by_track = Counter()
    for talk_id, (_, _, status, track) in cfp_by_id.items():
        if status == "Screening" and talk_id not in scheduled_ids:
            screening_by_track[CFP_TRACK_TO_SHEET.get(track.strip().lower(), track or "(unknown)")] += 1

    for track_name in sorted(screening_by_track):
        warnings.append(f"[{track_name}] {screening_by_track[track_name]} session(s) in Screening status")

    missing_by_type = Counter()
    for talk_id, (session_type, title, status, track) in cfp_by_id.items():
        if status == "Approved" and talk_id not in scheduled_ids:
            expected_sheet = CFP_TRACK_TO_SHEET.get(track.strip().lower(), track)
            warnings.append(
                f"Approved session not on schedule: {title!r} ({talk_id}), track: {expected_sheet!r}"
            )
            missing_by_type[session_type] += 1

    if missing_by_type:
        warnings.append(
            "Approved sessions not on schedule, by Session Type: "
            + ", ".join(f"{t}: {n}" for t, n in sorted(missing_by_type.items()))
            + f", total: {sum(missing_by_type.values())}"
        )

    return schedule_by_sheet, warnings


def markdown_for_track(sessions):
    """Render a track's sessions as a "Time | Session" markdown table."""
    lines = ["| Time | Session |", "| --- | --- |"]
    for s in sessions:
        time = datetime.strptime(s["start_time"], TIME_FMT).strftime("%I:%M %p").lstrip("0")
        if s["linked_cfp"]:
            session = f"[{s['title']}]({CFP_URL_FMT.format(id=s['linked_cfp'])})"
        else:
            session = s["title"]
        lines.append(f"| {time} | {session} |")
    return "\n".join(lines)


def write_schedule_script(path, schedule_by_sheet):
    """Write `tracks = {...}` followed by frappe-insert.py's content to `path`."""
    from pprint import pformat

    with open(FRAPPE_INSERT_PATH, encoding="utf-8") as f:
        frappe_insert = f.read()

    with open(path, "w", encoding="utf-8") as f:
        f.write(f"tracks = {pformat(schedule_by_sheet)}\n\n")
        f.write(frappe_insert)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--markdown", metavar="TRACK",
        help="dump a Time/Session markdown table for the given track (sheet name)",
    )
    parser.add_argument(
        "--schedule-script", metavar="FILENAME.py",
        help="write the `tracks` dict plus frappe-insert.py's content to FILENAME.py",
    )
    args = parser.parse_args()

    if args.schedule_script and not args.schedule_script.endswith(".py"):
        parser.error("--schedule-script must end with .py")

    schedule_by_sheet, warnings = build_schedule()

    for w in warnings:
        print(f"WARNING: {w}", file=sys.stderr)

    if args.markdown:
        if args.markdown not in schedule_by_sheet:
            sys.exit(f"unknown track {args.markdown!r}; available: {', '.join(schedule_by_sheet)}")
        print(markdown_for_track(schedule_by_sheet[args.markdown]))

    if args.schedule_script:
        write_schedule_script(args.schedule_script, schedule_by_sheet)

        total = sum(len(sessions) for sessions in schedule_by_sheet.values())
        print(f"\n{total} sessions across {len(schedule_by_sheet)} sheets", file=sys.stderr)

        non_other = sum(
            1 for sessions in schedule_by_sheet.values() for s in sessions if s["category"] != "Other"
        )
        print(f"{non_other} sessions are not \"Other\"", file=sys.stderr)

    if not args.markdown and not args.schedule_script:
        parser.error("nothing to do: pass --markdown TRACK or --schedule-script FILENAME")


if __name__ == "__main__":
    main()
