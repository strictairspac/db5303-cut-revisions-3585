from enum import StrEnum


class Role(StrEnum):
    OPERATOR = 'operator'
    REVIEWER = 'reviewer'
    VIEWER = 'viewer'


class Status(StrEnum):
    QUEUED = 'queued'
    RUNNING = 'running'
    COMPLETE = 'complete'
    FAILED = 'failed'


FIELDS = {
    'duration_frames': '镜头时长（帧）',
    'grade': '调色方案',
    'edit_note': '剪辑备注',
}
