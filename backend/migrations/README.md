# 初始结构

`src/models/tables.py` 定义初始关系结构。API 启动时显式创建演示库的表；此版本只包含初始 schema。需要在已有部署升级表结构时，应单独引入版本化迁移。

## 常设镜头台账与撤销

除档案、执行、证据和审计外，`tables.py` 还定义撤销所需的常设台账：

- `shot_ledger`：按镜头号保存当前已发布版次与该版次快照。
- `shot_releases`：每次会审发布、撤销各一条，`run_id`/`evidence_id` 关联来源，`state` 标记 `active`/`revoked`。
- `shot_release_fields`：每次发布真正变动的字段，记录发布前后值与版次；只追加，不删除，撤销与冲突判定都以它为依据。
- `shot_revocations`：撤销结果，含撤销发布、操作者与明细，`evidence_id` 唯一以支持幂等重试。

API 启动用 `schema.create_all` 建表。已有部署升级时，应通过独立的版本化迁移创建上述表；字段履历启用前的旧证据在 `shot_releases` 没有记录，撤销会明确拒绝并提示人工回退。
