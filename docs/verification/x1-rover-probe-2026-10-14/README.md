# X1 地面车 SITL 实测（2026-10-14）

这是 [X1 实施方案](../../x1-implementation.md) §4 规定的「先实测再写代码」。它只是一次测量，不是发布门禁，也不计入任何验收。

## 怎么测的

- **地面车**：M0 基础镜像 `drone-agent-sitl:px4-1.17.0-m0`（PX4 v1.17.0，`d6f12ad1`）里的 `gz_rover_differential`（airframe 50000），空默认世界，PX4 不做任何修改。
- **探针**：在 `drone-agent-m1-aircraft:6249129…` 中运行（MAVSDK 3.17.4），共享 SITL 容器的网络命名空间；容器无外网。
- **主机侧**：持项目锁运行，结束后删除容器与临时文件。
- **脚本**：本目录的 `rover_probe.py` 与 `run_probe.py`。原始结果在 `result.json`（各阶段、参数、事件，以及每 0.4 s 一个样本）与 `report.json`（编排记录与 PX4 日志尾部）。

## 结果

| 项 | 实测 | 对设计的影响 |
|---|---|---|
| 关键参数 | `SYS_AUTOSTART` 50000，`MAV_TYPE` 10；`COM_DISARM_PRFLT` 10 s，`COM_DISARM_LAND` 2 s；`NAV_ACC_RAD` 0.5 m；`RO_MAX_THR_SPEED` 2.0 m/s，`RO_SPEED_LIM` 1.8 m/s；`SIM_BAT_DRAIN` 60 s | 仿真电量 60 s 就放完，X1 仿真要像 M3 一样另设 |
| 解锁 | 无遥控输入，HOLD 中 `arm` 即成功 | 不改参数 |
| `in_air` | 未解锁时为 false；解锁并静止时先是 false，随后变为 true；行驶时为 true。`landed_state` 依次出现 ON_GROUND、IN_AIR、LANDING | `in_air` 跟随解锁状态，不表示离地。地面车一律不读它，改用 armed 加水平速度 |
| 自动上锁 | 解锁后静止 20 s 未上锁；任务结束停在 HOLD 中 15 s 未上锁；RTL 到家后约 14 s 上锁 | 正常停车后飞控不会上锁，`stopped_disarmed` 必须由本项目显式完成 |
| 任务末点 | 约 12 m 的两段任务 11 s 完成，随后自动转 HOLD，1.8 s 内停稳，终点误差 0.553 m | 到点容差取 0.8 m 量级 |
| RTL | 从 (5.94, 5.47) 回家，轨迹偏离直线最多 0.167 m，停在离家约 0.53 m 处 | RTL 是直线，会穿过禁入区与障碍。首版地面恢复不用 RTL |
| 行驶中 Hold | 1.18 m/s 时 0.5 s 停住，滑行 0.313 m；模式遥测比实际停车晚约 1 s | 前瞻时长与限速据此设定；判断停车看速度，不看模式遥测 |
| 行驶中 disarm | 1.04 m/s 行驶时普通 `disarm` 被接受，1 s 内上锁并停住 | 飞控不拦行驶中上锁，适配器的「只在静止时上锁」守卫是唯一屏障，须由契约测试钉住 |
| 静止 disarm | HOLD 中静止时 `disarm` 被接受，约 1 s 上锁 | 骨架的上锁步骤与终态上锁都走这条路径 |

这些结论写入[安全体系 §12](../../architecture/03-safety.md) 与[实施方案 §14](../../x1-implementation.md)。
