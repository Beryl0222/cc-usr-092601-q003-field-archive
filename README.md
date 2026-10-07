# 青年遗产田野资料治理

记录青年田野采集、地点沿革、受访授权和档案版本的交换契约，以及其上的
治理服务：幂等上传、冲突隔离、状态推进、历史审计与公众脱敏视图。

## 目录

- `contracts/domain.schema.json`：对象、事件和载荷字段约定。
- `data/sample.json`：可直接校验的单事件联调样例。
- `data/sample_journal.jsonl`：覆盖认领、上传、合并、收窄、冻结的完整事件日志样例。
- `src/field_archive/contracts.py`：基础契约校验。
- `src/field_archive/store.py`：追加式事件日志，按事件标识幂等。
- `src/field_archive/service.py`：治理服务（认领、上传、同意、复核、快照、恢复）。
- `src/field_archive/audit.py`：按历史日期重建称谓、证据、冲突观点与公开范围。
- `src/field_archive/public_api.py`：面向公众的脱敏档案。
- `src/field_archive/cli.py`：契约校验命令行。
- `src/field_archive/service_cli.py`：恢复与审计命令行。
- `tests/`：信封、时间、版本、事件载荷与服务行为测试。
- `docs/domain.md`：领域对象、事件语义与治理规则。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests
```

## 样例校验

```bash
PYTHONPATH=src python3 -m field_archive.cli contracts/domain.schema.json data/sample.json
```

样例有效时输出 `valid`；发现问题时逐行给出字段、代码和中文说明，并返回非零状态。

## 治理服务命令行

```bash
# 服务恢复：继续授权到期处理并列出待复核任务
PYTHONPATH=src python3 -m field_archive.service_cli data/sample_journal.jsonl \
  recover --now 2026-10-07T09:00:00+08:00

# 按历史日期重建地点称谓（旧称与有效期全部保留）
PYTHONPATH=src python3 -m field_archive.service_cli data/sample_journal.jsonl \
  audit-place-names --place place-donghu-north --at 1985-06-01T00:00:00+08:00

# 按历史日期重建证据来源 / 冲突观点 / 当时可用的公开范围
PYTHONPATH=src python3 -m field_archive.service_cli data/sample_journal.jsonl \
  audit-evidence --place place-donghu --at 2026-10-01T00:00:00+08:00
PYTHONPATH=src python3 -m field_archive.service_cli data/sample_journal.jsonl \
  audit-conflicts --place place-donghu --at 2026-10-01T00:00:00+08:00
PYTHONPATH=src python3 -m field_archive.service_cli data/sample_journal.jsonl \
  audit-public-scope --at 2026-04-15T00:00:00+08:00

# 面向公众的脱敏档案（未授权姓名显示为“匿名受访者”）
PYTHONPATH=src python3 -m field_archive.service_cli data/sample_journal.jsonl \
  public-archive --at 2026-04-15T00:00:00+08:00
```
