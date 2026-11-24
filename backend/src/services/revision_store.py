"""Persist one isolated review in PostgreSQL and capture its write evidence."""

from contextlib import contextmanager
from dataclasses import dataclass
import json
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine


Snapshot = dict[str, int | str]


@dataclass(frozen=True)
class Revision:
    version: int
    values: Snapshot


@dataclass(frozen=True)
class StaleWrite:
    shot_id: str
    baseline: Revision
    proposal: Snapshot
    updated_rows: int
    editor_pid: int
    submitter_pid: int


@contextmanager
def _seeded_table(engine: Engine, baseline: dict[str, Revision]):
    table = f"public.gb5303_{uuid4().hex}"
    with engine.begin() as conn:
        conn.execute(text(f"""
            CREATE TABLE {table} (
                shot_id text PRIMARY KEY,
                version integer NOT NULL,
                snapshot jsonb NOT NULL
            )
        """))
        conn.execute(text(f"""
            INSERT INTO {table} (shot_id, version, snapshot)
            VALUES (:shot_id, :version, CAST(:snapshot AS jsonb))
        """), [
            {"shot_id": shot_id, "version": revision.version,
             "snapshot": json.dumps(revision.values)}
            for shot_id, revision in baseline.items()
        ])
    try:
        yield table
    finally:
        with engine.begin() as conn:
            conn.execute(text(f"DROP TABLE {table}"))


def _conditional_update(
    conn: Connection, table: str, shot_id: str, expected: int,
    snapshot: Snapshot,
) -> int:
    return conn.execute(text(f"""
        UPDATE {table}
        SET snapshot = CAST(:snapshot AS jsonb), version = version + 1
        WHERE shot_id = :shot_id AND version = :expected
    """), {"shot_id": shot_id, "expected": expected, "snapshot": json.dumps(snapshot)}).rowcount


def _publish(engine: Engine, table: str, changes: dict[str, Revision]) -> int:
    with engine.begin() as editor:
        pid = editor.execute(text("SELECT pg_backend_pid()")).scalar_one()
        for shot_id, change in changes.items():
            updated = _conditional_update(editor, table, shot_id, change.version, change.values)
            if updated != 1:
                raise RuntimeError(f"镜头 {shot_id} 的会审写入未命中基线版本，已回滚整批会审写入")
        return pid


def _attempt_stale_write(
    engine: Engine, table: str, changes: dict[str, Revision], shot_id: str, frames: int,
) -> StaleWrite:
    with engine.connect().execution_options(isolation_level="READ COMMITTED") as submitter:
        with submitter.begin():
            observed = submitter.execute(text(f"""
                SELECT version, snapshot, pg_backend_pid() AS pid
                FROM {table} WHERE shot_id = :shot_id
            """), {"shot_id": shot_id}).mappings().one()
            baseline = Revision(observed["version"], observed["snapshot"])
            proposal = {**baseline.values, "duration_frames": frames}

            # This session keeps the earlier base while the editor commits on another connection.
            editor_pid = _publish(engine, table, changes)
            updated = _conditional_update(submitter, table, shot_id, baseline.version, proposal)
            if updated != 0:
                raise RuntimeError(f"镜头 {shot_id} 的旧版提交未被版本条件拦截，已回滚该提交")
            return StaleWrite(shot_id, baseline, proposal, updated, editor_pid, observed["pid"])


def record_review(
    engine: Engine,
    baseline: dict[str, Revision],
    changes: dict[str, Revision],
    stale_attempt: tuple[str, int] | None,
) -> tuple[dict[str, Revision], StaleWrite | None]:
    if engine.dialect.name != "postgresql":
        raise ValueError("会审写入需要 PostgreSQL 数据库")
    with _seeded_table(engine, baseline) as table:
        if stale_attempt is None:
            _publish(engine, table, changes)
            evidence = None
        else:
            evidence = _attempt_stale_write(engine, table, changes, *stale_attempt)

        with engine.begin() as reader:
            rows = reader.execute(text(f"SELECT shot_id, version, snapshot FROM {table}")).mappings()
            saved = {row["shot_id"]: Revision(row["version"], row["snapshot"]) for row in rows}
    return saved, evidence
