"""Owner's five reversed devices: physical introductions separate from wire limits."""

from copy import deepcopy


HARDWARE = {
    "muse_ble": {
        "id": "muse", "name": "枕趣炮机 · Muse", "driver": "muse_ble",
        "description": "枕趣炮机：具有往复伸缩和震动两种动作。伸缩负责前后运动，震动是另一路，二者不是同一个动作的强弱变化。可以同时运行、交替或错峰，也可以停住伸缩而保留震动，或反过来。每路可切换设备自带的节奏模式；模式编号不代表由弱到强。设备的位置、固定方式和配件由手动调整，远程控制只改变动作，不会改变摆放或自动更换配件。",
        "capabilities": {
            "channels": {
                "vib_pattern": {"min": 0, "max": 9, "integer": True},
                "thrust_pattern": {"min": 0, "max": 9, "integer": True},
            },
            "min_step_ms": 5000, "max_step_ms": 60000, "max_cycle_ms": 600000,
        },
    },
    "sosexy_ble": {
        "id": "sosexy", "name": "FUNF 繁野 · 啵啵贝", "driver": "sosexy_ble",
        "description": "FUNF 繁野的啵啵贝，蓝牙名称为 SOSEXY。具有吮吸、震动和电流三个独立通道：吮吸产生吸放变化，震动产生机械振动，电流是单独的电刺激输出，不是更强的震动。各路都能分别改变预设节奏和输出大小，可叠加、交替、错峰，也可只停一路而保留其余动作。设备需要手动放置和调整接触位置；连接成功不代表已经放好或正在接触身体。",
        "capabilities": {
            "channels": {
                f"{motor}_{field}": {"min": 1 if field == "mode" else 0,
                                     "max": 4 if field == "mode" else 100, "integer": True}
                for motor in ("vibration", "electro", "suction") for field in ("mode", "speed")
            },
            "min_step_ms": 1500, "max_step_ms": 60000, "max_cycle_ms": 600000,
        },
    },
    "sk30_ble": {
        "id": "sk30", "name": "Cachito · 失控 3.0", "driver": "sk30_ble",
        "description": "Cachito 失控 3.0：吮吸端和入体端分别提供吮吸与脉冲动作。这里的脉冲是入体端的机械动作，不是电流，也不要当作普通震动。吮吸的输出大小和脉冲的快慢可以独立安排，可以同时、交替或错峰运行，也可单独停下一路。位置和接触方式需要手动调整；远程控制不会移动整台设备。",
        "capabilities": {
            "channels": {
                "suction_on": {"min": 0, "max": 1, "integer": True},
                "suction_level": {"min": 0, "max": 100, "integer": True},
                "pulse_gear": {"min": 0, "max": 100, "integer": True},
            },
            "min_step_ms": 1500, "max_step_ms": 60000, "max_cycle_ms": 600000,
        },
    },
    "sk40_ble": {
        "id": "sk40", "name": "Cachito · 失控 4.0", "driver": "sk40_ble",
        "description": "Cachito 失控 4.0：具有吮吸端和可更换的入体端，本次已接入的是吮吸与脉冲两路动作。吮吸调节吸放的输出，脉冲调节入体端机械动作的快慢，不是电刺激。两路能独立运行、叠加、交替或错峰，停止一路不会要求另一路一起停。更换入体端和调整位置需要手动完成，不能把可选配件当成本场已经装好的配件。",
        "capabilities": {
            "channels": {
                "suction_on": {"min": 0, "max": 1, "integer": True},
                "suction_level": {"min": 0, "max": 100, "integer": True},
                "pulse_gear": {"min": 0, "max": 100, "integer": True},
            },
            "min_step_ms": 1500, "max_step_ms": 60000, "max_cycle_ms": 600000,
        },
    },
    "cx492b_ble": {
        "id": "cx492b", "name": "礼物猫 · 野行 Pro", "driver": "cx492b_ble",
        "description": "礼物猫（BeYourLover）野行 Pro：吸震一体的穿戴式设备，本场通过 CX492B 连接。吮吸和震动是两个独立动作，各自可切换预设模式并调节输出大小。可以让一路保持节奏、另一路变化，或两路交替、叠加、错峰；也能只停其中一路。穿戴和位置调整需要手动完成，支持穿戴不代表本场已经穿戴。当前远程动作是吮吸和震动，不包含加热或灯光控制。",
        "capabilities": {
            "channels": {
                "vibration_mode": {"min": 1, "max": 11, "integer": True},
                "vibration_speed": {"min": 0, "max": 10, "integer": True},
                "suction_mode": {"min": 1, "max": 5, "integer": True},
                "suction_speed": {"min": 0, "max": 10, "integer": True},
            },
            "min_step_ms": 1500, "max_step_ms": 60000, "max_cycle_ms": 600000,
        },
    },
}

# Upgrade only shipped copy. Owner-written introductions and effective limits survive.
_OLD_INTROS = {
    "muse_ble": ("Tide · Muse", "Muse：震动与伸缩两个通道，可以在同一台设备上叠加、交替、错峰变化，也可只调整或停止其中一路，另一路继续。各通道使用预设模式。通过手机蓝牙广播控制，没有设备回执。模式编号不代表连续强度。"),
    "sosexy_ble": ("密语 · SOSEXY", "SOSEXY：沿用密语的蓝牙连接。同一台设备有震动、电流、吮吸三个通道，可叠加、交替、错峰变化；每路可独立调整模式和输出，也可单独停止而让其他通道继续。"),
}


def current_introduction(driver, name, description):
    if driver in _OLD_INTROS:
        old_name, old_intro = _OLD_INTROS[driver]
        if name == old_name:
            name = HARDWARE[driver]["name"]
        if description in (old_intro, old_intro + " 本次开放的取值范围以设备能力配置为准，保留多通道编排。"):
            description = HARDWARE[driver]["description"]
    return name, description


def compiler_capabilities(driver, capabilities):
    """Describe the effective ranges, never a second set of protocol maxima.

    Only transport-owned compiler metadata is regenerated. Owner-authored
    device introductions (used by Core) and mock-device facts remain intact.
    """
    result = deepcopy(capabilities)
    if driver not in HARDWARE:
        return result
    ranges = "；".join(
        f"{name}：整数 {bounds['min']}～{bounds['max']}"
        for name, bounds in result["channels"].items()
    )
    if driver == "muse_ble":
        meaning = ("每路的 0 是停止，非零值是离散预设模式，不是线性强度。"
                   "一次模式切换最多需要四条串行广播，每条约 1 秒。无物理执行回执。")
    elif driver in ("sk30_ble", "sk40_ble"):
        meaning = ("suction_on 是吮吸开关：0 停、1 开；suction_level 是原厂输出刻度，数值越大输出越高，"
                   "0 仍是最低运行输出，不是停止，必须用 suction_on=0 停吮吸。"
                   "pulse_gear=0 停脉冲；非零刻度越小越快，越大间隔越长、速度越慢，不是越强。"
                   "脉冲是机械动作，不是电流。两路独立，手机持续广播维持动作，无物理执行回执。")
    else:
        meaning = ("mode 是预设模式编号；停止某路时只将该路 speed 置 0，mode 仍须保持上述范围内的合法值。"
                   "speed 是协议输出数值，不等于实测物理强度。不得用模式名称推断未实测的波形。")
    result["description"] = (
        f"本次允许的字段范围：{ranges}。{meaning}"
        f"每步至少 {result['min_step_ms']} 毫秒。"
    )
    return result


def devices(*, bench=False, single_channel=False, drivers=None):
    result = deepcopy([profile for key, profile in HARDWARE.items() if drivers is None or key in drivers])
    if bench:
        # Bench setup is opt-in, never a silent change to ordinary device control.
        for device in result:
            channels = device["capabilities"]["channels"]
            driver = device["driver"]
            if driver == "muse_ble":
                channels["vib_pattern"]["max"] = 1 if single_channel else 3
                channels["thrust_pattern"]["max"] = 0 if single_channel else 3
            elif driver == "sosexy_ble":
                for motor in ("vibration", "electro", "suction"):
                    channels[f"{motor}_speed"]["max"] = 0 if single_channel and motor != "vibration" else 15
            elif driver == "cx492b_ble":
                channels["vibration_speed"]["max"] = 2
                channels["suction_speed"]["max"] = 0 if single_channel else 2
            else:
                channels["suction_level"]["max"] = 15
                # No linear-strength cap for reverse-direction pulse gear. In
                # vibration-only legacy pilots these devices are not selected.
                if single_channel:
                    channels["suction_on"]["max"] = channels["pulse_gear"]["max"] = 0
            device["description"] += (" 本次是桌面联调，仅开放震动的受限输出，其他动作尚未授权。" if single_channel else
                " 本次是多通道桌面联调，可使用全部已列明通道编排；输出取值仍受本场设备范围限制。")
    for device in result:
        device["capabilities"] = compiler_capabilities(device["driver"], device["capabilities"])
    return result


def validate_capabilities(driver, caps):
    """Owner may narrow ranges, but cannot invent channels or raise hardware limits."""
    reference = HARDWARE[driver]["capabilities"]
    channels = caps["channels"]
    if set(channels) != set(reference["channels"]):
        raise ValueError("scene_hardware_channels_invalid")
    for key, bounds in channels.items():
        actual = reference["channels"][key]
        if (bounds.get("integer") is not True or
                any(type(bounds.get(k)) is not int for k in ("min", "max")) or
                not actual["min"] <= bounds["min"] <= bounds["max"] <= actual["max"]):
            raise ValueError("scene_hardware_range_invalid")
        if actual["min"] == 0 and bounds["min"] != 0:
            raise ValueError("scene_hardware_stop_required")
    if (type(caps.get("min_step_ms")) is not int or
            not reference["min_step_ms"] <= caps["min_step_ms"] <= caps["max_step_ms"]):
        raise ValueError("scene_hardware_timing_invalid")
