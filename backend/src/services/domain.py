"""Screen cut review, field-level three-way merge, and conflict handoff."""

from dataclasses import dataclass

from .revision_store import Revision, Snapshot, record_review


MAX_DURATION_FRAMES = 86_400
MAX_BASE_VERSION = 2_147_483_646
DISAGREEMENT = "双方分歧"
FIELDS = {
    "duration_frames": "镜头时长（帧）",
    "grade": "调色方案",
    "edit_note": "剪辑备注",
}

SPEC = {
    "name": "剪岸后期·镜头版次会审",
    "summary": "合并剪辑与调色的镜头修订，保留已发布版次，将字段分歧和过期提交交给会审制片。",
    "customer": "剪岸后期·短片交付组",
    "entity_labels": ["镜头清单", "版次会审", "冲突交接单"],
    "field_labels": {
        "base_shots": "基线镜头清单",
        "left_revisions": "剪辑组修订",
        "right_revisions": "调色组修订",
        "stale_shot_id": "迟交补单的镜头号",
        "stale_frames": "迟交补单的时长（帧）",
        "shot_id": "镜头号",
        "base_version": "基线版次",
        **FIELDS,
    },
    "guidance": "填入基线和双方修订，同一镜头的基线版次应一致。修订字段留空值 null 表示沿用基线，空字符串可清除备注。迟交镜头和时长须同时填写，所选镜头须有可发布的变更。",
    "sample": {
        "base_shots": [
            {"shot_id": "R01-S010", "base_version": 7, "duration_frames": 96, "grade": "原色", "edit_note": "保留车窗反光"},
            {"shot_id": "R01-S020", "base_version": 12, "duration_frames": 72, "grade": "原色", "edit_note": "站台过肩"},
            {"shot_id": "R01-S030", "base_version": 3, "duration_frames": 48, "grade": "原色", "edit_note": "临时占位"},
            {"shot_id": "R01-S040", "base_version": 5, "duration_frames": 120, "grade": "暖灯", "edit_note": "站牌全景"},
        ],
        "left_revisions": [
            {"shot_id": "R01-S010", "base_version": 7, "duration_frames": 104, "grade": None, "edit_note": "保留车窗反光至切点"},
            {"shot_id": "R01-S020", "base_version": 12, "duration_frames": 68, "grade": None, "edit_note": None},
            {"shot_id": "R01-S030", "base_version": 3, "duration_frames": None, "grade": None, "edit_note": ""},
        ],
        "right_revisions": [
            {"shot_id": "R01-S010", "base_version": 7, "duration_frames": None, "grade": "冷雾", "edit_note": "保留车窗反光至切点"},
            {"shot_id": "R01-S020", "base_version": 12, "duration_frames": 76, "grade": "冷雾", "edit_note": None},
        ],
        "stale_shot_id": "R01-S010",
        "stale_frames": 112,
    },
    "expect": {"已发布镜头": 2, "待会审镜头": 1, "分歧字段": 1, "过期提交拦截": 1, "交接事项": 2},
}


@dataclass(frozen=True)
class Review:
    baseline: dict[str, Revision]
    changes: dict[str, Revision]
    blocked: set[str]
    comparisons: list[dict]
    handoff: list[dict]
    stale_attempt: tuple[str, int] | None


def _integer(value, name: str, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{name}须为 1 至 {maximum} 的整数")
    return value


def _string(value, name: str, maximum: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value:
        raise ValueError(f"{name}须为不超过 {maximum} 字的有效文本")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name}含无效字符") from exc
    value = value.strip()
    if not value and not allow_empty:
        raise ValueError(f"{name}不能为空")
    return value


def _fields(row: dict, *, patch: bool) -> Snapshot:
    fields = {}
    for key, label in FIELDS.items():
        value = row.get(key)
        if patch and value is None:
            continue
        if key == "duration_frames":
            fields[key] = _integer(value, label, MAX_DURATION_FRAMES)
        elif key == "grade":
            fields[key] = _string(value, label, 48)
        else:
            fields[key] = _string(value, label, 240, allow_empty=True)
    return fields


def _input_rows(payload: dict, key: str):
    rows = payload.get(key)
    if not isinstance(rows, list) or (key == "base_shots" and not rows):
        raise ValueError(f"{SPEC['field_labels'][key]}须为列表，且基线清单至少包含一条镜头")
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or row.keys() - {"shot_id", "base_version", *FIELDS}:
            raise ValueError(f"{SPEC['field_labels'][key]}含有格式错误或未知字段")
        shot_id = _string(row.get("shot_id"), "镜头号", 64)
        if shot_id in seen:
            raise ValueError(f"{SPEC['field_labels'][key]}包含重复镜头 {shot_id}")
        seen.add(shot_id)
        version = _integer(row.get("base_version"), f"镜头 {shot_id} 的基线版次", MAX_BASE_VERSION)
        yield shot_id, version, row


def _read_revisions(payload: dict, key: str, baseline: dict[str, Revision]) -> dict[str, Snapshot]:
    revisions = {}
    for shot_id, version, row in _input_rows(payload, key):
        if shot_id not in baseline:
            raise ValueError(f"修订镜头 {shot_id} 不在基线清单中")
        base = baseline[shot_id]
        if version != base.version:
            raise ValueError(f"修订镜头 {shot_id} 的基线版次为 {version}，应为 {base.version}")
        revisions[shot_id] = {**base.values, **_fields(row, patch=True)}
    return revisions


def _decide(base, left, right):
    if left == right:
        return left, "沿用基线" if left == base else "双方一致"
    if left == base:
        return right, "采用调色组"
    if right == base:
        return left, "采用剪辑组"
    return base, DISAGREEMENT


def _stale_input(
    payload: dict, baseline: dict[str, Revision], changes: dict[str, Revision],
) -> tuple[str, int] | None:
    shot_id = payload.get("stale_shot_id")
    frames = payload.get("stale_frames")
    if shot_id in (None, "") and frames in (None, ""):
        return None
    shot_id = _string(shot_id, "迟交补单镜头号", 64)
    frames = _integer(frames, "迟交补单时长", MAX_DURATION_FRAMES)
    if shot_id not in changes:
        raise ValueError("迟交补单须指向本次有可发布变更的镜头")
    if frames == baseline[shot_id].values["duration_frames"]:
        raise ValueError("迟交补单时长须与基线不同")
    return shot_id, frames


def _plan(payload: dict) -> Review:
    if not isinstance(payload, dict):
        raise ValueError("会审输入须为对象")
    baseline = {
        shot_id: Revision(version, _fields(row, patch=False))
        for shot_id, version, row in _input_rows(payload, "base_shots")
    }
    left = _read_revisions(payload, "left_revisions", baseline)
    right = _read_revisions(payload, "right_revisions", baseline)
    changes, blocked, comparisons, handoff = {}, set(), [], []
    for shot_id, base in baseline.items():
        left_values = left.get(shot_id, base.values)
        right_values = right.get(shot_id, base.values)
        merged = {}
        for key, label in FIELDS.items():
            original, proposed_left, proposed_right = base.values[key], left_values[key], right_values[key]
            value, decision = _decide(original, proposed_left, proposed_right)
            merged[key] = value
            if decision == "沿用基线":
                continue
            comparisons.append({
                "镜头号": shot_id, "字段": label, "基线": original, "剪辑组": proposed_left,
                "调色组": proposed_right, "字段判定": decision,
            })
            if decision == DISAGREEMENT:
                blocked.add(shot_id)
                handoff.append({
                    "镜头号": shot_id, "事项": "字段分歧", "字段": label,
                    "提案基线版次": base.version, "基线值": original,
                    "当前版次": base.version, "当前值": original,
                    "提交方甲": "剪辑组", "提案甲": proposed_left, "提交方乙": "调色组", "提案乙": proposed_right,
                    "接手人": "会审制片", "后续动作": "核对切点后选定字段值，重新发起镜头会审",
                })
        if shot_id not in blocked and merged != base.values:
            changes[shot_id] = Revision(base.version, merged)
    return Review(baseline, changes, blocked, comparisons, handoff, _stale_input(payload, baseline, changes))


def validate(payload):
    _plan(payload)


def solve(payload, engine):
    review = _plan(payload)
    saved, stale = record_review(engine, review.baseline, review.changes, review.stale_attempt)
    handoff = list(review.handoff)
    evidence = []
    if stale is not None:
        shot_id = stale.shot_id
        winning = saved[shot_id]
        evidence.append({
            "镜头号": shot_id, "提交时携带版次": stale.baseline.version, "胜出版次": winning.version,
            "基线时长（帧）": stale.baseline.values["duration_frames"],
            "补单时长（帧）": stale.proposal["duration_frames"], "胜出时长（帧）": winning.values["duration_frames"],
            "更新行数": stale.updated_rows, "会审数据库进程": stale.editor_pid,
            "补单数据库进程": stale.submitter_pid, "数据库判定": "过期提交已拦截",
        })
        handoff.append({
            "镜头号": shot_id, "事项": "过期提交", "字段": FIELDS["duration_frames"],
            "提案基线版次": stale.baseline.version, "基线值": stale.baseline.values["duration_frames"],
            "当前版次": winning.version,
            "当前值": winning.values["duration_frames"], "提交方甲": "迟交补单人",
            "提案甲": stale.proposal["duration_frames"], "提交方乙": "会审已发布版", "提案乙": winning.values["duration_frames"],
            "接手人": "会审制片", "后续动作": "以当前版次复核补单时长，经相关组确认后重新提交",
        })

    shot_rows = []
    for shot_id, baseline in review.baseline.items():
        current = saved[shot_id]
        if shot_id in review.blocked:
            status = "待会审"
        elif current.version > baseline.version:
            status = "已发布"
        else:
            status = "沿用原版"
        shot_rows.append({
            "镜头号": shot_id, "基线版次": baseline.version, "当前版次": current.version, "发布状态": status,
            **{label: current.values[key] for key, label in FIELDS.items()},
        })
    published = sum(row["发布状态"] == "已发布" for row in shot_rows)
    blocked = len(review.blocked)
    stale_rejected = len(evidence)
    return {
        "summary": f"已发布 {published} 条镜头，{blocked} 条镜头待会审，数据库拦截 {stale_rejected} 次过期提交。",
        "metrics": {
            "已发布镜头": published, "待会审镜头": blocked, "分歧字段": len(review.handoff),
            "沿用原版镜头": len(shot_rows) - published - blocked,
            "过期提交拦截": stale_rejected, "交接事项": len(handoff),
        },
        "sections": [
            {"title": "镜头当前版次", "rows": shot_rows},
            {"title": "三方逐字段比对", "rows": review.comparisons},
            {"title": "PostgreSQL 条件更新凭据", "rows": evidence},
            {"title": "冲突交接单", "rows": handoff},
        ],
    }
