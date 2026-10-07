# 基因组数据访问治理

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8304`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `src/local_ledger.py`：机构本地授权账（断网办理取用/撤回，待对账操作队列）。
- `src/federation.py`：按授权编号对账、主账策略变更失效、先落账者胜。
- `src/merger.py`：机构合并的可恢复幂等迁移。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则、失败场景和联盟场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8304
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。
`--ledger-dir`指定机构本地账目录，每个机构一个`<机构编号>.db`文件，默认`./local_ledgers`。

## 核心对象

- `dataset`：受控数据集；`application`：访问申请；`grant`：限时数据使用凭证。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 联盟多机构授权账

- 每家机构各留一份本地账（SQLite，断网可用）。本地登记授权后可离线办理取用`activate`和撤回`revoke`，操作以`pending`入队，重复办理被拒绝；网络恢复后按授权编号对账，本地操作重放到主账，重复对账幂等。
- 对账时主账先做原子归属认领（`grant_ownership`表）：两家机构同时提交同一张授权，只有先落主账的那家生效，另一家得到409冲突，主账里只有一份授权。
- 主账限制数据集（`restrict`）或改期限（`change_term`，会推进`policy_version`）后，本地仍处于`issued`（没启用）的授权立刻失效，转为`review_returned`退回复核，挂起的取用操作标记为`rejected`；已启用的授权不受影响。
- 机构合并（`merge`）把源机构历史授权迁到新机构：任务按授权逐张落检查点，中断后用同一`merge_id`重跑接着做，重复提交同一`merge_id`返回409，不会多出第二份授权。

### 联盟接口

- `POST /api/federation/ledgers/<机构>/grants/<授权编号>/register`：本地登记一张主账授权。
- `POST /api/federation/ledgers/<机构>/grants/<授权编号>/activate`：断网办理取用（入待对账队列）。
- `POST /api/federation/ledgers/<机构>/grants/<授权编号>/revoke`：断网办理撤回。
- `POST /api/federation/ledgers/<机构>/reconcile`：网络恢复后整份本地账对账。
- `GET  /api/federation/ledgers/<机构>/grants`：查本地账授权。
- `GET  /api/federation/ledgers/<机构>/ops`：查挂起操作。
- `GET  /api/federation/reconciliation?institution=<机构>`：查对账记录。
- `POST /api/federation/merges`：`{"from_institution","to_institution","merge_id"?}`发起合并迁移。
- `POST /api/federation/merges/<id>/run`：执行（可带`{"limit":N}`分批，中断后再跑续做）。
- `GET  /api/federation/merges/<id>`：迁移进度与每张授权的检查点状态。

主账侧新增`grant`动作：`change_term`（改期限，保留状态并推进策略版本）、`invalidate`（issued→review_returned）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

数据目录和授权凭证是治理流程演示，不包含真实数据下载、加密或机构身份联邦。
