# 无人机飞行计划审批与空域协调系统

标准库独立项目。系统记录运营方计划、航线、机型、备用降落点、载荷、高度、人口风险和应急方案，检查临时禁飞区、高度范围、人口风险、相邻有效计划冲突以及续航放行。审核结果支持离线编号幂等回传，计划变更会使原批准失效并生成通知。

## 模块划分

- `endurance.py`：续航放行计算规则（球面距离累加、风阻折算、返航裕度），纯函数。
- `service.py`：机型与计划数据层及业务规则（仓储、计划生命周期、续航放行编排）。
- `app.py`：办理入口，HTTP 接口层。

## 运行

```bash
python3 app.py --db drone_airspace.db
```

默认监听 `127.0.0.1:8205`，首页 `/`，健康检查 `/health`。

身份头为 `X-User-Id`、`X-Role`；运营方还需 `X-Operator`。角色：`viewer`、`operator`、`airspace_reviewer`、`commander`、`auditor`。

## 主要接口

- `POST /api/aircraft-types`：登记机型资料（巡航速度、满电时长、安全返航分钟）。
- `GET /api/aircraft-types`：查询已登记机型。
- `POST /api/restrictions`：新增临时限制或禁飞区。
- `POST /api/plans`：创建飞行计划，需选择已登记机型（`aircraft_type` 或 `aircraft_type_id`）和备用降落点（`alternate_point`）。
- `GET /api/plans/{id}/check`：检查硬约束和相邻交通冲突。
- `POST /api/plans/{id}/endurance`：续航放行检查，body 可带 `wind_factor`（默认 1.0，范围 1.0–1.5）。
- `POST /api/plans/{id}/submit`、`approve`、`reject`：提交和审核；`approve` 必须填写 `wind_factor`，续航不足或备用点落入生效限制即退回并说明还差多少分钟；审核使用 `offline_id` 保证断网重连幂等。
- `POST /api/plans/{id}/change`、`cancel`：版本化变更与取消；航线、机型或备用点变更后原批准立即失效。缺少机型参数的旧计划不能再次提交，需先变更补齐。
- `GET /api/notifications`、`POST /api/expire`：通知与到期处理。
- `GET /api/state`：按角色返回计划、限制、机型和公开信息。

## 续航放行规则

按相邻航点球面直线距离累加航线里程，除以机型巡航速度折算耗时，乘审核员填写的风阻系数（1.0–1.5），再加安全返航分钟，总和不得超过机型满电时长；备用降落点不得落入与计划时间重叠的生效限制。任一不满足即退回，并给出还差多少分钟或命中的限制。紧急授权不能覆盖续航放行。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

空域几何使用经纬度矩形和航线包围盒近似，备用降落点按平面点包含判断且不按高度分层，续航距离按球面 haversine 累加，不包含多边形、椭球距离、地形、实时遥测和完整间隔标准。紧急授权只能覆盖空域及交通冲突，不能绕过载荷、高度硬限制和续航放行。身份头、无签名离线审核以及单机 SQLite 适合原型，生产环境需要 PKI、真实 GIS 引擎和跨机构事件总线。
