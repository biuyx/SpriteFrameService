"""按动作名生成提示词模板（Seedance i2v，游戏精灵动作向）。

约束来自实际生产经验（video-src 历史请求）：
- 保持首帧角色形象一致（参考图是唯一的角色锚点）
- 纯色背景（便于后续抠图）、镜头固定（精灵图不允许运镜）
- 循环类动作要求首尾衔接；单次类动作（死亡/胜利）不做循环约束
"""
from __future__ import annotations

# 公共尾缀
_LOOP_TAIL = (
    "保持角色外观与首帧图完全一致，纯白色背景，镜头完全固定不动，"
    "角色全身居中显示，动作流畅自然，首尾姿势衔接、可无缝循环播放，"
    "画面中不出现文字与水印。"
)
_ONESHOT_TAIL = (
    "保持角色外观与首帧图完全一致，纯白色背景，镜头完全固定不动，"
    "角色全身居中显示，动作流畅自然，动作完成后保持结束姿势，"
    "画面中不出现文字与水印。"
)

# 动作模板：动作描述 + 对应尾缀
ACTION_TEMPLATES: dict[str, str] = {
    "idle":    "角色原地待机，身体随呼吸轻微起伏，重心自然摆动。" + _LOOP_TAIL,
    "walk":    "角色原地走路（踏步式），双臂自然前后摆动，步伐节奏均匀。" + _LOOP_TAIL,
    "run":     "角色原地奔跑（踏步式），手臂大幅摆动，身体略前倾，节奏轻快。" + _LOOP_TAIL,
    "attack":  "角色向前挥出一次有力的攻击，随后收势回到起始姿势。" + _LOOP_TAIL,
    "hit":     "角色受到打击，身体后仰晃动一下，随后恢复到起始姿势。" + _LOOP_TAIL,
    "die":     "角色受创无力地倒下，动作缓慢自然。" + _ONESHOT_TAIL,
    "jump":    "角色原地屈膝起跳再落地站稳，落地姿势与起跳前一致。" + _LOOP_TAIL,
    "skill":   "角色施展技能：抬手蓄力后释放，带轻微的身体发力感，随后收势回到起始姿势。" + _LOOP_TAIL,
    "victory": "角色做出胜利庆祝动作，欢快地举手雀跃。" + _ONESHOT_TAIL,
    "sleep":   "角色闭眼安睡，身体随呼吸缓慢起伏。" + _LOOP_TAIL,
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
GENERIC_TEMPLATE = "角色做「{action}」动作。" + _LOOP_TAIL


def template_payload() -> dict:
    """随 /generate/capabilities 下发给前端。"""
    return {
        "templates": ACTION_TEMPLATES,
        "aliases": ALIASES,
        "generic": GENERIC_TEMPLATE,
    }
