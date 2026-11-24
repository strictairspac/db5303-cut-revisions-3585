from sqlalchemy import (JSON, Column, ForeignKey, Integer, MetaData, String,
                        Table, Text, UniqueConstraint)

schema = MetaData()
users = Table('accounts', schema,
    Column('id', Integer, primary_key=True),
    Column('username', String(40), nullable=False, unique=True),
    Column('password_hash', String(200), nullable=False),
    Column('role', String(20), nullable=False))
cases = Table('business_cases', schema,
    Column('id', Integer, primary_key=True),
    Column('slug', String(64), nullable=False, unique=True),
    Column('title', String(120), nullable=False),
    Column('payload', JSON, nullable=False),
    Column('author', String(40), nullable=False),
    Column('created_at', String(32), nullable=False))
runs = Table('business_runs', schema,
    Column('id', Integer, primary_key=True),
    Column('case_id', ForeignKey('business_cases.id'), nullable=False, index=True),
    Column('status', String(20), nullable=False, index=True),
    Column('owner', String(40), nullable=False),
    Column('attempt_token', String(36)),
    Column('started_at', String(32)),
    Column('finished_at', String(32)),
    Column('created_at', String(32), nullable=False),
    Column('error', Text))
evidence = Table('business_evidence', schema,
    Column('id', Integer, primary_key=True),
    Column('run_id', ForeignKey('business_runs.id'), nullable=False, unique=True),
    Column('report', JSON, nullable=False),
    Column('digest', String(64), nullable=False),
    Column('reviewer', String(40)),
    Column('review_note', String(500)),
    Column('reviewed_at', String(32)))
audit = Table('audit_events', schema,
    Column('id', Integer, primary_key=True),
    Column('actor', String(40), nullable=False),
    Column('action', String(32), nullable=False),
    Column('reference', String(50), nullable=False),
    Column('created_at', String(32), nullable=False))

# Durable shot ledger plus one release row per review/revocation. Each release
# records only the fields it actually moved, so a later revocation can restore
# exactly those fields and leave every other change in place. History rows are
# never deleted, which lets conflict detection see a field that was changed and
# later changed back.
shot_releases = Table('shot_releases', schema,
    Column('id', Integer, primary_key=True),
    Column('kind', String(16), nullable=False),
    Column('run_id', ForeignKey('business_runs.id'), unique=True),
    Column('case_id', ForeignKey('business_cases.id'), index=True),
    Column('evidence_id', ForeignKey('business_evidence.id'), unique=True),
    Column('state', String(16), nullable=False),
    Column('actor', String(40)),
    Column('created_at', String(32), nullable=False),
    Column('revoked_at', String(32)),
    Column('revoked_by', String(40)),
    Column('revocation_release_id', ForeignKey('shot_releases.id')))
shot_release_fields = Table('shot_release_fields', schema,
    Column('id', Integer, primary_key=True),
    Column('release_id', ForeignKey('shot_releases.id'), nullable=False, index=True),
    Column('shot_id', String(64), nullable=False, index=True),
    Column('from_version', Integer, nullable=False),
    Column('to_version', Integer, nullable=False),
    Column('field', String(40), nullable=False),
    Column('before_value', JSON),
    Column('after_value', JSON),
    Column('created_at', String(32), nullable=False),
    UniqueConstraint('release_id', 'shot_id', 'field', name='uq_release_shot_field'))
shot_ledger = Table('shot_ledger', schema,
    Column('shot_id', String(64), primary_key=True),
    Column('case_id', ForeignKey('business_cases.id'), index=True),
    Column('version', Integer, nullable=False),
    Column('snapshot', JSON, nullable=False),
    Column('release_id', ForeignKey('shot_releases.id'), index=True),
    Column('updated_at', String(32), nullable=False))
shot_revocations = Table('shot_revocations', schema,
    Column('id', Integer, primary_key=True),
    Column('evidence_id', ForeignKey('business_evidence.id'), nullable=False, unique=True),
    Column('release_id', ForeignKey('shot_releases.id'), nullable=False),
    Column('revocation_release_id', ForeignKey('shot_releases.id'), nullable=False),
    Column('actor', String(40), nullable=False),
    Column('note', Text),
    Column('created_at', String(32), nullable=False),
    Column('detail', JSON, nullable=False))
