"""Ask Windows not to slow this Python process down (EcoQoS), for unattended local S0 runs (D084).

While the PC is unattended, Windows throttles background processes; the S0 worlds then run about 2.3 times slower
and the 100-node P3 rung times out whatever the code. Put this directory on PYTHONPATH and Python imports this file at
start-up in every process the run starts (the setting itself is not inherited, the environment variable is). It
calls SetProcessInformation(ProcessPowerThrottling) for the calling process only; no system setting changes, and on
other platforms it does nothing.

让 Windows 不对本 Python 进程降速（EcoQoS），用于无人值守的本机 S0 运行（D084）。开发机无人操作时 Windows 会对后台进程
降速，S0 世界因此慢约 2.3 倍，100 节点 P3 档无论代码如何都会超时。把本目录加入 PYTHONPATH 后，运行启动的每个进程都会在
启动时导入本文件（设置本身不继承，环境变量会继承）。它只对调用进程调用 SetProcessInformation(ProcessPowerThrottling)；
不改任何系统设置，其他平台上什么也不做。
"""

import sys

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _PowerThrottling(ctypes.Structure):
        _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG), ("StateMask", wintypes.ULONG)]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _kernel32.SetProcessInformation.restype = wintypes.BOOL
    # Execution speed and timer resolution under our control, both not throttled (high QoS).
    # 执行速度与定时器精度由本进程控制，且都不降速（高 QoS）。
    _state = _PowerThrottling(1, 0x1 | 0x4, 0)
    _kernel32.SetProcessInformation(_kernel32.GetCurrentProcess(), 4, ctypes.byref(_state), ctypes.sizeof(_state))
