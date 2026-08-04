# -*- coding: utf-8 -*-
"""
飞书折叠面板卡片构建模块（Fork 自定义扩展，低冲突架构）

设计目标
========
将原本一条超长 lark_md 文本消息，改为一张交互卡片：顶部总览 +
每只股票一个 ``collapsible_panel``（默认折叠，点击展开看明细）。
解决多股票刷屏、阅读友好度差的问题。

实测确认的可用结构（飞书自定义机器人 webhook）
================================================
- 必须用 ``schema: "2.0"`` + ``body.elements``（顶层 elements 会被拒）
- 折叠组件 ``collapsible_panel``：字段为 ``header.title`` + ``elements``，
  可选 ``expanded``(默认 false)、``border``
- 多个 panel 互相独立，可同时展开多个
- 支持的基础元素：div(lark_md)、hr、note、action+button、column_set
- 不支持：collapsible_group、divider、background_style、顶层 elements

低冲突要点
==========
本模块是**新增文件**，不依赖被上游频繁改动的 notification.py / pipeline.py 主体逻辑。
它复用 NotificationService 上已有的渲染 helper（``_get_display_name`` /
``_get_signal_level`` / ``_append_*`` 等），保证明细与现有 markdown 报告一致。

开关
====
环境变量 ``FEISHU_COLLAPSIBLE_CARD=true`` 开启（由 feishu_sender 读取）。
``FEISHU_COLLAPSIBLE_MAX_ITEMS`` 控制单卡最多股票数（超出则分多张卡发送，
飞书单卡 JSON 上限约 30KB），默认 15。
两者均**不触碰** config.py。
"""
import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from src.formatters import format_feishu_markdown

logger = logging.getLogger(__name__)

# 单张飞书卡片 JSON 体量软上限（经验值，留余量）。超出按股票数分多张卡。
_CARD_MAX_BYTES = 28000

# 每张卡片默认最多容纳的股票折叠面板数（可通过环境变量覆盖）。
_DEFAULT_MAX_ITEMS = 15


def _env_bool(name: str) -> bool:
    return (os.getenv(name, "") or "").strip().lower() in ("true", "1", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        raw = (os.getenv(name, "") or "").strip()
        if not raw:
            return default
        value = int(raw)
        return value if value > 0 else default
    except (TypeError, ValueError):
        logger.warning("环境变量 %s 值非法，回退默认 %d", name, default)
        return default


def is_collapsible_enabled() -> bool:
    """折叠卡片功能是否开启。"""
    return _env_bool("FEISHU_COLLAPSIBLE_CARD")


def _max_items_per_card() -> int:
    return _env_int("FEISHU_COLLAPSIBLE_MAX_ITEMS", _DEFAULT_MAX_ITEMS)


# ---------------------------------------------------------------------------
# 明细渲染：复用 NotificationService 的 helper，保证与 markdown 报告一致
# ---------------------------------------------------------------------------

def _render_stock_detail_lines(notifier: Any, result: Any, labels: Dict[str, str],
                               report_language: str) -> List[str]:
    """
    渲染单只股票的明细 markdown 行。

    复刻 ``generate_dashboard_report`` 中每股详情的主体结构
    （舆情/核心结论/数据透视/作战计划/信号归因/多策略/财务摘要 等），
    通过调用 notifier 已有的 ``_append_*`` helper 实现，避免逻辑漂移。
    """
    from src.report_language import (
        get_chip_unavailable_reason,
        is_chip_structure_unavailable,
        localize_chip_health,
        localize_trend_prediction,
        normalize_strategy_synthesis_payload,
    )
    from src.utils.data_processing import (
        signal_attribution_has_content,
        signal_attribution_weight_items,
    )
    from src import notification as _notif_module

    dashboard = getattr(result, "dashboard", None) or {}
    lines: List[str] = []

    signal_text, signal_emoji, _ = notifier._get_signal_level(result)

    # 舆情与基本面概览
    intel = dashboard.get("intelligence", {}) if dashboard else {}
    if intel:
        if intel.get("sentiment_summary"):
            lines.append(f"💭 {labels.get('sentiment_summary_label', '舆情')}: {intel['sentiment_summary']}")
        if intel.get("earnings_outlook"):
            lines.append(f"📊 {labels.get('earnings_outlook_label', '业绩预期')}: {intel['earnings_outlook']}")
        risk_alerts = intel.get("risk_alerts", [])
        if risk_alerts:
            lines.append(f"🚨 {labels.get('risk_alerts_label', '风险警报')}:")
            for alert in risk_alerts:
                lines.append(f"- {alert}")
        catalysts = intel.get("positive_catalysts", [])
        if catalysts:
            lines.append(f"✨ {labels.get('positive_catalysts_label', '利好催化')}:")
            for cat in catalysts:
                lines.append(f"- {cat}")
        if intel.get("latest_news"):
            lines.append(f"📢 {labels.get('latest_news_label', '最新消息')}: {intel['latest_news']}")
        lines.append("")

    # 核心结论
    core = dashboard.get("core_conclusion", {}) if dashboard else {}
    one_sentence = core.get("one_sentence", getattr(result, "analysis_summary", ""))
    if one_sentence:
        lines.append(f"📌 {labels.get('one_sentence_label', '一句话结论')}: {one_sentence}")
    lines.append(
        f"**{signal_emoji} {signal_text}** | "
        f"{localize_trend_prediction(result.trend_prediction, report_language)}"
    )
    pos_advice = core.get("position_advice", {})
    if pos_advice:
        lines.append(
            f"💼 {labels.get('no_position_label', '空仓')}: "
            f"{pos_advice.get('no_position', notifier._get_display_operation_advice(result, report_language))} | "
            f"{labels.get('has_position_label', '持仓')}: "
            f"{pos_advice.get('has_position', labels.get('continue_holding', '继续持有'))}"
        )
    lines.append("")

    # 数据透视
    data_persp = dashboard.get("data_perspective", {}) if dashboard else {}
    if data_persp:
        trend_data = data_persp.get("trend_status", {}) or {}
        price_data = data_persp.get("price_position", {}) or {}
        vol_data = data_persp.get("volume_analysis", {}) or {}
        chip_data = data_persp.get("chip_structure", {}) or {}

        if trend_data:
            lines.append(
                f"📈 {labels.get('ma_alignment_label', '均线排列')}: "
                f"{trend_data.get('ma_alignment', 'N/A')} | "
                f"{labels.get('trend_strength_label', '趋势强度')}: "
                f"{trend_data.get('trend_score', 'N/A')}/100"
            )
        if price_data:
            lines.append(
                f"💲 {labels.get('current_price_label', '现价')}: "
                f"{price_data.get('current_price', 'N/A')} | "
                f"{labels.get('ma5_label', 'MA5')}: {price_data.get('ma5', 'N/A')} | "
                f"{labels.get('ma20_label', 'MA20')}: {price_data.get('ma20', 'N/A')}"
            )
        if vol_data:
            lines.append(
                f"📊 {labels.get('volume_label', '量能')}: "
                f"{labels.get('volume_ratio_label', '量比')} "
                f"{vol_data.get('volume_ratio', 'N/A')} "
                f"({vol_data.get('volume_status', '')})"
            )
        if chip_data:
            if is_chip_structure_unavailable(chip_data):
                lines.append(
                    f"🧱 {labels.get('chip_label', '筹码')}: "
                    f"{get_chip_unavailable_reason(chip_data, report_language)}"
                )
            else:
                chip_health = localize_chip_health(
                    chip_data.get("chip_health", "N/A"), report_language
                )
                lines.append(
                    f"🧱 {labels.get('chip_label', '筹码')}: "
                    f"{chip_data.get('profit_ratio', 'N/A')} | "
                    f"{chip_data.get('avg_cost', 'N/A')} | "
                    f"{chip_data.get('concentration', 'N/A')} {chip_health}"
                )
        lines.append("")

    # 阶段决策块
    notifier._append_phase_decision_block(lines, dashboard, labels)

    # 作战计划
    battle = dashboard.get("battle_plan", {}) if dashboard else {}
    if battle:
        sniper = battle.get("sniper_points", {}) or {}
        if sniper:
            pts = []
            if sniper.get("ideal_buy"):
                pts.append(f"🎯{labels.get('ideal_buy_label', '理想买点')}:"
                           f"{notifier._clean_sniper_value(sniper.get('ideal_buy', 'N/A'))}")
            if sniper.get("stop_loss"):
                pts.append(f"🛑{labels.get('stop_loss_label', '止损')}:"
                           f"{notifier._clean_sniper_value(sniper.get('stop_loss', 'N/A'))}")
            if sniper.get("take_profit"):
                pts.append(f"🎊{labels.get('take_profit_label', '目标位')}:"
                           f"{notifier._clean_sniper_value(sniper.get('take_profit', 'N/A'))}")
            if pts:
                lines.append(" | ".join(pts))
        position = battle.get("position_strategy", {}) or {}
        if position:
            lines.append(
                f"💰 {labels.get('suggested_position_label', '建议仓位')}: "
                f"{position.get('suggested_position', 'N/A')}"
            )
        checklist = battle.get("action_checklist", []) if battle else []
        if checklist:
            lines.append(f"✅ {labels.get('checklist_heading', '行动清单')}:")
            for item in checklist:
                lines.append(f"- {item}")
        lines.append("")

    # 信号归因
    signal_attr = dashboard.get("signal_attribution", {}) if dashboard else {}
    if signal_attribution_has_content(signal_attr):
        weight_items = signal_attribution_weight_items(signal_attr)
        if weight_items:
            weight_labels = {
                "technical_indicators": ("📈", labels.get("technical_indicators_label", "技术指标")),
                "news_sentiment": ("📰", labels.get("news_sentiment_label", "消息情绪")),
                "fundamentals": ("📊", labels.get("fundamentals_label", "基本面")),
                "market_conditions": ("🌐", labels.get("market_conditions_label", "市场环境")),
            }
            parts = []
            for key, value in weight_items:
                icon, label = weight_labels.get(key, ("", key))
                parts.append(f"{icon} {label}: {value}%")
            if parts:
                lines.append("🎯 归因: " + " | ".join(parts))
        lines.append("")

    # 多策略综合
    strategy_synthesis = normalize_strategy_synthesis_payload(
        dashboard.get("strategy_synthesis") if dashboard else None
    )
    _notif_module._append_strategy_synthesis_block(lines, strategy_synthesis, labels, report_language)

    # 财务摘要 / 股东回报 / 关联板块
    notifier._append_fundamental_blocks(lines, result)

    # 无 dashboard 的传统字段兜底
    if not dashboard:
        if getattr(result, "buy_reason", None):
            lines.append(f"💡 {labels.get('reason_label', '操作理由')}: {result.buy_reason}")
        if getattr(result, "risk_warning", None):
            lines.append(f"⚠️ {labels.get('risk_warning_label', '风险提示')}: {result.risk_warning}")

    while lines and not lines[-1].strip():
        lines.pop()
    return lines


# ---------------------------------------------------------------------------
# 卡片 JSON 构建（schema 2.0 + body.elements + collapsible_panel）
# ---------------------------------------------------------------------------

def _overview_elements(notifier: Any, results: List[Any],
                       labels: Dict[str, str], report_language: str) -> List[dict]:
    """顶部总览区元素（统计 + 大盘状态 + 评分速览）。"""
    from src.report_language import localize_trend_prediction

    sorted_results = sorted(results, key=lambda x: getattr(x, "sentiment_score", 0), reverse=True)
    buy_count, hold_count, sell_count = notifier._count_display_decisions(results, report_language)
    market_status = notifier._public_market_status_line(results, report_language)

    lines: List[str] = [
        f"**{len(results)} 只** | "
        f"🟢{labels.get('buy_label', '买入')}:{buy_count} "
        f"🟡{labels.get('watch_label', '观望')}:{hold_count} "
        f"🔴{labels.get('sell_label', '卖出')}:{sell_count}",
    ]
    if market_status:
        lines += ["", market_status]
    lines += ["", f"**📊 {labels.get('summary_heading', '评分速览')}**"]
    for r in sorted_results:
        signal_text, signal_emoji, _ = notifier._get_signal_level(r)
        display_name = notifier._get_display_name(r, report_language)
        lines.append(
            f"{signal_emoji} **{display_name}({r.code})**: "
            f"{signal_text} | "
            f"{labels.get('score_label', '评分')} {r.sentiment_score} | "
            f"{localize_trend_prediction(r.trend_prediction, report_language)}"
        )

    return [
        {"tag": "div", "text": {"tag": "lark_md", "content": format_feishu_markdown("\n".join(lines))}},
        {"tag": "hr"},
    ]


def _stock_panel(notifier: Any, result: Any, labels: Dict[str, str],
                 report_language: str) -> Optional[dict]:
    """构建单只股票的折叠面板。返回 None 表示无内容。"""
    detail_lines = _render_stock_detail_lines(notifier, result, labels, report_language)
    if not detail_lines:
        return None

    signal_text, signal_emoji, _ = notifier._get_signal_level(result)
    stock_name = notifier._get_display_name(result, report_language)
    # 面板标题 = 摘要行（永久显示），展开后看明细
    title = (
        f"{signal_emoji} {stock_name}({result.code}) · {signal_text} · "
        f"{labels.get('score_label', '评分')} {result.sentiment_score}"
    )
    body_md = format_feishu_markdown("\n".join(detail_lines))

    return {
        "tag": "collapsible_panel",
        "expanded": False,
        "border": {"color": "grey"},
        "header": {
            "title": {"tag": "lark_md", "content": title},
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": body_md}},
        ],
    }


def _card(header_title: str, body_elements: List[dict],
          page_info: Optional[tuple] = None) -> dict:
    """构建一张 schema 2.0 interactive 卡片 body。"""
    elements = list(body_elements)
    if page_info:
        cur, total = page_info
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"<font color='grey'>📄 {cur}/{total}</font>"},
        })
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True, "enable_forward": True},
        "header": {
            "title": {"tag": "plain_text", "content": header_title},
            "template": "blue",
        },
        "body": {"elements": elements},
    }


def build_separator_card(push_time: Optional[str] = None) -> dict:
    """构建一张"新消息分隔条"卡片。

    作为独立的一条消息发在折叠分析卡之前，与上一次推送在视觉上隔开：
    群里看到的是 [上次推送] -> 飞书自然的消息间隔 -> [本分隔条] -> [本次分析卡]。
    header 用显眼颜色 + 文字提示，body 放粗分隔线。
    """
    if push_time is None:
        push_time = datetime.now().strftime("%Y-%m-%d %H:%M")
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True, "enable_forward": False},
        "header": {
            "title": {"tag": "plain_text", "content": f"🆕 新消息 · {push_time}"},
            "template": "red",
        },
        "body": {"elements": [
            {"tag": "hr"},
        ]},
    }


def _card_size(card: dict) -> int:
    return len(json.dumps(card, ensure_ascii=False).encode("utf-8"))


def build_cards(notifier: Any, results: List[Any],
                report_date: Optional[str] = None) -> List[dict]:
    """
    构建一组折叠面板卡片（可能多张，按大小/数量分片）。

    Args:
        notifier: NotificationService 实例
        results: AnalysisResult 列表
        report_date: 报告日期字符串

    Returns:
        schema 2.0 卡片 body 列表（每张可直接 json.dumps 后发送）
    """
    if not results:
        return []

    report_language = notifier._get_report_language(results)
    labels = notifier._get_labels(results)
    if report_date is None:
        report_date = datetime.now().strftime("%Y-%m-%d")

    header_title = f"📊 {report_date} {labels.get('dashboard_title', '股票智能分析报告')}"
    overview = _overview_elements(notifier, results, labels, report_language)

    sorted_results = sorted(results, key=lambda x: getattr(x, "sentiment_score", 0), reverse=True)
    panels: List[dict] = []
    for r in sorted_results:
        p = _stock_panel(notifier, r, labels, report_language)
        if p:
            panels.append(p)

    if not panels:
        return [_card(header_title, overview, None)]

    max_items = _max_items_per_card()

    # 切分：每张卡 = 总览(仅首张) + 若干 panel，受 max_items 和字节上限约束
    pages: List[List[dict]] = []
    current: List[dict] = list(overview)
    count = 0
    for p in panels:
        candidate = current + [p]
        would_card = _card(header_title, candidate, None)
        if (count >= max_items or _card_size(would_card) > _CARD_MAX_BYTES) and count > 0:
            pages.append(current)
            current = [p]
            count = 0
        else:
            current = candidate
            count += 1
    if current:
        pages.append(current)

    # 后续卡片补一个"续"提示 div 开头
    total_pages = len(pages)
    cards: List[dict] = []
    for i, elems in enumerate(pages, start=1):
        if i == 1:
            body = elems
        else:
            body = [{"tag": "div", "text": {"tag": "lark_md",
                    "content": f"_{header_title}（续）_"}}] + elems
        page_info = (i, total_pages) if total_pages > 1 else None
        cards.append(_card(header_title, body, page_info))
    return cards
