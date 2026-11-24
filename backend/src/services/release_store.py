"""Durable shot ledger, per-release field history, and atomic revocation.

Every review publishes into a durable, per-shot ledger. For each shot whose
value actually moves it appends one release row plus one history row per field
it changed. History is never deleted, so a later revocation can restore exactly
the fields that release moved, and conflict detection can still see a field that
a later release changed and then changed back.

Publish and revocation both lock the affected shot rows in the same (sorted)
order before writing, so two concurrent batches touching the same shots
serialize instead of interleaving and never leave a half-applied batch.

Revocation restores only the fields the target release changed and keeps every
later change to other fields. If any later release touched the same field it
refuses the whole batch and names the shot and version. The original evidence
row and its signed review are never modified. A retry returns the stored result
without publishing another version.
"""

from dataclasses import dataclass
import json

from sqlalchemy import and_, select, text
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.engine import Engine

from src.constants.workflow import FIELDS
from src.models.tables import (audit, shot_ledger, shot_release_fields,
                               shot_releases, shot_revocations)
from src.utils.time import utc_time

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
    blocked: bool = True
    note: str = "过期提交已拦截"


@dataclass(frozen=True)
class PublishResult:
    release_id: int
    saved: dict[str, Revision]
    stale: StaleWrite | None
    applied: dict[str, int]
    history: list[dict]
    replayed: bool = False


class HistoryMissing(Exception):
    """The evidence has no field history, so its release cannot be revoked."""


class RevocationBlocked(Exception):
    """A later release touched a field this release changed; refuse the batch."""

    def __init__(self, conflicts: list[dict]):
        self.conflicts = conflicts
        super().__init__(conflict_message(conflicts))


def conflict_message(conflicts: list[dict]) -> str:
    parts = sorted({
        f"镜头 {row['镜头号']} 第 {row['版次']} 版（发布 #{row['发布编号']}）的「{row['字段']}」"
        for row in conflicts
    })
    return "后续发布改过同一字段，整批撤销已拦下：" + "；".join(parts)


# --------------------------------------------------------------------------- read


def _lock_shots(conn, shot_ids: list[str]) -> dict[str, dict]:
    if not shot_ids:
        return {}
    rows = conn.execute(select(shot_ledger).where(
        shot_ledger.c.shot_id.in_(shot_ids)).order_by(shot_ledger.c.shot_id)
        .with_for_update()).mappings()
    return {row["shot_id"]: dict(row) for row in rows}


def _snapshots(conn, shot_ids: list[str]) -> dict[str, Revision]:
    if not shot_ids:
        return {}
    rows = conn.execute(select(shot_ledger).where(
        shot_ledger.c.shot_id.in_(shot_ids))).mappings()
    return {row["shot_id"]: Revision(row["version"], dict(row["snapshot"])) for row in rows}


# ------------------------------------------------------------------------ publish


def _seed_baseline(engine: Engine, case_id: int | None, baseline: dict[str, Revision]) -> None:
    """Commit the baseline for shots not yet tracked, before the editor publishes.

    Existing ledger rows are never overwritten: the ledger, not the submitted
    input, is the authority on the currently published version.
    """
    with engine.begin() as conn:
        for shot_id, revision in baseline.items():
            conn.execute(postgres_insert(shot_ledger).values(
                shot_id=shot_id, case_id=case_id, version=revision.version,
                snapshot=dict(revision.values), updated_at=utc_time(),
            ).on_conflict_do_nothing(index_elements=[shot_ledger.c.shot_id]))


def _apply(
    engine: Engine, case_id: int | None, actor: str | None, run_id: int | None,
    baseline: dict[str, Revision], changes: dict[str, Revision],
) -> tuple[int, dict[str, int], list[dict], int, bool]:
    with engine.begin() as conn:
        if run_id is not None:
            existing = conn.execute(select(shot_releases.c.id).where(and_(
                shot_releases.c.kind == "review",
                shot_releases.c.run_id == run_id))).scalar_one_or_none()
            if existing is not None:
                applied, history = _release_fields(conn, existing)
                return existing, applied, history, -1, True
        editor_pid = conn.execute(text("SELECT pg_backend_pid()")).scalar_one()
        # Lock before writing, matching revocation's lock order.
        locked = _lock_shots(conn, sorted(changes))
        release_id = conn.execute(shot_releases.insert().values(
            kind="review", run_id=run_id, case_id=case_id, evidence_id=None,
            state="active", actor=actor, created_at=utc_time()).returning(
            shot_releases.c.id)).scalar_one()
        applied: dict[str, int] = {}
        history: list[dict] = []
        for shot_id in sorted(changes):
            current = locked.get(shot_id)
            if current is None:
                raise RuntimeError(f"镜头 {shot_id} 的基线版次尚未建立，已回滚整批会审写入")
            before = dict(current["snapshot"])
            after = dict(before)
            moved = []
            for key in FIELDS:
                # Only fields this review actually changed relative to its own
                # baseline may overwrite the ledger. Fields it left at baseline
                # keep whatever a later release has since published.
                if changes[shot_id].values[key] == baseline[shot_id].values[key]:
                    continue
                if changes[shot_id].values[key] != before.get(key):
                    after[key] = changes[shot_id].values[key]
                    moved.append((key, before.get(key), changes[shot_id].values[key]))
            if not moved:
                # Nothing differs from the current published value, so this is a
                # no-op (a re-submission of an already-published state). It does
                # not bump the version.
                continue
            version = current["version"] + 1
            updated = conn.execute(shot_ledger.update().where(and_(
                shot_ledger.c.shot_id == shot_id,
                shot_ledger.c.version == current["version"])).values(
                version=version, snapshot=after, release_id=release_id,
                updated_at=utc_time())).rowcount
            if updated != 1:
                raise RuntimeError(f"镜头 {shot_id} 的会审写入未命中当前版次，已回滚整批会审写入")
            for key, old, new in moved:
                conn.execute(shot_release_fields.insert().values(
                    release_id=release_id, shot_id=shot_id,
                    from_version=current["version"], to_version=version,
                    field=key, before_value=old, after_value=new,
                    created_at=utc_time()))
                history.append({
                    "镜头号": shot_id, "字段": FIELDS[key], "字段键": key,
                    "发布前": old, "发布后": new,
                    "发布前版次": current["version"], "发布后版次": version,
                })
            applied[shot_id] = version
        return release_id, applied, history, editor_pid, False


def _release_fields(conn, release_id: int) -> tuple[dict[str, int], list[dict]]:
    rows = conn.execute(select(shot_release_fields).where(
        shot_release_fields.c.release_id == release_id)).mappings()
    applied, history = {}, []
    for row in rows:
        applied[row["shot_id"]] = row["to_version"]
        history.append({
            "镜头号": row["shot_id"], "字段": FIELDS.get(row["field"], row["field"]),
            "字段键": row["field"], "发布前": row["before_value"], "发布后": row["after_value"],
            "发布前版次": row["from_version"], "发布后版次": row["to_version"],
        })
    return applied, history


def _stale_write(submitter, engine: Engine, observed: dict, frames: int,
                 applied: dict[str, int], editor_pid: int) -> StaleWrite:
    """One session keeps its earlier base while the editor commits on another."""
    shot_id = observed["shot_id"]
    baseline = Revision(observed["version"], dict(observed["snapshot"]))
    proposal = {**baseline.values, "duration_frames": frames}
    submitter_pid = submitter.execute(text("SELECT pg_backend_pid()")).scalar_one()
    if shot_id not in applied:
        return StaleWrite(shot_id, baseline, proposal, 0,
                          editor_pid, submitter_pid, blocked=False,
                          note="会审未升版，补单未参与并发")
    updated = submitter.execute(text("""
        UPDATE shot_ledger
        SET snapshot = CAST(:snapshot AS jsonb), version = version + 1
        WHERE shot_id = :shot_id AND version = :expected
    """), {"shot_id": shot_id, "expected": baseline.version,
           "snapshot": json.dumps(proposal)}).rowcount
    if updated != 0:
        raise RuntimeError(f"镜头 {shot_id} 的旧版提交未被版本条件拦截，已回滚该提交")
    return StaleWrite(shot_id, baseline, proposal, updated, editor_pid, submitter_pid)


def publish_review(
    engine: Engine, case_id: int | None, actor: str | None,
    baseline: dict[str, Revision], changes: dict[str, Revision],
    stale_attempt: tuple[str, int] | None, *, run_id: int | None = None,
) -> PublishResult:
    if engine.dialect.name != "postgresql":
        raise ValueError("会审写入需要 PostgreSQL 数据库")
    _seed_baseline(engine, case_id, baseline)

    # The late submitter observes the committed base in its own session and
    # keeps that transaction open while the editor publishes on another
    # connection. Only then does the submitter retry with the old version.
    submitter = None
    observed = None
    if stale_attempt is not None:
        submitter = engine.connect().execution_options(isolation_level="READ COMMITTED")
        submitter.begin()
        row = submitter.execute(select(shot_ledger).where(
            shot_ledger.c.shot_id == stale_attempt[0])).mappings().one_or_none()
        observed = dict(row) if row is not None else None
        if observed is None:
            submitter.rollback()
            submitter.close()
            raise ValueError(f"迟交补单镜头 {stale_attempt[0]} 没有可用的当前版次")

    try:
        release_id, applied, history, editor_pid, replayed = _apply(
            engine, case_id, actor, run_id, baseline, changes)
        stale = None
        if observed is not None and not replayed:
            stale = _stale_write(submitter, engine, observed, stale_attempt[1],
                                 applied, editor_pid)
    finally:
        if submitter is not None:
            submitter.rollback()
            submitter.close()

    with engine.connect() as conn:
        saved = _snapshots(conn, list(baseline))
    return PublishResult(release_id, saved, stale, applied, history, replayed)


# ------------------------------------------------------------------ evidence view


def evidence_history(engine: Engine, evidence_id: int) -> dict:
    """Describe the release recorded by an evidence item and its later field changes.

    A legacy evidence item with no release row still gets a structured answer so
    the evidence page can explain why it cannot be revoked.
    """
    with engine.connect() as conn:
        release = conn.execute(select(shot_releases).where(
            shot_releases.c.evidence_id == evidence_id).order_by(
            shot_releases.c.id.desc())).mappings().first()
        if release is None:
            return {
                "release_id": None, "kind": None, "state": "missing",
                "created_at": None, "fields": [], "conflicts": [], "revocation": None,
                "reason": "这条会审证据没有字段履历，通常是字段履历启用前的旧会审；"
                          "无法确认那次发布改过哪些字段，因此不能自动撤销。原证据与签署照常保留。",
            }
        release = dict(release)
        rows = [dict(row) for row in conn.execute(select(shot_release_fields).where(
            shot_release_fields.c.release_id == release["id"])
            .order_by(shot_release_fields.c.shot_id, shot_release_fields.c.field)).mappings()]
        revocation = conn.execute(select(shot_revocations).where(
            shot_revocations.c.evidence_id == evidence_id)).mappings().first()
        conflicts = _conflicts(conn, release, rows) if rows and release["state"] == "active" else []
    return {
        "release_id": release["id"], "kind": release["kind"], "state": release["state"],
        "created_at": release["created_at"],
        "fields": [{
            "镜头号": row["shot_id"], "字段": FIELDS.get(row["field"], row["field"]),
            "发布前": row["before_value"], "发布后": row["after_value"],
            "发布前版次": row["from_version"], "发布后版次": row["to_version"],
        } for row in rows],
        "conflicts": conflicts,
        "revocation": {
            "summary": revocation["detail"].get("summary"),
            "actor": revocation["actor"], "created_at": revocation["created_at"],
            "release_id": revocation["revocation_release_id"],
        } if revocation else None,
    }


def _conflicts(conn, release: dict, history_rows: list[dict]) -> list[dict]:
    """Later releases that touched a field this release changed (even if reverted)."""
    pairs = {(row["shot_id"], row["field"]) for row in history_rows}
    if not pairs:
        return []
    rows = conn.execute(select(
        shot_release_fields.c.shot_id, shot_release_fields.c.field,
        shot_release_fields.c.to_version, shot_release_fields.c.release_id,
        shot_releases.c.created_at,
    ).select_from(shot_release_fields.join(
        shot_releases, shot_releases.c.id == shot_release_fields.c.release_id)).where(and_(
        shot_release_fields.c.release_id > release["id"],
        shot_release_fields.c.shot_id.in_({shot for shot, _ in pairs}),
    )).order_by(shot_release_fields.c.release_id, shot_release_fields.c.shot_id)
        ).mappings()
    conflicts = []
    for row in rows:
        if (row["shot_id"], row["field"]) not in pairs:
            continue
        conflicts.append({
            "镜头号": row["shot_id"], "字段": FIELDS.get(row["field"], row["field"]),
            "版次": row["to_version"], "发布编号": row["release_id"],
            "发布时间": row["created_at"],
        })
    return conflicts


# ------------------------------------------------------------------------- revoke


def revoke(engine: Engine, evidence_id: int, actor: str, note: str = "") -> dict:
    """Revoke the release recorded against one evidence item.

    Idempotent: a retry returns the stored result instead of publishing again.
    """
    if engine.dialect.name != "postgresql":
        raise ValueError("撤销需要 PostgreSQL 数据库")
    with engine.begin() as conn:
        release = conn.execute(select(shot_releases).where(and_(
            shot_releases.c.evidence_id == evidence_id,
            shot_releases.c.kind == "review")).with_for_update()).mappings().first()
        if release is None:
            raise HistoryMissing(
                "这条会审证据没有字段履历，通常是字段履历启用前的旧会审；"
                "无法确认那次发布改过哪些字段，因此不能自动撤销。请保留原证据，按原始清单人工回退。")
        release = dict(release)
        if release["state"] == "revoked":
            record = conn.execute(select(shot_revocations).where(
                shot_revocations.c.evidence_id == evidence_id)).mappings().first()
            if record is None:
                raise RuntimeError("该发布已标记撤销，但找不到撤销记录")
            detail = dict(record["detail"])
            detail["already_revoked"] = True
            return detail

        history_rows = [dict(row) for row in conn.execute(select(shot_release_fields).where(
            shot_release_fields.c.release_id == release["id"])
            .order_by(shot_release_fields.c.shot_id, shot_release_fields.c.field)).mappings()]
        if not history_rows:
            raise HistoryMissing(
                "这条会审没有记录任何字段变更（字段履历为空），无从撤销。"
                "请保留原证据，并确认这次会审本来是否发布过字段。")

        shots = sorted({row["shot_id"] for row in history_rows})
        # Lock the target shots before checking later releases, so a concurrent
        # publish serializes behind this transaction instead of slipping between
        # the conflict check and the restore.
        locked = _lock_shots(conn, shots)
        missing = [shot for shot in shots if shot not in locked]
        if missing:
            raise HistoryMissing(f"镜头 {', '.join(missing)} 的当前版次缺失，无法撤销这次发布。")

        conflicts = _conflicts(conn, release, history_rows)
        if conflicts:
            raise RevocationBlocked(conflicts)

        now = utc_time()
        revocation_release_id = conn.execute(shot_releases.insert().values(
            kind="revocation", run_id=None, case_id=release["case_id"], evidence_id=None,
            state="active", actor=actor, created_at=now).returning(
            shot_releases.c.id)).scalar_one()

        details = []
        for shot_id in shots:
            current = locked[shot_id]
            before = dict(current["snapshot"])
            after = dict(before)
            shot_rows = [row for row in history_rows if row["shot_id"] == shot_id]
            for row in shot_rows:
                if row["field"] in before:
                    after[row["field"]] = row["before_value"]
            if after == before:
                # Every field this release moved already holds the target value:
                # nothing to restore, but the batch is still accepted.
                details.append({"镜头号": shot_id, "撤销前版次": current["version"],
                                "撤销后版次": current["version"], "恢复字段": [],
                                "说明": "该镜头字段已与撤销目标一致，无需升版"})
                continue
            version = current["version"] + 1
            updated = conn.execute(shot_ledger.update().where(and_(
                shot_ledger.c.shot_id == shot_id,
                shot_ledger.c.version == current["version"])).values(
                version=version, snapshot=after, release_id=revocation_release_id,
                updated_at=now)).rowcount
            if updated != 1:
                raise RuntimeError(f"镜头 {shot_id} 的撤销写入未命中当前版次，已回滚整批撤销")
            restored = []
            for row in shot_rows:
                if row["field"] not in before:
                    continue
                conn.execute(shot_release_fields.insert().values(
                    release_id=revocation_release_id, shot_id=shot_id,
                    from_version=current["version"], to_version=version,
                    field=row["field"], before_value=before[row["field"]],
                    after_value=row["before_value"], created_at=now))
                restored.append({"字段": FIELDS.get(row["field"], row["field"]),
                                 "撤销前": before[row["field"]],
                                 "撤销后": row["before_value"]})
            details.append({"镜头号": shot_id, "撤销前版次": current["version"],
                            "撤销后版次": version, "恢复字段": restored})

        conn.execute(shot_releases.update().where(shot_releases.c.id == release["id"]).values(
            state="revoked", revoked_at=now, revoked_by=actor,
            revocation_release_id=revocation_release_id))
        restored_count = sum(len(item["恢复字段"]) for item in details)
        detail = {
            "evidence_id": evidence_id, "release_id": release["id"],
            "revocation_release_id": revocation_release_id, "actor": actor,
            "note": note, "created_at": now, "already_revoked": False,
            "summary": f"已撤销该批发布并按新版发布，恢复 {len(details)} 条镜头的 {restored_count} 个字段。",
            "shots": details, "conflicts": [],
        }
        conn.execute(shot_revocations.insert().values(
            evidence_id=evidence_id, release_id=release["id"],
            revocation_release_id=revocation_release_id, actor=actor,
            note=note, created_at=now, detail=detail))
        conn.execute(audit.insert().values(
            actor=actor, action="revoke", reference=str(evidence_id), created_at=now))
        return detail
