# -*- coding: utf-8 -*-
"""提示词组合注入器 (astrbot_plugin_prompt_injector)

支持在每次 AstrBot 请求 LLM 时，在以下 4 个位置注入一个或多个（数量可配置）提示词：
1. 人格设定前 (before_persona)
2. 人格设定后 (after_persona)
3. 用户最新输入前 (before_user)
4. 用户最新输入后 (after_user)

特性：
- 提供 WebUI 可视化拖拽调整编辑面板（views/editor），在排序流中直观呈现「人格设定」与「用户最新输入」两大锚点卡片。
- 同时兼容 AstrBot 原生 `_conf_schema.json` 的 `template_list` 配置面板，双向实时同步。
- 支持创建、编辑、复制、导入导出多个提示词组合（Preset），并可通过可视化面板或聊天指令自由选择切换。
- 支持设置每个位置允许注入的提示词最大数量（0 为不限制）。
"""

from __future__ import annotations

import copy
import datetime
import re
import uuid
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request as web_request
from astrbot.core.agent.message import TextPart

PLUGIN_NAME = "astrbot_plugin_prompt_injector"

POSITIONS = ("before_persona", "after_persona", "before_user", "after_user")
POSITION_LABELS = {
    "before_persona": "人格设定前",
    "after_persona": "人格设定后",
    "before_user": "用户最新输入前",
    "after_user": "用户最新输入后",
}
SCOPES = ("all", "group", "private")
WEEKDAY_CN = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")

# 匹配 AstrBot 核心注入的人格设定标题行
PERSONA_HEADER_RE = re.compile(r"(?:^|\n)# Persona Instructions\n+")
# 匹配可能存在的后续内置系统块（如 ## Skills, [Shipyard Neo, You are a helpful and friendly 等后置工具块）
POST_PERSONA_ANCHORS = (
    "\n## Skills\n",
    "\n[Tool Call",
    "\nWhen using tools",
    "\nLocal computer tools are available",
    "\n[Shipyard Neo",
)


def _new_id(prefix: str = "p") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


class PromptInjectorPlugin(Star):
    """支持多位置提示词排序注入与多组合切换的插件。"""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self._ensure_valid_config()
        self._register_web_apis()
        logger.info(f"[{PLUGIN_NAME}] 提示词组合注入器已加载")

    # ================================================================
    # Web API 注册与处理 (供 views/editor 可视化编排面板调用)
    # ================================================================

    def _register_web_apis(self) -> None:
        register_fn = getattr(self.context, "register_web_api", None)
        if not callable(register_fn):
            logger.warning(f"[{PLUGIN_NAME}] 当前 AstrBot 版本不支持 register_web_api，仅使用原生配置与指令。")
            return

        routes = (
            ("state", self.api_get_state, ["GET"], "获取所有提示词组合、全局配置与当前人格预览"),
            ("save", self.api_save_state, ["POST"], "保存提示词组合与全局设置"),
            ("preset/switch", self.api_switch_preset, ["POST"], "切换当前激活的提示词组合"),
            ("preset/save", self.api_save_preset, ["POST"], "保存或更新单个提示词组合"),
            ("preset/delete", self.api_delete_preset, ["POST"], "删除指定的提示词组合"),
            ("preview", self.api_preview_injection, ["POST"], "预览指定组合在请求时的实际注入结果"),
        )
        for path, handler, methods, desc in routes:
            try:
                register_fn(f"/{PLUGIN_NAME}/{path}", handler, methods, desc)
            except Exception as exc:
                logger.error(f"[{PLUGIN_NAME}] 注册 Web API /{PLUGIN_NAME}/{path} 失败: {exc}")

    async def api_get_state(self):
        """返回插件完整状态、所有提示词组合以及系统中的人格列表供可视化面板展示。"""
        try:
            self._ensure_valid_config()
            presets = self._get_normalized_presets()
            active_preset = self._resolve_active_preset(presets)
            personas_info = self._get_system_personas_preview()
            return json_response(
                {
                    "enabled": bool(self.config.get("enabled", True)),
                    "active_preset_name": active_preset["preset_name"] if active_preset else "",
                    "active_preset_id": active_preset["preset_id"] if active_preset else "",
                    "max_prompts_per_position": int(self.config.get("max_prompts_per_position", 10) or 0),
                    "separator": str(self.config.get("separator", "\\n\\n")),
                    "save_user_injected_to_history": bool(
                        self.config.get("save_user_injected_to_history", False)
                    ),
                    "presets": presets,
                    "personas": personas_info,
                    "variables_help": [
                        {"var": "{user_name}", "desc": "当前发送者昵称"},
                        {"var": "{user_id}", "desc": "当前发送者平台 ID"},
                        {"var": "{group_id}", "desc": "当前群号（私聊为空）"},
                        {"var": "{date}", "desc": "当前日期 (YYYY-MM-DD)"},
                        {"var": "{time}", "desc": "当前时间 (HH:MM:SS)"},
                        {"var": "{datetime}", "desc": "完整日期时间"},
                        {"var": "{weekday}", "desc": "当前星期几"},
                        {"var": "{umo}", "desc": "当前会话唯一标识 (UMO)"},
                    ],
                }
            )
        except Exception as exc:
            logger.exception(f"[{PLUGIN_NAME}] api_get_state 异常")
            return error_response(f"读取配置失败: {exc}", status_code=500)

    async def api_save_state(self):
        """整体保存全局设置与所有提示词组合。"""
        try:
            payload = await web_request.json(default={})
            if not isinstance(payload, dict):
                return error_response("请求体必须是 JSON 对象", status_code=400)

            updates: dict[str, Any] = {}

            if "enabled" in payload:
                updates["enabled"] = bool(payload["enabled"])

            if "max_prompts_per_position" in payload:
                try:
                    max_cnt = int(payload["max_prompts_per_position"])
                    updates["max_prompts_per_position"] = max(0, min(100, max_cnt))
                except (ValueError, TypeError):
                    return error_response("单位置最大提示词数量必须是整数", status_code=400)

            if "separator" in payload and isinstance(payload["separator"], str):
                updates["separator"] = payload["separator"]

            if "save_user_injected_to_history" in payload:
                updates["save_user_injected_to_history"] = bool(
                    payload["save_user_injected_to_history"]
                )

            if "presets" in payload:
                raw_presets = payload["presets"]
                if not isinstance(raw_presets, list) or not raw_presets:
                    return error_response("至少需要保留一个提示词组合", status_code=400)
                normalized_presets = [
                    self._normalize_single_preset(item, idx)
                    for idx, item in enumerate(raw_presets)
                    if isinstance(item, dict)
                ]
                if not normalized_presets:
                    return error_response("提示词组合数据格式无效", status_code=400)
                updates["presets_config"] = normalized_presets

            if "active_preset_name" in payload and isinstance(payload["active_preset_name"], str):
                updates["active_preset_name"] = payload["active_preset_name"].strip()

            self._persist_updates(updates)
            presets = self._get_normalized_presets()
            active = self._resolve_active_preset(presets)

            return json_response(
                {
                    "saved": True,
                    "enabled": bool(self.config.get("enabled", True)),
                    "active_preset_name": active["preset_name"] if active else "",
                    "active_preset_id": active["preset_id"] if active else "",
                    "max_prompts_per_position": int(self.config.get("max_prompts_per_position", 10) or 0),
                    "separator": str(self.config.get("separator", "\\n\\n")),
                    "save_user_injected_to_history": bool(
                        self.config.get("save_user_injected_to_history", False)
                    ),
                    "presets": presets,
                }
            )
        except Exception as exc:
            logger.exception(f"[{PLUGIN_NAME}] api_save_state 异常")
            return error_response(f"保存配置失败: {exc}", status_code=500)

    async def api_switch_preset(self):
        """快速切换当前激活的提示词组合。"""
        try:
            payload = await web_request.json(default={})
            target_id = str(payload.get("preset_id") or "").strip()
            target_name = str(payload.get("preset_name") or "").strip()

            presets = self._get_normalized_presets()
            matched = None
            for p in presets:
                if (target_id and p["preset_id"] == target_id) or (
                    target_name and p["preset_name"] == target_name
                ):
                    matched = p
                    break

            if not matched:
                return error_response("未找到指定的提示词组合", status_code=404)

            # 切换时若目标组合未启用，自动将其启用
            if not matched.get("enabled", True):
                matched["enabled"] = True

            self._persist_updates(
                {
                    "active_preset_name": matched["preset_name"],
                    "presets_config": presets,
                }
            )
            return json_response(
                {
                    "switched": True,
                    "active_preset_name": matched["preset_name"],
                    "active_preset_id": matched["preset_id"],
                    "presets": presets,
                }
            )
        except Exception as exc:
            logger.exception(f"[{PLUGIN_NAME}] api_switch_preset 异常")
            return error_response(f"切换组合失败: {exc}", status_code=500)

    async def api_save_preset(self):
        """新增或更新单个提示词组合。"""
        try:
            payload = await web_request.json(default={})
            preset_data = payload.get("preset")
            if not isinstance(preset_data, dict):
                return error_response("缺少有效的 preset 对象", status_code=400)

            presets = self._get_normalized_presets()
            norm = self._normalize_single_preset(preset_data, len(presets))

            replaced = False
            for idx, existing in enumerate(presets):
                if existing["preset_id"] == norm["preset_id"]:
                    presets[idx] = norm
                    replaced = True
                    break

            if not replaced:
                # 确保名称不冲突
                existing_names = {p["preset_name"] for p in presets}
                base_name = norm["preset_name"]
                suffix = 2
                while norm["preset_name"] in existing_names:
                    norm["preset_name"] = f"{base_name} ({suffix})"
                    suffix += 1
                presets.append(norm)

            updates: dict[str, Any] = {"presets_config": presets}
            if payload.get("set_active"):
                updates["active_preset_name"] = norm["preset_name"]

            self._persist_updates(updates)
            active = self._resolve_active_preset(presets)
            return json_response(
                {
                    "saved": True,
                    "preset": norm,
                    "active_preset_name": active["preset_name"] if active else "",
                    "active_preset_id": active["preset_id"] if active else "",
                    "presets": presets,
                }
            )
        except Exception as exc:
            logger.exception(f"[{PLUGIN_NAME}] api_save_preset 异常")
            return error_response(f"保存组合失败: {exc}", status_code=500)

    async def api_delete_preset(self):
        """删除指定的提示词组合。"""
        try:
            payload = await web_request.json(default={})
            target_id = str(payload.get("preset_id") or "").strip()
            target_name = str(payload.get("preset_name") or "").strip()

            presets = self._get_normalized_presets()
            if len(presets) <= 1:
                return error_response("至少需要保留一个提示词组合，无法删除最后一个组合", status_code=400)

            new_presets = [
                p
                for p in presets
                if not (
                    (target_id and p["preset_id"] == target_id)
                    or (target_name and p["preset_name"] == target_name)
                )
            ]
            if len(new_presets) == len(presets):
                return error_response("未找到要删除的提示词组合", status_code=404)

            active = self._resolve_active_preset(new_presets)
            self._persist_updates(
                {
                    "presets_config": new_presets,
                    "active_preset_name": active["preset_name"] if active else new_presets[0]["preset_name"],
                }
            )
            return json_response(
                {
                    "deleted": True,
                    "active_preset_name": active["preset_name"] if active else "",
                    "active_preset_id": active["preset_id"] if active else "",
                    "presets": new_presets,
                }
            )
        except Exception as exc:
            logger.exception(f"[{PLUGIN_NAME}] api_delete_preset 异常")
            return error_response(f"删除组合失败: {exc}", status_code=500)

    async def api_preview_injection(self):
        """根据前端传入的组合草稿、示例人格与示例用户输入，实时生成注入预览。"""
        try:
            payload = await web_request.json(default={})
            preset_raw = payload.get("preset")
            presets = self._get_normalized_presets()
            if isinstance(preset_raw, dict):
                preset = self._normalize_single_preset(preset_raw, 0)
            else:
                preset = self._resolve_active_preset(presets)

            if not preset:
                return error_response("没有可用的提示词组合", status_code=400)

            sample_persona = str(
                payload.get("sample_persona")
                or "你是一个温柔、博学且富有同理心的 AI 助手。"
            )
            sample_user_input = str(
                payload.get("sample_user_input")
                or "你好！请帮我介绍一下今天的计划。"
            )
            max_cnt = payload.get("max_prompts_per_position")
            if max_cnt is None:
                max_cnt = int(self.config.get("max_prompts_per_position", 10) or 0)
            else:
                max_cnt = max(0, int(max_cnt))

            sep = self._get_real_separator(payload.get("separator"))
            buckets = self._extract_position_buckets(preset, max_per_pos=max_cnt)

            now = datetime.datetime.now()
            sample_vars = {
                "user_name": "测试用户",
                "user_id": "10001",
                "group_id": "888888",
                "date": now.strftime("%Y-%m-%d"),
                "time": now.strftime("%H:%M:%S"),
                "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
                "weekday": WEEKDAY_CN[now.weekday()],
                "umo": "webchat:FriendMessage:preview_user",
            }

            def render_list(items: list[dict]) -> list[dict]:
                out = []
                for it in items:
                    rendered = self._render_template_vars(it["content"], sample_vars)
                    if rendered.strip():
                        out.append({"name": it["name"], "content": rendered.strip()})
                return out

            b_persona = render_list(buckets["before_persona"])
            a_persona = render_list(buckets["after_persona"])
            b_user = render_list(buckets["before_user"])
            a_user = render_list(buckets["after_user"])

            sys_parts = (
                [x["content"] for x in b_persona]
                + [f"# Persona Instructions\n\n{sample_persona}"]
                + [x["content"] for x in a_persona]
            )
            final_system_prompt = sep.join(p for p in sys_parts if p)

            user_parts = [x["content"] for x in b_user] + [sample_user_input]
            final_user_prompt = sep.join(p for p in user_parts if p)
            extra_user_parts = [x["content"] for x in a_user]

            return json_response(
                {
                    "preset_name": preset["preset_name"],
                    "counts": {
                        "before_persona": len(b_persona),
                        "after_persona": len(a_persona),
                        "before_user": len(b_user),
                        "after_user": len(a_user),
                    },
                    "buckets": {
                        "before_persona": b_persona,
                        "after_persona": a_persona,
                        "before_user": b_user,
                        "after_user": a_user,
                    },
                    "final_system_prompt": final_system_prompt,
                    "final_user_prompt": final_user_prompt,
                    "extra_user_content_parts": extra_user_parts,
                }
            )
        except Exception as exc:
            logger.exception(f"[{PLUGIN_NAME}] api_preview_injection 异常")
            return error_response(f"生成预览失败: {exc}", status_code=500)

    # ================================================================
    # LLM 请求注入钩子 (@filter.on_llm_request)
    # ================================================================

    @filter.on_llm_request(priority=100)
    async def on_llm_request_inject(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """在每次请求 LLM 时按当前激活的提示词组合注入 4 个位置的提示词。"""
        if not self.config.get("enabled", True):
            return

        try:
            presets = self._get_normalized_presets()
            preset = self._resolve_active_preset_for_event(presets, event)
            if not preset:
                return

            max_per_pos = int(self.config.get("max_prompts_per_position", 10) or 0)
            sep = self._get_real_separator()
            buckets = self._extract_position_buckets(preset, max_per_pos=max_per_pos)

            vars_map = self._build_event_vars(event)

            def render_texts(items: list[dict]) -> list[str]:
                res: list[str] = []
                for item in items:
                    raw_text = str(item.get("content") or "").strip()
                    if not raw_text:
                        continue
                    rendered = self._render_template_vars(raw_text, vars_map).strip()
                    if rendered:
                        res.append(rendered)
                return res

            before_persona_texts = render_texts(buckets["before_persona"])
            after_persona_texts = render_texts(buckets["after_persona"])
            before_user_texts = render_texts(buckets["before_user"])
            after_user_texts = render_texts(buckets["after_user"])

            if not (
                before_persona_texts
                or after_persona_texts
                or before_user_texts
                or after_user_texts
            ):
                return

            # 1 & 2. 注入「人格设定前」与「人格设定后」
            if before_persona_texts or after_persona_texts:
                req.system_prompt = self._inject_around_persona(
                    req.system_prompt or "",
                    before_persona_texts,
                    after_persona_texts,
                    sep,
                )

            # 3. 注入「用户最新输入前」
            if before_user_texts:
                prefix_block = sep.join(before_user_texts)
                current_prompt = req.prompt or ""
                if current_prompt.strip():
                    req.prompt = f"{prefix_block}{sep}{current_prompt}"
                else:
                    req.prompt = prefix_block

            # 4. 注入「用户最新输入后」
            if after_user_texts:
                save_to_history = bool(
                    self.config.get("save_user_injected_to_history", False)
                )
                if req.extra_user_content_parts is None:
                    req.extra_user_content_parts = []
                for text in after_user_texts:
                    part = TextPart(text=text)
                    if not save_to_history:
                        part.mark_as_temp()
                    req.extra_user_content_parts.append(part)

            logger.debug(
                f"[{PLUGIN_NAME}] 已应用组合「{preset['preset_name']}」: "
                f"人格前={len(before_persona_texts)}, 人格后={len(after_persona_texts)}, "
                f"输入前={len(before_user_texts)}, 输入后={len(after_user_texts)}"
            )
        except Exception as exc:
            logger.error(f"[{PLUGIN_NAME}] 注入提示词时发生异常: {exc}", exc_info=True)

    # ================================================================
    # 聊天管理命令 (/pi)
    # ================================================================

    @filter.command_group("pi", alias={"提示词注入", "prompt_injector"})
    def pi_group(self):
        """提示词组合注入器管理指令组"""
        pass

    @filter.permission_type(filter.PermissionType.ADMIN)
    @pi_group.command("list", alias={"列表", "ls"})
    async def cmd_list_presets(self, event: AstrMessageEvent):
        """查看所有提示词组合及当前激活状态"""
        presets = self._get_normalized_presets()
        active = self._resolve_active_preset(presets)
        active_id = active["preset_id"] if active else ""
        enabled_global = bool(self.config.get("enabled", True))
        max_cnt = int(self.config.get("max_prompts_per_position", 10) or 0)

        lines = [
            f"🎛️ 【提示词组合注入器】总开关：{'✅已启用' if enabled_global else '⏸️已关闭'}",
            f"🔢 单位置数量上限：{max_cnt if max_cnt > 0 else '不限制'} 条",
            f"📂 共有 {len(presets)} 个提示词组合：",
        ]
        for idx, p in enumerate(presets, start=1):
            is_curr = p["preset_id"] == active_id
            buckets = self._extract_position_buckets(p, max_per_pos=max_cnt)
            c_bp = len(buckets["before_persona"])
            c_ap = len(buckets["after_persona"])
            c_bu = len(buckets["before_user"])
            c_au = len(buckets["after_user"])
            mark = "👉 [当前生效]" if is_curr else ("✅ [启用]" if p["enabled"] else "⚪ [禁用]")
            scope_map = {"all": "全部", "group": "仅群聊", "private": "仅私聊"}
            scope_label = scope_map.get(p["scope"], "全部")
            lines.append(
                f"{idx}. {mark} {p['preset_name']} (范围:{scope_label})\n"
                f"   └ 人格前:{c_bp} | 人格后:{c_ap} | 输入前:{c_bu} | 输入后:{c_au}"
            )
        lines.append("\n💡 发送 `/pi switch <组合名称或序号>` 可自由切换组合，或在 WebUI 插件详情页打开「提示词编排面板」可视化拖拽排序。")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @pi_group.command("switch", alias={"切换", "use"})
    async def cmd_switch_preset(self, event: AstrMessageEvent, target: str):
        """切换当前生效的提示词组合（支持输入组合名称或序号）"""
        target = target.strip()
        presets = self._get_normalized_presets()
        matched = None

        if target.isdigit():
            idx = int(target) - 1
            if 0 <= idx < len(presets):
                matched = presets[idx]

        if matched is None:
            for p in presets:
                if p["preset_name"] == target or p["preset_id"] == target:
                    matched = p
                    break

        if matched is None:
            for p in presets:
                if target.lower() in p["preset_name"].lower():
                    matched = p
                    break

        if matched is None:
            names = "、".join(f"{i + 1}.{p['preset_name']}" for i, p in enumerate(presets))
            yield event.plain_result(f"❌ 未找到名为「{target}」的提示词组合。\n可选组合：{names}")
            return

        matched["enabled"] = True
        self._persist_updates(
            {
                "active_preset_name": matched["preset_name"],
                "presets_config": presets,
            }
        )
        buckets = self._extract_position_buckets(
            matched,
            max_per_pos=int(self.config.get("max_prompts_per_position", 10) or 0),
        )
        yield event.plain_result(
            f"✅ 已切换至提示词组合：【{matched['preset_name']}】\n"
            f"当前生效提示词：人格设定前 {len(buckets['before_persona'])} 条、"
            f"人格设定后 {len(buckets['after_persona'])} 条、"
            f"用户输入前 {len(buckets['before_user'])} 条、"
            f"用户输入后 {len(buckets['after_user'])} 条。"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @pi_group.command("detail", alias={"详情", "info"})
    async def cmd_preset_detail(self, event: AstrMessageEvent, target: str = ""):
        """查看当前或指定提示词组合的完整排序流（含人格设定和用户最新输入锚点）"""
        presets = self._get_normalized_presets()
        matched = None
        target = target.strip()
        if not target:
            matched = self._resolve_active_preset(presets)
        else:
            if target.isdigit():
                idx = int(target) - 1
                if 0 <= idx < len(presets):
                    matched = presets[idx]
            if matched is None:
                for p in presets:
                    if p["preset_name"] == target or p["preset_id"] == target:
                        matched = p
                        break

        if not matched:
            yield event.plain_result("❌ 未找到对应的提示词组合。")
            return

        max_cnt = int(self.config.get("max_prompts_per_position", 10) or 0)
        ordered_flow = self._build_visual_flow(matched, max_per_pos=max_cnt)
        lines = [
            f"📋 提示词组合【{matched['preset_name']}】完整注入顺序流：",
            "────────────────────",
        ]
        for idx, node in enumerate(ordered_flow, start=1):
            if node["type"] == "anchor_persona":
                lines.append(f"{idx}. 🔒 【系统锚点：人格设定 (Persona)】")
            elif node["type"] == "anchor_user_input":
                lines.append(f"{idx}. 💬 【系统锚点：用户最新输入 (Latest User Input)】")
            else:
                status = "✅" if node["enabled"] else "⚪(已禁用)"
                pos_label = POSITION_LABELS.get(node["effective_position"], node["effective_position"])
                preview = node["content"].replace("\n", " ")
                if len(preview) > 40:
                    preview = preview[:40] + "..."
                limit_tag = " [超出单位置数量上限，未生效]" if node.get("exceeded_limit") else ""
                lines.append(
                    f"{idx}. {status} [{pos_label}] {node['name']}{limit_tag}\n"
                    f"   └ {preview or '(内容为空)'}"
                )
        lines.append("────────────────────")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @pi_group.command("toggle", alias={"开关"})
    async def cmd_toggle_plugin(self, event: AstrMessageEvent):
        """开启或关闭提示词注入器总开关"""
        curr = bool(self.config.get("enabled", True))
        new_state = not curr
        self._persist_updates({"enabled": new_state})
        yield event.plain_result(
            f"🎛️ 提示词组合注入器已{'✅ 开启' if new_state else '⏸️ 关闭'}。"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @pi_group.command("limit", alias={"数量限制"})
    async def cmd_set_limit(self, event: AstrMessageEvent, count: int):
        """设置单位置最多可注入的提示词数量（0表示不限制）"""
        count = max(0, min(100, int(count)))
        self._persist_updates({"max_prompts_per_position": count})
        desc = f"{count} 条" if count > 0 else "不限制"
        yield event.plain_result(f"🔢 已将每个位置的最大提示词注入数量设置为：{desc}。")

    # ================================================================
    # 核心注入逻辑与排序流解析
    # ================================================================

    @staticmethod
    def _inject_around_persona(
        system_prompt: str,
        before_persona: list[str],
        after_persona: list[str],
        sep: str,
    ) -> str:
        """在 system_prompt 中的人格设定前后精准注入提示词。

        AstrBot 核心在 `_ensure_persona_and_skills` 中会通过
        `\\n# Persona Instructions\\n\\n{prompt}\\n` 注入人格设定，
        随后可能继续附加 `## Skills` 或工具调用提示。
        - 如果找到了 `# Persona Instructions`，则在其紧前方注入 `before_persona`，
          在人格设定正文结束处（下一个内置锚点如 `## Skills` 之前，或末尾）注入 `after_persona`。
        - 如果当前会话未配置人格或使用无标题的默认提示词，则将 `system_prompt` 整体视作人格设定区，
          `before_persona` 置于最前，`after_persona` 置于末尾。
        """
        before_block = sep.join(before_persona) if before_persona else ""
        after_block = sep.join(after_persona) if after_persona else ""

        if not system_prompt.strip():
            parts = [p for p in (before_block, after_block) if p]
            return sep.join(parts)

        match = PERSONA_HEADER_RE.search(system_prompt)
        if not match:
            parts = [p for p in (before_block, system_prompt.strip(), after_block) if p]
            return sep.join(parts)

        # 找到 `# Persona Instructions` 的起始位置
        header_start = match.start()
        if system_prompt[header_start] == "\n":
            header_start += 1

        prefix_sys = system_prompt[:header_start].strip()
        from_persona = system_prompt[header_start:]

        # 在 `# Persona Instructions` 之后查找是否有其他内置后置区块（如 ## Skills 等）
        persona_end_offset = len(from_persona)
        for anchor in POST_PERSONA_ANCHORS:
            idx = from_persona.find(anchor)
            if idx != -1 and idx < persona_end_offset:
                persona_end_offset = idx

        persona_segment = from_persona[:persona_end_offset].strip()
        suffix_sys = from_persona[persona_end_offset:].strip()

        assembled: list[str] = []
        if prefix_sys:
            assembled.append(prefix_sys)
        if before_block:
            assembled.append(before_block)
        if persona_segment:
            assembled.append(persona_segment)
        if after_block:
            assembled.append(after_block)
        if suffix_sys:
            assembled.append(suffix_sys)

        return sep.join(assembled)

    def _extract_position_buckets(
        self, preset: dict, max_per_pos: int = 0
    ) -> dict[str, list[dict]]:
        """从组合的 sequence（包含 anchor_persona 与 anchor_user_input 的有序序列）中解析出 4 个位置的启用提示词列表。"""
        flow = self._build_visual_flow(preset, max_per_pos=max_per_pos)
        buckets: dict[str, list[dict]] = {pos: [] for pos in POSITIONS}
        for node in flow:
            if node["type"] != "prompt_item":
                continue
            if not node.get("enabled", True):
                continue
            if not str(node.get("content") or "").strip():
                continue
            if node.get("exceeded_limit"):
                continue
            pos = node["effective_position"]
            if pos in buckets:
                buckets[pos].append(node)
        return buckets

    def _build_visual_flow(
        self, preset: dict, max_per_pos: int = 0
    ) -> list[dict[str, Any]]:
        """将组合内的 sequence 转换为带有精确有效位置（effective_position）和数量限制标记的完整有序节点列表。

        排序判定规则：
        1. 序列中天然包含两个锚点：`anchor_persona`（人格设定）和 `anchor_user_input`（用户最新输入）。
        2. 排在 `anchor_persona` 之前的提示词，位置为 `before_persona`（人格设定前）。
        3. 排在 `anchor_user_input` 之后的提示词，位置为 `after_user`（用户最新输入后）。
        4. 排在 `anchor_persona` 与 `anchor_user_input` 之间的提示词：
           - 若条目的 `position_override` 设为 `before_user`，则为 `before_user`（用户最新输入前）；
           - 否则默认为 `after_persona`（人格设定后）。
        5. 若条目显式设置了 `position_override` 为四个固定位置之一，在位于对应锚点区间时优先遵循该设定；
           在可视化面板中拖拽跨越锚点时，前端会自动更新 `position_override` 与序列位置保持一致。
        """
        seq = preset.get("sequence")
        if not isinstance(seq, list):
            seq = []

        # 查找两个锚点的索引
        persona_idx = -1
        user_idx = -1
        for i, item in enumerate(seq):
            if not isinstance(item, dict):
                continue
            tkey = item.get("__template_key") or item.get("type")
            if tkey == "anchor_persona" and persona_idx == -1:
                persona_idx = i
            elif tkey == "anchor_user_input" and user_idx == -1:
                user_idx = i

        # 如果锚点缺失或顺序颠倒，先规范化序列
        if persona_idx == -1 or user_idx == -1 or persona_idx > user_idx:
            seq = self._ensure_anchors_in_sequence(seq)
            for i, item in enumerate(seq):
                tkey = item.get("__template_key") or item.get("type")
                if tkey == "anchor_persona" and persona_idx == -1:
                    persona_idx = i
                elif tkey == "anchor_user_input" and user_idx == -1:
                    user_idx = i

        pos_counters: dict[str, int] = {pos: 0 for pos in POSITIONS}
        flow_nodes: list[dict[str, Any]] = []

        for i, item in enumerate(seq):
            if not isinstance(item, dict):
                continue
            tkey = item.get("__template_key") or item.get("type") or "prompt_item"
            if tkey == "anchor_persona":
                flow_nodes.append(
                    {
                        "type": "anchor_persona",
                        "__template_key": "anchor_persona",
                        "id": "anchor_persona",
                        "name": "人格设定 (Persona)",
                        "note": str(
                            item.get("note")
                            or "【系统锚点】此处为 AstrBot 的人格设定（Persona）"
                        ),
                    }
                )
                continue
            if tkey == "anchor_user_input":
                flow_nodes.append(
                    {
                        "type": "anchor_user_input",
                        "__template_key": "anchor_user_input",
                        "id": "anchor_user_input",
                        "name": "用户最新输入 (Latest User Input)",
                        "note": str(
                            item.get("note")
                            or "【系统锚点】此处为用户的最新输入（User Input）"
                        ),
                    }
                )
                continue

            # 普通提示词条目
            override = str(item.get("position_override") or "auto").strip()
            if i < persona_idx:
                effective_pos = "before_persona"
            elif i > user_idx:
                effective_pos = "after_user"
            else:
                # 位于人格设定与用户最新输入之间
                if override == "before_user":
                    effective_pos = "before_user"
                else:
                    effective_pos = "after_persona"

            # 如果用户在原生表单中没有移动锚点但显式指定了强制位置，也予以支持
            if override in POSITIONS:
                if (
                    (override == "before_persona" and i < persona_idx)
                    or (override in ("after_persona", "before_user") and persona_idx < i < user_idx)
                    or (override == "after_user" and i > user_idx)
                ):
                    effective_pos = override

            enabled = bool(item.get("enabled", True))
            content = str(item.get("content") or "")
            exceeded = False
            if enabled and content.strip():
                pos_counters[effective_pos] += 1
                if max_per_pos > 0 and pos_counters[effective_pos] > max_per_pos:
                    exceeded = True

            flow_nodes.append(
                {
                    "type": "prompt_item",
                    "__template_key": "prompt_item",
                    "id": str(item.get("id") or _new_id("prompt")),
                    "name": str(item.get("name") or "未命名提示词").strip() or "未命名提示词",
                    "enabled": enabled,
                    "position_override": override if override in ("auto", *POSITIONS) else "auto",
                    "effective_position": effective_pos,
                    "content": content,
                    "exceeded_limit": exceeded,
                }
            )

        return flow_nodes

    # ================================================================
    # 配置规范化与持久化辅助方法
    # ================================================================

    def _ensure_valid_config(self) -> None:
        """确保配置文件中的 presets_config 结构完整且包含默认锚点。"""
        raw_presets = self.config.get("presets_config")
        if not isinstance(raw_presets, list) or not raw_presets:
            default_presets = [self._create_default_preset()]
            self._persist_updates({"presets_config": default_presets})
            return

        normalized = [
            self._normalize_single_preset(item, idx)
            for idx, item in enumerate(raw_presets)
            if isinstance(item, dict)
        ]
        if not normalized:
            normalized = [self._create_default_preset()]

        if normalized != raw_presets:
            self._persist_updates({"presets_config": normalized})

    def _get_normalized_presets(self) -> list[dict[str, Any]]:
        raw_presets = self.config.get("presets_config")
        if not isinstance(raw_presets, list) or not raw_presets:
            return [self._create_default_preset()]
        res = [
            self._normalize_single_preset(item, idx)
            for idx, item in enumerate(raw_presets)
            if isinstance(item, dict)
        ]
        return res or [self._create_default_preset()]

    def _normalize_single_preset(self, raw: dict[str, Any], idx: int) -> dict[str, Any]:
        preset_id = str(raw.get("preset_id") or "").strip() or _new_id("preset")
        preset_name = (
            str(raw.get("preset_name") or "").strip() or f"提示词组合 {idx + 1}"
        )
        enabled = bool(raw.get("enabled", True))
        scope = str(raw.get("scope") or "all").strip()
        if scope not in SCOPES:
            scope = "all"
        description = str(raw.get("description") or "").strip()

        raw_seq = raw.get("sequence")
        if not isinstance(raw_seq, list):
            raw_seq = []
        sequence = self._ensure_anchors_in_sequence(raw_seq)

        return {
            "__template_key": "preset_group",
            "preset_id": preset_id,
            "preset_name": preset_name,
            "enabled": enabled,
            "scope": scope,
            "description": description,
            "sequence": sequence,
        }

    def _ensure_anchors_in_sequence(
        self, raw_seq: list[Any]
    ) -> list[dict[str, Any]]:
        """确保序列中有且仅有一个 `anchor_persona` 和一个 `anchor_user_input`，且 `anchor_persona` 在 `anchor_user_input` 之前。

        同时确保位于两个锚点之间的提示词，所有 `after_persona` 排在 `before_user` 之前。
        """
        before_persona_items: list[dict[str, Any]] = []
        after_persona_items: list[dict[str, Any]] = []
        before_user_items: list[dict[str, Any]] = []
        after_user_items: list[dict[str, Any]] = []

        anchor_persona_node = {
            "__template_key": "anchor_persona",
            "note": "【系统锚点】此处为 AstrBot 的人格设定（Persona）。排在此锚点上方的提示词注入在「人格设定前」，下方的提示词注入在「人格设定后」。",
        }
        anchor_user_node = {
            "__template_key": "anchor_user_input",
            "note": "【系统锚点】此处为「用户最新输入」。排在此锚点上方（且在人格设定锚点下方）的提示词可注入在「用户最新输入前」，排在此锚点下方的提示词注入在「用户最新输入后」。",
        }

        seen_persona = False
        seen_user = False
        seen_ids: set[str] = set()

        for item in raw_seq:
            if not isinstance(item, dict):
                continue
            tkey = item.get("__template_key") or item.get("type") or "prompt_item"
            if tkey == "anchor_persona":
                if not seen_persona:
                    seen_persona = True
                    if item.get("note"):
                        anchor_persona_node["note"] = str(item["note"])
                continue
            if tkey == "anchor_user_input":
                if not seen_user:
                    seen_user = True
                    if item.get("note"):
                        anchor_user_node["note"] = str(item["note"])
                continue

            item_id = str(item.get("id") or "").strip()
            if not item_id or item_id in seen_ids:
                item_id = _new_id("p")
            seen_ids.add(item_id)

            override = str(item.get("position_override") or "auto").strip()
            if override not in ("auto", *POSITIONS):
                override = "auto"

            clean_item = {
                "__template_key": "prompt_item",
                "id": item_id,
                "name": str(item.get("name") or "未命名提示词").strip() or "未命名提示词",
                "enabled": bool(item.get("enabled", True)),
                "position_override": override,
                "content": str(item.get("content") or ""),
            }

            # 判断该条目归属哪个区间
            if override == "before_persona":
                before_persona_items.append(clean_item)
            elif override == "after_persona":
                after_persona_items.append(clean_item)
            elif override == "before_user":
                before_user_items.append(clean_item)
            elif override == "after_user":
                after_user_items.append(clean_item)
            else:
                # auto: 根据当前相对于锚点的位置决定
                if not seen_persona and not seen_user:
                    before_persona_items.append(clean_item)
                elif seen_persona and not seen_user:
                    after_persona_items.append(clean_item)
                else:
                    after_user_items.append(clean_item)

        return (
            before_persona_items
            + [anchor_persona_node]
            + after_persona_items
            + before_user_items
            + [anchor_user_node]
            + after_user_items
        )

    def _create_default_preset(self) -> dict[str, Any]:
        return self._normalize_single_preset(
            {
                "preset_id": "default",
                "preset_name": "默认提示词组合",
                "enabled": True,
                "scope": "all",
                "description": "通用默认提示词注入组合",
                "sequence": [
                    {
                        "__template_key": "prompt_item",
                        "id": "p_pre_persona_1",
                        "name": "全局前置准则",
                        "enabled": True,
                        "position_override": "before_persona",
                        "content": "请始终保持自然、沉浸式的对话体验，严格遵循接下来的角色设定与上下文逻辑。",
                    },
                    {
                        "__template_key": "anchor_persona",
                        "note": "【系统锚点】此处为 AstrBot 的人格设定（Persona）。排在此锚点上方的提示词注入在「人格设定前」，下方的提示词注入在「人格设定后」。",
                    },
                    {
                        "__template_key": "prompt_item",
                        "id": "p_post_persona_1",
                        "name": "补充背景与文风要求",
                        "enabled": True,
                        "position_override": "after_persona",
                        "content": "回复时注重语言简练与情感表达，避免机械式说教。",
                    },
                    {
                        "__template_key": "prompt_item",
                        "id": "p_pre_user_1",
                        "name": "用户输入前置引导",
                        "enabled": False,
                        "position_override": "before_user",
                        "content": "[以下是用户的最新输入，请结合上下文认真回应]",
                    },
                    {
                        "__template_key": "anchor_user_input",
                        "note": "【系统锚点】此处为「用户最新输入」。排在此锚点上方（且在人格设定锚点下方）的提示词可注入在「用户最新输入前」，排在此锚点下方的提示词注入在「用户最新输入后」。",
                    },
                    {
                        "__template_key": "prompt_item",
                        "id": "p_post_user_1",
                        "name": "用户输入后置提醒",
                        "enabled": False,
                        "position_override": "after_user",
                        "content": "<system_hint>当前时间：{date} {time}，发送者：{user_name}</system_hint>",
                    },
                ],
            },
            0,
        )

    def _resolve_active_preset(
        self, presets: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        if not presets:
            return None
        active_name = str(self.config.get("active_preset_name") or "").strip()
        if active_name:
            for p in presets:
                if (
                    p["preset_name"] == active_name or p["preset_id"] == active_name
                ) and p.get("enabled", True):
                    return p
            for p in presets:
                if p["preset_name"] == active_name or p["preset_id"] == active_name:
                    return p
        for p in presets:
            if p.get("enabled", True):
                return p
        return presets[0]

    def _resolve_active_preset_for_event(
        self, presets: list[dict[str, Any]], event: AstrMessageEvent
    ) -> dict[str, Any] | None:
        """解析当前消息事件应使用的提示词组合（校验启用状态和群聊/私聊范围）。"""
        umo = getattr(event, "unified_msg_origin", "") or ""
        is_group = "GroupMessage" in umo or bool(getattr(event.message_obj, "group_id", ""))

        def scope_matches(p: dict[str, Any]) -> bool:
            sc = p.get("scope", "all")
            if sc == "group":
                return is_group
            if sc == "private":
                return not is_group
            return True

        active_name = str(self.config.get("active_preset_name") or "").strip()
        if active_name:
            for p in presets:
                if (
                    (p["preset_name"] == active_name or p["preset_id"] == active_name)
                    and p.get("enabled", True)
                    and scope_matches(p)
                ):
                    return p

        # 若当前激活组合不匹配当前会话范围（例如激活了仅群聊组合，但当前是私聊），则回退到第一个启用且匹配范围的组合
        for p in presets:
            if p.get("enabled", True) and scope_matches(p):
                return p
        return None

    def _persist_updates(self, updates: dict[str, Any]) -> None:
        """将更新写入 self.config 并持久化到磁盘。"""
        for k, v in updates.items():
            self.config[k] = copy.deepcopy(v)
        try:
            self.config.save_config(updates)
        except TypeError:
            self.config.save_config()
        except Exception as exc:
            logger.error(f"[{PLUGIN_NAME}] 保存配置文件失败: {exc}")

    def _get_real_separator(self, raw_sep: Any = None) -> str:
        if raw_sep is None:
            raw_sep = self.config.get("separator", "\\n\\n")
        s = str(raw_sep)
        return s.replace("\\n", "\n").replace("\\t", "\t")

    @staticmethod
    def _build_event_vars(event: AstrMessageEvent) -> dict[str, str]:
        now = datetime.datetime.now()
        sender = getattr(event.message_obj, "sender", None)
        user_name = (
            getattr(sender, "nickname", "")
            or event.get_sender_name()
            or "User"
        )
        user_id = (
            getattr(sender, "user_id", "")
            or event.get_sender_id()
            or ""
        )
        group_id = getattr(event.message_obj, "group_id", "") or ""
        umo = getattr(event, "unified_msg_origin", "") or ""
        return {
            "user_name": str(user_name),
            "user_id": str(user_id),
            "group_id": str(group_id),
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M:%S"),
            "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
            "weekday": WEEKDAY_CN[now.weekday()],
            "umo": str(umo),
        }

    @staticmethod
    def _render_template_vars(text: str, vars_map: dict[str, str]) -> str:
        out = text
        for k, v in vars_map.items():
            placeholder = "{" + k + "}"
            if placeholder in out:
                out = out.replace(placeholder, v)
        return out

    def _get_system_personas_preview(self) -> list[dict[str, str]]:
        """获取系统中已配置的人格设定列表，方便用户在可视化面板中实时预览不同人格下的排序效果。"""
        result: list[dict[str, str]] = []
        try:
            pm = getattr(self.context, "persona_manager", None)
            if pm is None:
                return result
            personas_v3 = getattr(pm, "personas_v3", None)
            if isinstance(personas_v3, list):
                for p in personas_v3:
                    if isinstance(p, dict):
                        name = str(p.get("name") or "default")
                        prompt = str(p.get("prompt") or "")
                        result.append({"name": name, "prompt": prompt[:500]})
        except Exception as exc:
            logger.debug(f"[{PLUGIN_NAME}] 获取系统人格列表失败: {exc}")
        return result
