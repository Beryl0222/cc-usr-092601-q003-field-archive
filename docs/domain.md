# 领域约定

记录青年田野采集、培训资质、地点沿革、受访授权、原始素材、转写版本、空间判断、专家复核、公开用途与数字档案快照的交换契约。

所有发生时间都必须携带时区，版本号从 1 开始递增，基础校验不会改写调用方输入。相同事件标识的业务幂等、冲突隔离、状态推进与历史重建由上层服务负责；本契约只定义可稳定交换的基础事实。

## 聚合对象

- `field_assignment`：采集任务。一个任务只能被一名学生成功认领，认领者承担提交责任。
- `collector_profile`：学生培训资质。资质有有效期，过期后不得认领新任务。
- `place_record`：地点及其称谓沿革。同一地点可有多个曾用名（如「东湖公园」「老鳖坑」），每个称谓携带有效期；地点合并只建立指向关系，保留旧称，不删除也不覆盖。
- `source_asset`：原始文件（照片、录音、录像）及其摘要、文件指纹、采集时引用的测绘边界版本。原始文件一经上传不可改写；专家退回只产生复核意见。
- `consent_record`：受访者同意。授权范围（scope）可授予、更新、收窄、到期；未成年人材料还需监护授权与独立复核事件。
- `transcript`：口述史转写，按版本递增保存，不同代际的叙述作为并列证据保留，不互相覆盖。
- `review_case`：专家复核案件，可退回（要求补正）或通过；退回不改变任何原始资料。
- `archive_snapshot`：面向公众的数字档案快照，按截止时间发布，可因授权收窄而冻结其中部分公开版本。

## 事件类型与载荷

| 事件 | 聚合 | 必填载荷 | 语义 |
| --- | --- | --- | --- |
| `ASSIGNMENT_REGISTERED` | field_assignment | `assignment_key`, `required_certification` | 注册采集任务及其资质要求 |
| `ASSIGNMENT_CLAIMED` | field_assignment | `assignment_key`, `collector_ref`, `certification_id` | 学生认领任务；并发时只有一人获得提交责任 |
| `CERTIFICATION_GRANTED` | collector_profile | `certification_id`, `collector_ref`, `training_code`, `valid_from` | 培训资质生效，可带 `valid_until` |
| `CERTIFICATION_EXPIRED` | collector_profile | `certification_id`, `expired_at` | 资质到期 |
| `PLACE_NAMED` | place_record | `place_id`, `name`, `valid_from` | 登记地点称谓，可带 `valid_until`；同名事件可重复登记不同称谓 |
| `PLACE_MERGED` | place_record | `surviving_place_id`, `merged_place_ids`, `effective_at` | 地点合并，旧地点及其称谓全部保留 |
| `BOUNDARY_REVISED` | place_record | `boundary_ref`, `revision_no`, `effective_at` | 测绘边界修订；素材保存其采集时引用的修订号 |
| `NARRATIVE_RECORDED` | place_record | `narrative_id`, `place_ref`, `generation`, `captured_at` | 记录一代人的叙述；不同代际叙述并列保留 |
| `ASSET_UPLOADED` | source_asset | `business_key`, `content_hash`, `captured_at`, `consent_scope` | 上传原始文件摘要；同业务键仅在指纹与授权范围一致时幂等 |
| `CONSENT_GRANTED` | consent_record | `subject_ref`, `scope`, `granted_at` | 授予授权范围，可带 `valid_until`、`guardian_subject_ref` |
| `CONSENT_UPDATED` | consent_record | `subject_ref`, `scope` | 更新授权（含扩展） |
| `CONSENT_NARROWED` | consent_record | `subject_ref`, `scope`, `effective_at` | 收窄许可；只冻结依赖被收窄范围的公开版本 |
| `CONSENT_EXPIRED` | consent_record | `subject_ref`, `expired_at` | 授权到期，效果等同收窄至内部研究 |
| `TRANSCRIPT_VERSIONED` | transcript | `transcript_id`, `source_asset_id`, `version_no` | 保存新一版转写，旧版本不删除 |
| `REVIEW_OPENED` | review_case | `review_case_id`, `target_ref` | 开启专家复核 |
| `REVIEW_RETURNED` | review_case | `review_case_id`, `reason` | 专家退回；原始资料保持不变 |
| `REVIEW_COMPLETED` | review_case | `review_case_id`, `decision` | 复核结论（通过/驳回） |
| `SNAPSHOT_PUBLISHED` | archive_snapshot | `cutoff_at`, `visibility` | 发布某截止时间的档案快照 |
| `SNAPSHOT_FROZEN` | archive_snapshot | `snapshot_id`, `reason`, `frozen_at` | 冻结快照中依赖特定身份或声音的公开版本 |

## 授权范围（scope）

- `internal_research`：内部研究使用的最小事实（地点、年代、代际等），不依赖受访者身份或声音公开。
- `voice_public`：声音可公开。
- `identity_public`：姓名等身份信息可公开。
- `transcript_public`：转写文本可公开。
- `image_public`：肖像/照片可公开。
- `frozen`：已被冻结，不得继续公开。

收窄许可时，只有依赖被收窄范围的公开版本被冻结；已用于内部研究的最小事实按原依据保留。

## 未成年人材料

`CONSENT_GRANTED` 涉及未成年人时，载荷须带 `guardian_subject_ref`（监护人同意引用），且相关素材公开前必须存在一条 `REVIEW_COMPLETED`（独立复核通过）记录。

## 审计重建

研究人员可按历史日期重放事件，重建：当时的地点称谓与有效期、证据来源（素材与转写版本）、冲突的代际观点，以及该日期可用的公开范围。重建只读取事件日志，不接受事后改写。
