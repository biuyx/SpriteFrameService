"""按动作名生成提示词模板（Seedance i2v，精灵帧源片向）。

规则来源：seedance-video-gen skill 的 prompt-guide（官方指南 + 实战验证）：
- 首帧锚定放**第一句**且措辞具体（identity 漂移的首要修复手段就是加强锚定）
- 背景用饱和绿幕 #00FF00（与九宫格 chroma-key 流程一致），绝不用白底——
  浅色角色会和背景融掉；角色配色偏绿时用户应改为品红 #FF00FF
- 禁一切视觉特效：半透明光效/粒子会沾染底色，抠像去不掉；
  技能特效属于游戏运行时的独立图层，不该烧进帧里
- 锁机位 + 固定脚底基线 + 四周留白：帧间对齐与裁切的前提
- 循环类动作要求首尾衔接；单次类（die/victory）保持结束姿势
"""
from __future__ import annotations

# 首句锚定（一致性核心，必须在最前）
_ANCHOR = (
    "以首帧图为视频第一帧，全程严格保持角色的身份、脸部、发型、"
    "服装轮廓与配色完全一致，不得改变角色设计。"
)

# 禁特效 + 绿幕 + 锁机位（精灵帧源片三铁律）
_SPRITE_RULES = (
    "动作只通过身体、四肢、头发与道具的姿态表现，画面中禁止出现任何特效："
    "无光效、无发光、无粒子、无光环、无能量、无烟雾、无灰尘、无拖影、"
    "无残影、无速度线。"
    "背景为单一饱和纯绿色（#00FF00）抠像幕布：完全平整均匀，无渐变、"
    "无阴影、无地面、无环境光，背景色不得映照或沾染到角色身上。"
    "镜头完全锁定（不推拉、不摇移、不变焦），角色全身居中，"
    "脚底基线固定不变，四周留出空白边距。"
)

_LOOP_TAIL = "动作流畅自然，首尾姿势衔接、可无缝循环播放。画面无文字无水印。"
_ONESHOT_TAIL = "动作流畅自然，完成后保持结束姿势。画面无文字无水印。"


def _loop(action_desc: str) -> str:
    return f"{_ANCHOR}{action_desc}{_SPRITE_RULES}{_LOOP_TAIL}"


def _oneshot(action_desc: str) -> str:
    return f"{_ANCHOR}{action_desc}{_SPRITE_RULES}{_ONESHOT_TAIL}"


# 动作模板（动作描述避免任何会诱发特效的措辞）
ACTION_TEMPLATES: dict[str, str] = {
    "idle":    _loop("角色原地待机，身体随呼吸轻微起伏，重心自然摆动。"),
    "walk":    _loop("角色原地走路（踏步式，不产生位移），双臂自然前后摆动，步伐节奏均匀。"),
    "run":     _loop("角色原地奔跑（踏步式，不产生位移），手臂大幅摆动，身体略前倾，节奏轻快。"),
    "attack":  _loop("角色向前挥出一次有力的徒手或持械攻击，仅用肢体与武器姿态表现力度，随后收势回到起始姿势。"),
    "hit":     _loop("角色受到打击，身体后仰晃动一下，随后恢复到起始姿势。"),
    "die":     _oneshot("角色受创无力地倒下，动作缓慢自然。"),
    "jump":    _loop("角色原地屈膝起跳再落地站稳，落地姿势与起跳前一致，脚底回到同一基线。"),
    "skill":   _loop("角色做施法动作：抬手蓄力后向前推出，仅用身体姿态与手势表现发力，随后收势回到起始姿势。"),
    "victory": _oneshot("角色做出胜利庆祝动作，欢快地举手雀跃。"),
    "sleep":   _loop("角色闭眼安睡，身体随呼吸缓慢起伏。"),
}

# 中文/常见别名 → 模板键
ALIASES: dict[str, str] = {
    "待机": "idle", "站立": "idle", "stand": "idle",
    "走路": "walk", "行走": "walk", "走": "walk",
    "跑": "run", "奔跑": "run", "跑步": "run",
    "攻击": "attack", "普攻": "attack",
    "受击": "hit", "受伤": "hit", "hurt": "hit",
    "死亡": "die", "倒下": "die", "death": "die",
    "跳": "jump", "跳跃": "jump",
    "技能": "skill", "施法": "skill", "cast": "skill",
    "胜利": "victory", "欢呼": "victory", "win": "victory",
    "睡觉": "sleep", "睡眠": "sleep",
}

# 未命中时的通用模板（{action} 会被替换为动作名）
GENERIC_TEMPLATE = _loop("角色做「{action}」动作。")

# 前端提示（随 capabilities 下发）
NOTES = (
    "模板使用绿幕背景（#00FF00），抽帧后用「背景处理 → 颜色过滤」抠图；"
    "若角色配色含绿色，请把模板中的绿色改为品红 #FF00FF。"
    "若角色形象仍漂移：换更清晰的全身参考图、简化动作描述，或换 Fast/Pro 模型。"
)


def template_payload() -> dict:
    """随 /generate/capabilities 下发给前端。"""
    return {
        "templates": ACTION_TEMPLATES,
        "aliases": ALIASES,
        "generic": GENERIC_TEMPLATE,
        "notes": NOTES,
    }
