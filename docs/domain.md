# 领域约定

记录青年田野采集、地点沿革、受访授权和档案版本的交换契约。

聚合对象包括`field_assignment`、`place_record`、`source_asset`、`archive_snapshot`。事件类型包括`ASSIGNMENT_CLAIMED`、`ASSET_UPLOADED`、`CONSENT_UPDATED`、`REVIEW_COMPLETED`、`SNAPSHOT_PUBLISHED`。所有发生时间都必须携带时区，版本号从 1 开始递增，基础校验不会改写调用方输入。

## 事件载荷

- `ASSET_UPLOADED`：载荷还需包含 `content_hash`, `captured_at`。
- `CONSENT_UPDATED`：载荷还需包含 `subject_ref`, `scope`。
- `SNAPSHOT_PUBLISHED`：载荷还需包含 `cutoff_at`, `visibility`。

相同事件标识的业务幂等、冲突隔离和状态推进由上层服务负责；本仓库只定义可稳定交换的基础事实。
