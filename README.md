# 青年遗产田野资料治理

在领域事件契约之上实现青年田野资料治理服务：采集任务、培训资质、地点沿革、受访者同意、原始文件摘要、转写版本、空间判断、专家复核、公开用途与数字档案快照分层保存。

## 目录

- `contracts/domain.schema.json`：聚合、事件与载荷字段契约。
- `docs/domain.md`：领域语义，含授权范围、未成年人材料与审计重建约定。
- `data/sample.json`：可直接校验的联调样例。
- `src/field_archive/`
  - `contracts.py`：事件信封基础校验（不改写调用方输入）。
  - `store.py`：线程安全的 JSONL 事件存储，条件追加（乐观并发）、事件标识幂等、按追加顺序重放，服务重启即恢复。
  - `service.py`：全部治理规则与公开/审计视图。
  - `cli.py`：校验、审计、公众档案、恢复推进命令。
- `tests/`：契约与治理规则测试（30 个用例）。

## 核心规则

- **乱序重试幂等上传**：相同业务键只有文件指纹 `content_hash` 与授权范围 `consent_scope` 都一致才视为重传；不一致直接拒绝，绝不覆盖。
- **并发认领唯一责任**：多名学生并发认领同一采集项，只有第一个资质有效的学生获得提交责任；本人重试幂等返回原事件。
- **地点沿革不覆盖**：同一地点可登记多个称谓及有效期（「老鳖坑」「东湖公园」）；地点合并只建立指向关系，旧称、有效期与各代际叙述全部保留。
- **测绘边界**：边界修订号递增；素材保存采集时引用的修订号，空间复核会对照现行修订号。
- **授权收窄定向冻结**：受访者收窄许可时，只冻结依赖被收窄范围（身份/声音/转写/肖像）的公开版本；已用于内部研究的最小事实按原依据保留。
- **未成年人材料**：必须携带监护人授权引用，且公开前需独立复核（`kind=independent`）通过。
- **专家退回不改原始资料**：退回只产生复核意见与轮次，原始素材事件流保持不变。
- **恢复续办**：服务重启后 `run-due` 继续处理授权/资质到期与待复核案件，推进本身幂等。
- **公众脱敏档案**：未授权姓名的受访者以稳定假名出现，已冻结/已失效条目不展示。
- **历史审计**：按历史日期重放，重建地点称谓与有效期、证据来源（素材指纹、边界修订、转写版本）、冲突的代际观点与当时可用的公开范围。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests
```

## 命令行

```bash
# 样例/事件契约校验（两种写法等价）
PYTHONPATH=src python3 -m field_archive.cli contracts/domain.schema.json data/sample.json
PYTHONPATH=src python3 -m field_archive.cli validate <schema.json> <event.json>

# 研究人员审计：按历史日期重建
PYTHONPATH=src python3 -m field_archive.cli audit events.jsonl --as-of 2026-06-15T00:00:00+08:00

# 公众脱敏档案
PYTHONPATH=src python3 -m field_archive.cli public events.jsonl --as-of 2026-06-15T00:00:00+08:00

# 服务恢复后推进到期任务并列出待复核案件
PYTHONPATH=src python3 -m field_archive.cli run-due events.jsonl --as-of 2026-10-01T00:00:00+08:00
```

## 作为库使用

```python
from field_archive import FieldArchiveService

svc = FieldArchiveService("events.jsonl")   # 文件存在即重放恢复
svc.grant_certification("cert-1", "student-7", "T-ORAL", "2026-01-01T00:00:00+08:00")
svc.register_assignment("donghu", "T-ORAL")
svc.claim_assignment("donghu", "student-7", "cert-1")
svc.grant_consent("elder-3", ["voice_public"], valid_until="2027-01-01T00:00:00+08:00")
svc.upload_asset("a1", "bk-1", "sha256:...", "2026-04-03T08:00:00+08:00", "voice_public",
                 kind="audio", place_ref="P", boundary_ref="B", boundary_revision=1,
                 subject_ref="elder-3", summary="老人讲述老鳖坑来历")
svc.publish_snapshot("snap-1", "2026-05-01T00:00:00+08:00")
svc.narrow_consent("elder-3", ["voice_public"], reason="撤回声音公开")  # 只冻结声音公开版本
view = svc.rebuild_as_of("2026-05-01T00:00:00+08:00")                   # 历史审计
catalog = svc.public_catalog()                                          # 公众脱敏档案
```

所有发生时间必须携带时区；事件只追加，状态由重放投影得到。
