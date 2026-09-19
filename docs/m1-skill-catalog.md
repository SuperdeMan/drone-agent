# M1 技能草案与执行边界

五个 [SkillManifest 配置](../configs/skills/) 面向 `uav_01` 的园区仿真目标，均为 M0 草案，没有注册为可执行技能。参数范围是待 M1 场景验证的保守初值；能源估计留空，须由实测模型计算，不能用猜测填成能力。

| 技能 | 参数与完成证据 | 资源与取消 |
|---|---|---|
| `skill.flight.takeoff` | 3–30 m AGL；新鲜高度达到目标并稳定 2 s | 独占 motion；起飞过程中取消转 guardian 恢复 |
| `skill.flight.fly_route` | 登记的 route_id、坐标系与地图版本、≤ 5 m/s；逐航点证据完整 | 独占 motion；可暂停 ≤ 120 s，须满足定位与能源条件 |
| `skill.flight.capture_image` | asset_id、cam_0、质量配置引用；对象、哈希、位姿协方差、时间窗与质量同时有效 | 独占 camera；仅停采集，不能自行决定飞行恢复 |
| `skill.flight.return_home` | home_ref 与返航航线引用；确认到达空中的 home holding point | 独占 motion；不能取消 guardian 的安全返航 |
| `skill.flight.land` | 当前降落点必须已验证并预约；on_ground 且 disarmed 稳定 2 s | 独占 motion；下降期间延后取消，不提供空中停电机 |

`return_home` 的业务技能完成点是返航等待点，之后由 `land` 完成着陆。它不能简单等同于可能自动着陆的 PX4 RTL；适配器应使用已验证返航航线。恢复策略的 `rtl` 是飞控原生恢复行为，独立于这两个业务节点。发生飞控接管时，应记录中止或恢复结果，不能把未执行完的业务节点标成成功。

同机器人两个 motion 技能不能并行。纯采集技能只占相机，允许与航线技能组合，但仍需采集位姿与影像质量证据；如果后续技能需要转向或定点观测，必须增加 motion 资源声明。相机先按独占处理，避免两个快门流程争用。

谓词由 M1 的确定性注册表实现，所有未实现或缺观测的谓词为 unknown，准入与前置条件失败关闭。`package_authorized` 检查版本/审批/哈希；`*_lease_valid` 检查所需资源；`*_resolved`、`home_verified`、`*_site_verified*` 从已批准地图登记表解析并核对坐标系/版本；定位、能源、新鲜度和飞控状态来自本机观测。任何谓词都不能由 Planner 文本直接宣称为真。

`m1_campus_v1.*` 是未来场景阈值配置引用，当前只在草案中声明；M1 编译器在对应配置存在并经验证前必须拒绝执行。没有实际阈值时不产生 verified 证据。

M0 的 `gz_x500` 仅用于飞控/世界冒烟，不含本项目的相机证据管线。平台 YAML 中的 cam_0 与五技能列表是目标能力清单；M1 必须选择并验证相机模型、接通图像与位姿时间同步，再由运行适配器如实发布能力。默认 x500 启动成功不能证明 capture_image 可用。

故障场景见 [注入矩阵](../configs/scenarios/m0_fault_matrix.yaml)，通过结果后续进入运行证据与恢复边验证记录。
