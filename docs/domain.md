# 领域约定

记录青年田野采集、地点沿革、受访授权和档案版本的交换契约与治理语义。

所有发生时间都必须携带时区，版本号从 1 开始按聚合递增，基础校验不会改写调用方输入。

## 分层聚合

| 聚合 | 含义 |
| --- | --- |
| `field_assignment` | 采集任务 |
| `training_qualification` | 培训资质 |
| `place_record` | 地点沿革 |
| `consent_record` | 受访者同意 |
| `source_asset` | 原始文件摘要 |
| `transcription` | 转写版本 |
| `spatial_judgment` | 空间判断 |
| `expert_review` | 专家复核 |
| `public_release` | 公开用途 |
| `archive_snapshot` | 数字档案快照 |

## 事件载荷

- `QUALIFICATION_RECORDED`：`student_ref`, `skill`, `valid_from`, `valid_until`。
- `ASSIGNMENT_REGISTERED`：`required_skill`。
- `ASSIGNMENT_CLAIMED` / `ASSIGNMENT_RELEASED`：`student_ref`。
- `ASSET_UPLOADED`：`business_key`, `content_hash`, `captured_at`, `uploader_ref`, `assignment_ref`, `authorized_scope`, `media_type`；可选 `subject_refs`, `place_ref`。
- `ASSET_UPLOAD_CONFLICTED`：`business_key`, `content_hash`, `existing_hash`, `reason`；冲突隔离记录，不改写已有资料。
- `TRANSCRIPTION_COMMITTED`：`asset_ref`, `transcription_version`, `text_hash`, `author_ref`。
- `PLACE_REGISTERED` / `PLACE_NAME_RECORDED`：`name`, `valid_from`，可选 `valid_to`。
- `PLACE_MERGED`：`surviving_place`, `merged_place`。
- `NARRATIVE_RECORDED`：`place_ref`, `narrator_ref`, `cohort`, `summary_hash`。
- `SPATIAL_JUDGMENT_RECORDED`：`place_ref`, `boundary_version`, `judgment`, `author_ref`。
- `CONSENT_UPDATED` / `CONSENT_EXPIRED`：`subject_ref`, `scope`；可选 `valid_until`, `is_minor`。
- `GUARDIAN_AUTHORIZATION_RECORDED`：`subject_ref`, `guardian_ref`, `scope`。
- `REVIEW_COMPLETED`：`target_ref`, `outcome`, `reviewer_ref`, `review_kind`（`expert` 或 `independent`）。
- `PUBLIC_VERSION_FROZEN`：`snapshot_ref`, `subject_ref`, `revoked_scopes`, `reason`。
- `SNAPSHOT_PUBLISHED`：`cutoff_at`, `visibility`, `asset_refs`；可选 `purpose`, `named_subjects`。

## 授权范围与媒体依赖

授权范围取值：`internal_research`（内部研究）、`name_public`（公开姓名）、
`voice_public`（公开声音）、`image_public`（公开画面）。媒体类型对公开授权的
依赖：`audio → voice_public`，`photo → image_public`，`video → voice_public + image_public`，
`text` 无额外依赖。

## 治理语义（上层服务负责）

- **幂等上传**：上传可乱序重试。相同 `event_id` 且内容一致的重试直接忽略；相同
  `business_key` 只有 `content_hash` 与 `authorized_scope` 都一致才视为幂等，否则
  追加 `ASSET_UPLOAD_CONFLICTED` 隔离冲突，已有资料保持不变。
- **提交责任**：同一采集项同一时刻只有一名学生持有提交责任；认领须持有效培训资质，
  并发认领的后来者收到冲突。只有当前认领人可以上传该任务的资料。
- **地点合并**：合并只记录谱系，旧称与有效期全部保留；不同代际叙述按 `cohort`
  各自留存，空间判断按 `boundary_version` 各自留存，绝不覆盖成一个答案。
- **同意收窄**：受访者收回许可时，只冻结依赖其被收回范围（身份、声音、画面）的
  公开快照；内部研究已使用的最小事实按原依据保留，原始资料不受影响。
- **未成年人**：公开含未成年受访者的材料前，必须具备监护授权与独立复核。
- **专家复核**：退回只把资料置为 `returned`，不改写原始资料；修正须以新业务键
  重新上传。
- **服务恢复**：恢复时重放事件日志，继续处理授权到期（收回公开范围并冻结依赖的
  公开版本）与待复核任务。
- **历史审计**：地点称谓按有效期回答“那一天叫什么”；证据来源、冲突观点与公开
  范围按发生时间重建“那一天系统里有什么”；公众视图始终脱敏。
