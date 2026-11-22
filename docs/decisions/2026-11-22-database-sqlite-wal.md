# Database: SQLite in WAL mode

**Status:** Accepted, 2026-11-22.

## Context

The spec (data retention) asks to evaluate a document-style store (MongoDB type), SQLite in WAL mode and PostgreSQL, and recommend one. What the station has to keep:

- About 10 part numbers × about 100 calibrated samples. The raw recordings are files (4 MB per 10 s, kept by the acquisition service); the database holds their descriptions, about 1 000 rows.
- One result row per tested part. At one part a minute over three shifts, about 1 500 a day and 550 000 a year, each with a few kilobytes of selected features and the decision.
- Users, settings, audit log, models, drift events, I/O events, an outbox for a future edge server.

How it is used: **one machine, one station, one application writing.** Readers are the screens, the reports and a read-only network dashboard. The CM5 can lose power at any moment (emergency exit, cabinet switched off). It has 4 GB of RAM, of which the analysis needs a good part.

## Decision

**SQLite 3 in WAL (write-ahead log) mode**, opened with `synchronous = FULL` and foreign keys on. One file: `<data dir>/eol.sqlite3`.

## Alternatives considered

| | SQLite, WAL | PostgreSQL | Document store (MongoDB type) |
|---|---|---|---|
| Runs as | part of the application; nothing to start or monitor | a separate server process, its own user, configuration and updates | a separate server process |
| RAM | a few MB | roughly 100 MB or more with sensible settings | several hundred MB is typical |
| Survives a power cut | yes: a committed transaction is on the disk with `synchronous = FULL`; an interrupted one is rolled back at the next open | yes | yes, with journaling |
| Many writers at once | one writer at a time (others wait) | yes | yes |
| Readers while writing | yes, in WAL mode | yes | yes |
| Schema, constraints | yes: foreign keys, CHECK constraints, unique indexes | yes, the richest | weak by design; validation in application code |
| Backup | copy one file with SQLite's online backup; easy to put on a USB stick | `pg_dump`, more steps | `mongodump` |
| Install on Raspberry Pi OS | part of Python's standard library | `apt install postgresql` | no package in Raspberry Pi OS; needs the vendor's repository |
| Fits the size | easily: SQLite is used for far larger files | yes | yes |

The things a server database is better at (many concurrent writers, several machines sharing one database) are not needed: there is one station and one application. The things that matter here (nothing to administer at the customer, survival of power cuts, schema rules that refuse bad rows, a backup that is one file) are where SQLite is strongest. A document store would give up the constraints the audit and calibration rules depend on.

## Consequences

- Better: no database server to install, configure, secure or update; one file to back up and restore; schema rules (lengths, allowed values, foreign keys, one master login) are enforced by the database itself.
- Worse or riskier: one writer at a time. Writes are short (one result per tested part), so this is not expected to matter; `busy_timeout` is 10 s. If several stations ever share one database, the move is to PostgreSQL: the schema uses plain SQL types and constraints, so it carries over.
- Migrations: numbered SQL files in `app/eol_app/migrations/`, each applied in one transaction; `PRAGMA user_version` records the last one. Never edit an applied migration; add a new one.
- Context (operator, part, shift) is recorded as change events in `context_events`, not on every result row, as the spec asks.

## Links

- Schema and data dictionary: `docs/database/README.md`
- Code: `app/eol_app/db.py`, `app/eol_app/migrations/0001_initial.sql`
- Review: `docs/phase0/PHASE0_HARDWARE_DESIGN_REVIEW.md`, section 13.6
