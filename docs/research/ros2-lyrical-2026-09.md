# ROS 2 Lyrical 评估纪要（WP-M3-05，截至 2026-09）

[决策记录 D043](../decisions.md) · [M3 实施计划](../m3-implementation.md)

> 目的：按 D008 核查切换到 Lyrical Luth 的条件（`px4_msgs`、Nav2、BehaviorTree.ROS2、`rmw_zenoh` 均有稳定发布），并补查对 PX4 v1.17.0 + uXRCE-DDS + `px4_ros2` 这套栈有实质影响的其他项。
> 观察口径：所有链接与提交均在 2026-09-24（UTC）实查。「apt」指 packages.ros.org 的 `ros2` 主仓库（resolute / noble 索引生成于 2026-09-23 20:25 UTC）；「rosdistro」指 `ros/rosdistro` master@`de24d60`；分支列表用 `git ls-remote` 取得。没有一手证据的写「未能核实」。

## 结论

**D008 的切换条件不满足，M3 维持 Jazzy + PX4 v1.17.0 + Gazebo Harmonic。** 四个组件中只有 Nav2 和 `rmw_zenoh` 在 Lyrical 有正式二进制发布。`px4_msgs` 和 BehaviorTree.ROS2 在 Lyrical 没有正式发布，在 Jazzy 也同样只有源码，说明「均有稳定发布」这条条件照字面永远达不到，需要改写。真正卡住切换的是 D008 没列出的 PX4 侧。PX4 文档写明：Lyrical 用 Fast DDS 3.6.x，需要搭配 Micro-XRCE-DDS-Agent 3.0.1，PX4 也必须打开 `UXRCE_DDS_CLIENT_USE_DDS_V3` 重新构建。这个 Kconfig 选项在 v1.17.0 源码里不存在，从 v1.18 起才有，而 v1.18 目前只有 `v1.18.0-rc1`（2026-09-10）。另外，Lyrical 的 Tier 1 平台只有 Ubuntu 26.04，Noble 是 Tier 3，只能源码构建；PX4 开发环境文档却写明「Ubuntu 26.04 is not yet supported」；`px4-ros2-interface-lib` 所有分支的 CI 和打包都不含 Lyrical。所以切到 Lyrical 不只是换 ROS 发行版，还要同时把 PX4 升到 1.18、基础镜像换成 26.04、ROS 节点的系统 Python 升到 3.14、Gazebo 换成 Jetty。建议在 `decisions.md` 记一条 D008 重估记录，把切换条件改成下文「重估触发器」里可以核查的事件，重点是补上 PX4 侧条件。不另立切换条目。

**本项目实测补充（2026-09-24）**：上游 `release/1.17` 的 CI 不覆盖 Jazzy，本项目在固定基础镜像（ROS 2 Jazzy ros-base、MicroXRCEAgent）中构建 `px4_msgs`（打包 `86d8239`、消息取自 PX4 v1.17.0）与 `px4_ros2_cpp`（`4a3370f`），用 PX4 v1.17.0 SITL 核实了消息兼容性检查通过、外部模式注册为 `nav_state 23`（D039 实施记录）。这证明的是 Jazzy 基线可用，不涉及 Lyrical。

## 组件状态

| 组件 | Jazzy 状态 | Lyrical 状态 | 证据（链接 + 观察日期/提交） | 是否满足 D008 条件 |
|---|---|---|---|---|
| `px4_msgs` | rosdistro 只有 source 条目（`main`），没有 release；apt 没有 `ros-jazzy-px4-msgs`。项目锁定的 `release/1.17`（`86d8239`，即 tag / GitHub release `v1.17.0`，发布于 2026-06-09）的 CI 只测 foxy / humble / rolling，没有测 Jazzy | 同样只有 source 条目，apt（resolute）没有 `ros-lyrical-px4-msgs`。仓库按 PX4 版本线分支，没有 lyrical 分支。`release/1.18`（`935c120`，2026-09-10，跟随 v1.18.0-rc1）和 `main` 的 CI 在 `ros:lyrical-ros-base` 容器中构建，并打 amd64/arm64 deb；这些是 CI 产物，不是正式发布。`release/1.17` 没有 Lyrical CI | [lyrical/distribution.yaml](https://github.com/ros/rosdistro/blob/master/lyrical/distribution.yaml)（文件最后提交 `a591482`，2026-09-22）；[README 支持矩阵](https://github.com/PX4/px4_msgs#supported-versions-and-compatibility)（main `d49263b`）；`build.yml` 见 [release/1.17](https://github.com/PX4/px4_msgs/blob/release/1.17/.github/workflows/build.yml) 与 [release/1.18](https://github.com/PX4/px4_msgs/blob/release/1.18/.github/workflows/build.yml)；要求发布二进制的 [#41](https://github.com/PX4/px4_msgs/issues/41) 自 2024-08 开着未关 | 否。没有正式发布；即使理解为「有对应 PX4 版本的 tag，且在 Lyrical 上 CI 通过」也不满足，因为 v1.18 还没有 tag |
| `px4-ros2-interface-lib`（`px4_ros2_cpp`，D008 未列） | `release/1.17`（`4a3370f`，2026-06-09）的 build_and_test 只跑 humble。三个分支的 Debian 打包矩阵都是 humble(jammy) / jazzy(noble) / rolling(noble)，并附 `rosdep-{humble,jazzy,rolling}.yaml`。`release/1.18`（`c3e410f`，2026-07-03）和 main（`72dbd86`，2026-09-23，对应 release 2.2.3）的 build_and_test 跑 jazzy | 所有分支都没有 Lyrical 的 CI、打包或 rosdep 文件；rolling 的打包仍在 noble 上，而 Rolling 已迁到 resolute。[PR #211](https://github.com/Auterion/px4-ros2-interface-lib/pull/211) 才会加入容器内的 Lyrical CI 和 Lyrical rosdep 规则；它 2026-07-16 开启，最后更新于 2026-07-31，至今未合并且有冲突 | [分支列表](https://api.github.com/repos/Auterion/px4-ros2-interface-lib/branches)；三个分支上的 [build_and_test.yml](https://github.com/Auterion/px4-ros2-interface-lib/blob/main/.github/workflows/build_and_test.yml) 与 [build-publish-debian-packages.yml](https://github.com/Auterion/px4-ros2-interface-lib/blob/main/.github/workflows/build-publish-debian-packages.yml)；[px4_ros2_cpp/](https://github.com/Auterion/px4-ros2-interface-lib/tree/release/1.17/px4_ros2_cpp) 下的 rosdep 文件 | D008 未列；不满足，建议补入条件 |
| Nav2 | `jazzy` 分支；rosdistro 1.3.13-1；apt 有 `ros-jazzy-navigation2` 1.3.13 | `lyrical` 分支（头 `dc550eb`，2026-09-18）；rosdistro 1.5.2-2（tag `1.5.2`，`7b9be24`，2026-09-16）；apt（resolute）有 `ros-lyrical-navigation2` 1.5.1-1，amd64 和 arm64 都有 | [navigation2@lyrical](https://github.com/ros-navigation/navigation2/tree/lyrical)；[lyrical/distribution.yaml](https://github.com/ros/rosdistro/blob/master/lyrical/distribution.yaml)；[resolute amd64 Packages.gz](http://packages.ros.org/ros2/ubuntu/dists/resolute/main/binary-amd64/Packages.gz) | 是。本项目要到 M5 地面平台才用 Nav2 |
| BehaviorTree.ROS2 | 不在 rosdistro，apt 没有 `ros-jazzy-behaviortree-ros2`，只能源码构建。默认分支 `humble`（最后提交 `6c6aa07`，2025-11-25），CI 只测 humble；最新 release `0.3.0`（2025-10-30） | 没有 lyrical 分支（全部分支只有 `humble`、`bug_fix/registry`、`issue_76`），没有 Lyrical CI，也没有二进制。Lyrical Tutorial Party 报告的 Ctrl-C 异常 [#131](https://github.com/BehaviorTree/BehaviorTree.ROS2/issues/131)（2026-05-09）仍开着且无人回复。它依赖的 `behaviortree_cpp` 4.10.0 在 Lyrical 有二进制 | [仓库](https://github.com/BehaviorTree/BehaviorTree.ROS2)；[test.yml](https://github.com/BehaviorTree/BehaviorTree.ROS2/blob/humble/.github/workflows/test.yml)；rosdistro 的 jazzy / lyrical / rolling 都没有这个仓库 | 否。Jazzy 按字面同样不满足 |
| `rmw_zenoh` | `jazzy` 分支；rosdistro 0.2.11-1，apt 0.2.10；REP 2000 的 Jazzy 中间件表没有列入 `rmw_zenoh` | `lyrical` 分支头就是 tag `0.10.6`（`4e17ccf`，2026-09-12）；rosdistro 0.10.6-1；apt（resolute）有 `ros-lyrical-rmw-zenoh-cpp` 0.10.6-1（amd64/arm64）；已进入 core `ros2.repos`。官方列为 Tier 1，但默认 RMW 仍是 `rmw_fastrtps_cpp`。jazzy 和 lyrical 分支都 vendor 了 zenoh-c 1.8.0（附若干修复） | [rmw_zenoh@lyrical](https://github.com/ros2/rmw_zenoh/tree/lyrical)；[Lyrical Supported Platforms](https://docs.ros.org/en/lyrical/Releases/lyrical/supported-platforms.html)（源文件最后提交 `8894014`，2026-08-28）；[REP 2000 源文件](https://github.com/ros-infrastructure/rep/blob/master/rep-2000.rst)（最后提交 `bd7ddd3`，2025-07-03，只写到 Kilted）；`zenoh_cpp_vendor/CMakeLists.txt` | 是 |
| PX4 uXRCE-DDS client / Agent（D008 未列） | Fast DDS 2.14 系列；PX4 文档配 Agent v2.4.3；client 默认 v2.x，PX4 v1.17.0 直接可用 | Fast DDS 3.6.x。PX4 文档要求 Agent 3.0.1，并要求 PX4 打开 `UXRCE_DDS_CLIENT_USE_DDS_V3` 自行构建（文档标注为 PX4 v1.18 功能）。该 Kconfig 在 `v1.17.0` 中不存在，在 `v1.18.0-rc1`（2026-09-10）和 main 中存在。v1.17 文档明写 v2.x client 与 v3.x Agent 不兼容。Agent 已发布 v3.0.2（2026-09-03），PX4 文档仍写 3.0.1 | uXRCE-DDS 页面 [main](https://docs.px4.io/main/en/middleware/uxrce_dds)（`release/1.18` 的表格相同）与 [v1.17](https://docs.px4.io/v1.17/en/middleware/uxrce_dds)；`src/modules/uxrce_dds_client/Kconfig` 在 [v1.17.0](https://github.com/PX4/PX4-Autopilot/blob/v1.17.0/src/modules/uxrce_dds_client/Kconfig) 与 [v1.18.0-rc1](https://github.com/PX4/PX4-Autopilot/blob/v1.18.0-rc1/src/modules/uxrce_dds_client/Kconfig) 的版本；[Agent releases](https://github.com/eProsima/Micro-XRCE-DDS-Agent/releases) | D008 未列；**阻塞**，需要 PX4 v1.18 或更高的正式版 |
| 平台与工具链（D008 未列） | Ubuntu 24.04 为 Tier 1；系统 Python 3.12；PX4 的 CI 与发布目标是 24.04；PX4 main 版 ROS 2 指南推荐 Jazzy/24.04（v1.17 版指南仍写 Humble/22.04） | Tier 1 只有 Ubuntu 26.04（amd64/arm64 deb）；Noble 是 Tier 3，只能源码构建，并在 2029-06-01 提前 EOL；noble 的 apt 索引中 `ros-lyrical-*` 包数为 0。系统 Python 3.14.3；默认 C++20。PX4 main 开发环境文档写明「Ubuntu 26.04 is not yet supported」。PX4 main、v1.17、v1.18 三个版本的 ROS 2 指南都没有把 Lyrical 列为支持平台 | [Lyrical Supported Platforms](https://docs.ros.org/en/lyrical/Releases/lyrical/supported-platforms.html)；[PX4 Ubuntu 开发环境](https://docs.px4.io/main/en/dev_setup/dev_env_linux_ubuntu)；ROS 2 指南 [main](https://docs.px4.io/main/en/ros2/user_guide) 与 [v1.17](https://docs.px4.io/v1.17/en/ros2/user_guide)；[C++20 讨论](https://discourse.openrobotics.org/t/ros-2-lyrical-c-version/52551)（2026-02-17） | D008 未列；成本高：基础镜像、Python 和 Jetson 用户态要一起变 |

## Gazebo 配对

- **Lyrical 官方配对是 Jetty。** Lyrical 平台页在 Ubuntu Resolute 一栏列的是 Jetty。Gazebo 兼容表对 Lyrical 的评级是：Jetty ✅；Harmonic / Ionic ⚡，即可行但需谨慎，要用非官方二进制或从源码编译 `ros_gz`，而该页列出的 OSRF 非官方包只有 Humble↔Harmonic 一组。对 Jazzy 则只有 Harmonic ✅，Jetty 标为 ❌。所以只要 M3 感知链路用 `ros_gz` 桥接，Gazebo 就必须和 ROS 一起切换：Jazzy 配 Harmonic，Lyrical 配 Jetty。
- **Lyrical 的 `ros_gz` 已发布。** 版本 3.0.10-1，apt（resolute）amd64/arm64 都有。其 `gz_sim_vendor` 0.4.6 封装 gz-sim 10.5.0（Jetty）；Jazzy 的 `gz_sim_vendor` 0.0.13 封装 gz-sim8（Harmonic）。
- **生命周期一一对应。** Harmonic 从 2023-09 支持到 2029-05，与 Jazzy 同时 EOL；Jetty 从 2025-09 支持到 2031-05，与 Lyrical 同时 EOL。Jetty 自己的官方平台只列 Ubuntu Noble amd64，arm 等平台是 best-effort；Lyrical 下用的 Jetty 来自 ROS 构建农场的 vendor 包。
- **PX4 侧。** v1.17 和 main 文档都写着「Harmonic、Ionic、Jetty 在 Ubuntu 24.04 上受支持，默认使用已安装的最新版」。v1.17.0 的 `gz_plugins` / `gz_bridge` CMake 先找不带版本号的 `gz-transport` / `gz-sim`，再找 14/9 和 13/8 版；gz-transport ≥ 15 时使用不带版本号的目标名，这与 Jetty 的命名一致。不过 `Tools/setup/ubuntu.sh` 默认仍安装 Harmonic，我们也没有 PX4 + Jetty 的 SITL 回归数据。
- **判断。** 换 Gazebo 会改变 M1 回归基线（D022 固定的仿真镜像），应作为单独变更，用固定场景集评测，不能夹带在 ROS 切换里一起做。

## 重估触发器

下面任一事件发生就重新核查。前三项全部满足之前不启动切换。

1. PX4 发布 v1.18.0 或更高的正式版，其 ROS 2 用户指南把 Lyrical 列为支持平台，而且开发环境文档不再写「Ubuntu 26.04 is not yet supported」。
2. `px4-ros2-interface-lib` 合并 Lyrical CI（PR #211 或等价改动），并且我们要用的 PX4 版本线的 release 分支在 Lyrical 上 CI 通过。
3. `px4_msgs` 在 `lyrical/distribution.yaml` 中出现 release 条目，或者至少我们锁定的版本线在 Lyrical 上 CI 通过。
4. BehaviorTree.ROS2 出现覆盖 Lyrical 的分支 / CI，或在 rosdistro 发布。如果项目最终不依赖它，就从 D008 条件中删去：M3 计划里没有用到它，Nav2 也是直接依赖 `behaviortree_cpp`。
5. 季度技术雷达（下一次在 2026-10）照例复查。硬截止是 Jazzy 与 Harmonic 都在 2029-05 EOL，迁移评估最迟在 2028-04 的雷达启动。

D008 条件建议改写为：PX4 正式版文档支持 Lyrical（包括 uXRCE-DDS v3 client），且 `px4_ros2` 在该版本线上有 Lyrical CI；Nav2 和 `rmw_zenoh` 有 Lyrical 二进制；项目实际使用的源码依赖在 Lyrical 上 CI 通过；PX4 SITL 在 Lyrical 目标平台上的部署方案（26.04，或分容器）通过 M1 回归验证。

## 未能核实

- PX4 v1.17（v2.x client）+ Agent v2.4.3 能否在 RTPS 层直接与 Lyrical（Fast DDS 3.6.x）节点互通。PX4 文档只说要按大版本配对，没有给出混用的结论。
- PX4 v1.17 的 zenoh-pico 能否与 zenoh-c 1.8.0 的 `rmw_zenoh` 互通。该 zenoh-pico 来自子模块分支 `dev/1.0.0-px4`，文档标为 Experimental。Jazzy 分支 vendor 的同样是 1.8.0，所以这一点不影响 Jazzy 与 Lyrical 的比较。
- Lyrical 默认 C++20，`px4_ros2_cpp`（未指定标准时默认 C++17）和 BehaviorTree.ROS2（固定 C++17）能否直接构建。
- 26.04 用户态的 Lyrical 容器在 JetPack 6 / L4T 36 主机上能否使用 GPU。
- `px4_msgs` 的 `release/1.17` 在 Lyrical 上能否构建。该分支没有 CI 覆盖，1.18 线和 main 已覆盖。

## 来源

1. ROS 2 Lyrical 平台与中间件：<https://docs.ros.org/en/lyrical/Releases/lyrical/supported-platforms.html>（`ros2/ros2_documentation` lyrical@`847854f`）；发布说明与时间线（GA 2026-05-22，EOL 2031-05）：<https://docs.ros.org/en/lyrical/Releases/Release-Lyrical-Luth.html>、<https://docs.ros.org/en/lyrical/Releases/lyrical/release-timeline.html>
2. core 仓库清单（`rmw_zenoh: lyrical`、`Fast-DDS: 3.6.x`）：<https://github.com/ros2/ros2/blob/lyrical/ros2.repos>
3. rosdistro：<https://github.com/ros/rosdistro/blob/master/lyrical/distribution.yaml>、<https://github.com/ros/rosdistro/blob/master/jazzy/distribution.yaml>（master@`de24d60`）
4. apt 索引：<http://packages.ros.org/ros2/ubuntu/dists/resolute/main/>（amd64、arm64）、<http://packages.ros.org/ros2/ubuntu/dists/noble/main/binary-amd64/Packages.gz>（均生成于 2026-09-23 20:25 UTC）
5. REP 2000（只写到 Kilted，Lyrical 起改由发布文档维护）：<https://github.com/ros-infrastructure/rep/blob/master/rep-2000.rst>（`bd7ddd3`）
6. px4_msgs：<https://github.com/PX4/px4_msgs>（main `d49263b`、release/1.17 `86d8239`、release/1.18 `935c120`）；发布：<https://github.com/PX4/px4_msgs/releases>；构建农场改造 PR #68（2026-06-12 合并）：<https://github.com/PX4/px4_msgs/pull/68>
7. px4-ros2-interface-lib：<https://github.com/Auterion/px4-ros2-interface-lib>（release/1.17 `4a3370f`、release/1.18 `c3e410f`、main `72dbd86`）；releases：<https://github.com/Auterion/px4-ros2-interface-lib/releases>；PR #211：<https://github.com/Auterion/px4-ros2-interface-lib/pull/211>
8. Nav2：<https://github.com/ros-navigation/navigation2/tree/lyrical>（`dc550eb`；tag 1.5.2 `7b9be24`）
9. BehaviorTree.ROS2：<https://github.com/BehaviorTree/BehaviorTree.ROS2>（humble `6c6aa07`；tag 0.3.0 `72a3bf5`）；issue #131
10. rmw_zenoh：<https://github.com/ros2/rmw_zenoh/tree/lyrical>（`4e17ccf` = 0.10.6）；vendor 版本：`zenoh_cpp_vendor/CMakeLists.txt`（jazzy、lyrical 分支）
11. PX4 uXRCE-DDS：<https://docs.px4.io/main/en/middleware/uxrce_dds>、<https://docs.px4.io/v1.17/en/middleware/uxrce_dds>；Kconfig：tag `v1.17.0`（2026-05-13）与 `v1.18.0-rc1`（2026-09-10）；main@`e370d98`
12. PX4 ROS 2 指南与开发环境：<https://docs.px4.io/main/en/ros2/user_guide>、<https://docs.px4.io/v1.17/en/ros2/user_guide>、<https://docs.px4.io/main/en/dev_setup/dev_env_linux_ubuntu>
13. PX4 Gazebo 与 Zenoh：<https://docs.px4.io/v1.17/en/sim_gazebo_gz/>；`src/modules/simulation/gz_plugins/CMakeLists.txt`@v1.17.0；<https://docs.px4.io/main/en/middleware/zenoh>；`.gitmodules`@v1.17.0（zenoh-pico `dev/1.0.0-px4`）
14. Micro-XRCE-DDS-Agent releases（v2.4.3 2024-03-20、v3.0.1 2025-03-18、v3.0.2 2026-09-03）：<https://github.com/eProsima/Micro-XRCE-DDS-Agent/releases>
15. Gazebo–ROS 配对表：<https://gazebosim.org/docs/latest/ros_installation/>（源文件 `gazebosim/docs` master@`26274ca` `common/ros_installation.md`）；发布与 EOL：<https://gazebosim.org/docs/latest/releases/>；Jetty 平台：<https://gazebosim.org/docs/jetty/install/>；`gz_sim_vendor` lyrical `package.xml`（gz-sim 10.5.0）：<https://github.com/gazebo-release/gz_sim_vendor/tree/lyrical>
16. Lyrical 默认 C++20 讨论（2026-02-17）：<https://discourse.openrobotics.org/t/ros-2-lyrical-c-version/52551>
