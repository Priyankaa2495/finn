"""Local-only store for the investor's network.

Privacy posture (Hard Rule 2):
- Lives in a single JSON file under DATA_DIR/network_private/, which is gitignored and
  kept apart from finn.db so it can be access-controlled, encrypted or wiped on its own.
- Data minimisation: we keep name, employer stints, education, self-declared
  expertise and relationship metadata. Email addresses and profile URLs are never stored;
  any email-like string is scrubbed on import, on add and again on save.
- Nothing in this package imports an HTTP client or the LLM module (enforced by a test).
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .. import config

PRIVATE_DIR = config.DATA_DIR / "network_private"
DEFAULT_PATH = PRIVATE_DIR / "network.json"
DEMO_PATH = config.BACKEND_DIR / "snapshot" / "network_demo" / "network.json"

MAX_CSV_BYTES = 5 * 1024 * 1024
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
EMAIL_PLACEHOLDER = "[email removed]"


def scrub_emails(text: str) -> tuple[str, int]:
    return EMAIL_RE.subn(EMAIL_PLACEHOLDER, text or "")


def _deep_scrub(obj: Any) -> Any:
    if isinstance(obj, str):
        return scrub_emails(obj)[0]
    if isinstance(obj, list):
        return [_deep_scrub(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _deep_scrub(v) for k, v in obj.items()}
    return obj


@dataclass
class Stint:
    org: str                    # raw employer name as given; resolved at query time
    title: str = ""
    start: str | None = None    # 'YYYY' or 'YYYY-MM'
    end: str | None = None      # None = current role

    @property
    def current(self) -> bool:
        return self.end is None


@dataclass
class Education:
    school: str
    start: int | None = None
    end: int | None = None


@dataclass
class Person:
    id: str
    name: str
    stints: list[Stint] = field(default_factory=list)
    education: list[Education] = field(default_factory=list)
    expertise: list[str] = field(default_factory=list)
    is_contact: bool = True          # False = person of interest (e.g. an incoming CFO)
    closeness: int = 3               # 1..5, investor-declared
    last_contacted: str | None = None  # YYYY-MM-DD
    connected_on: str | None = None    # YYYY-MM-DD
    knows: list[str] = field(default_factory=list)  # ids of people this person knows
    source: str = "manual"           # manual | synthetic | linkedin_csv | filing_bio

    @classmethod
    def from_dict(cls, d: dict) -> "Person":
        d = dict(d)
        d["stints"] = [Stint(**s) for s in d.get("stints", [])]
        d["education"] = [Education(**e) for e in d.get("education", [])]
        return cls(**d)

    def to_dict(self) -> dict:
        return asdict(self)


class ImportTooLarge(ValueError):
    pass


@dataclass
class ImportReport:
    added: int = 0
    skipped: int = 0
    emails_dropped: int = 0    # non-empty "Email Address" cells discarded
    emails_scrubbed: int = 0   # email-like strings removed from other fields


class NetworkStore:
    def __init__(self, people: Iterable[Person] = ()):
        self.people: dict[str, Person] = {}
        for p in people:
            self.add(p)

    def add(self, person: Person) -> None:
        self.people[person.id] = Person.from_dict(_deep_scrub(person.to_dict()))

    def contacts(self) -> list[Person]:
        return [p for p in self.people.values() if p.is_contact]

    # ------------------------------------------------------------ persistence
    @classmethod
    def load(cls, path: str | Path | None = None) -> "NetworkStore":
        p = Path(path or DEFAULT_PATH)
        if not p.exists():
            return cls()
        data = json.loads(p.read_text(encoding="utf-8"))
        return cls(Person.from_dict(d) for d in data.get("people", []))

    @classmethod
    def load_demo(cls) -> "NetworkStore":
        return cls.load(DEMO_PATH)

    def save(self, path: str | Path | None = None) -> Path:
        """Atomic write; file is created 0600 and its directory 0700 (owner-only).
        POSIX semantics (the Render container). On Windows chmod only toggles read-only."""
        p = Path(path or DEFAULT_PATH)
        p.parent.mkdir(parents=True, exist_ok=True)
        _chmod(p.parent, 0o700)
        payload = _deep_scrub({"people": [x.to_dict() for x in self.people.values()]})
        tmp = p.with_suffix(p.suffix + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # never world-readable, even briefly
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, indent=1, ensure_ascii=False))
        os.replace(tmp, p)
        _chmod(p, 0o600)
        return p

    # --------------------------------------------------------- LinkedIn import
    def import_linkedin_csv(self, source: bytes | str | Path) -> ImportReport:
        """LinkedIn 'Connections.csv': optional 'Notes:' preamble, optional UTF-8 BOM,
        columns First Name, Last Name, URL, Email Address, Company, Position, Connected On.
        Email and URL columns are discarded unread into storage."""
        if isinstance(source, (str, Path)):
            path = Path(source)
            if path.stat().st_size > MAX_CSV_BYTES:  # checked before reading
                raise ImportTooLarge(f"{path.name} exceeds {MAX_CSV_BYTES} bytes")
            raw = path.read_bytes()
        else:
            raw = source
        if len(raw) > MAX_CSV_BYTES:
            raise ImportTooLarge(f"upload exceeds {MAX_CSV_BYTES} bytes")

        text = raw.decode("utf-8-sig", errors="replace")
        lines = [ln.lstrip("﻿") for ln in text.splitlines()]
        try:
            start = next(i for i, ln in enumerate(lines) if ln.strip().lower().startswith("first name"))
        except StopIteration:
            raise ValueError("not a LinkedIn Connections.csv: header row not found") from None

        report = ImportReport()
        for row in csv.DictReader(io.StringIO("\n".join(lines[start:]))):
            clean: dict[str, str] = {}
            for key, value in row.items():
                if key is None:
                    continue
                k = key.strip().lower()
                if "email" in k:
                    if (value or "").strip():
                        report.emails_dropped += 1
                    continue                     # never stored
                if k == "url":
                    continue                     # minimisation: profile URLs not needed
                value, n = scrub_emails((value or "").strip())
                report.emails_scrubbed += n
                clean[k] = value

            first, last = clean.get("first name", ""), clean.get("last name", "")
            name = f"{first} {last}".strip()
            if not name:
                report.skipped += 1
                continue
            company, position = clean.get("company", ""), clean.get("position", "")
            pid = "li:" + hashlib.sha256(f"{name}|{company}".lower().encode()).hexdigest()[:12]
            if pid in self.people:
                report.skipped += 1
                continue
            self.add(Person(
                id=pid, name=name,
                stints=[Stint(org=company, title=position)] if company else [],
                closeness=2, connected_on=_parse_li_date(clean.get("connected on", "")),
                source="linkedin_csv",
            ))
            report.added += 1
        return report


def _chmod(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:  # e.g. filesystems without POSIX modes; never block a save on this
        pass


def _parse_li_date(s: str) -> str | None:
    for fmt in ("%d %b %Y", "%d-%b-%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None
