# -*- coding: utf-8 -*-
"""
AkShare fundamental adapter (fail-open).

This adapter intentionally uses capability probing against multiple AkShare
endpoint candidates. It should never raise to caller; partial data is allowed.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

_DIVIDEND_KEYWORD_MAP: Dict[str, List[str]] = {
    "per_share": [
        "每股派息",
        "每股现金红利",
        "每股分红",
        "每股派现",
        "派现(元/股)",
        "派息(元/股)",
        "税前派息(元/股)",
        "现金分红(税前)",
    ],
    "plan_text": [
        "分配方案",
        "分红方案",
        "实施方案",
        "派息方案",
        "方案",
        "预案",
        "方案说明",
    ],
    "ex_dividend_date": ["除权除息日", "除息日", "除权日", "除权除息", "除息日期"],
    "record_date": ["股权登记日", "登记日"],
    "announce_date": ["公告日期", "公告日", "实施公告日", "预案公告日"],
    "report_date": ["报告期", "报告日期", "截止日期", "统计截止日期"],
}


def _safe_float(value: Any) -> Optional[float]:
    """Best-effort float conversion."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    s = str(value).strip().replace(",", "").replace("%", "")
    if not s:
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _parse_cn_amount(value: Any) -> Optional[float]:
    """Parse Chinese-unit amounts like '-1.55亿' / '234.5万' to yuan."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    s = str(value).strip().replace(",", "").replace("元", "")
    if not s or s in ("-", "--", "nan", "None"):
        return None
    sign = -1.0 if s.startswith("-") else 1.0
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(亿|万)?", s.lstrip("+-"))
    if not m:
        return None
    num = float(m.group(1))
    unit = m.group(2) or ""
    if unit == "亿":
        num *= 1e8
    elif unit == "万":
        num *= 1e4
    return sign * num


def _safe_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _safe_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    try:
        parsed = pd.to_datetime(value)
    except Exception:
        return None
    if pd.isna(parsed):
        return None
    try:
        return parsed.to_pydatetime()
    except Exception:
        return None


def _normalize_code(raw: Any) -> str:
    s = _safe_str(raw).upper()
    if "." in s:
        s = s.split(".", 1)[0]
    s = re.sub(r"^(SH|SZ|BJ)", "", s)
    return s


def _pick_by_keywords(row: pd.Series, keywords: List[str]) -> Optional[Any]:
    """
    Return first non-empty row value whose column name contains any keyword.
    """
    for col in row.index:
        col_s = str(col)
        if any(k in col_s for k in keywords):
            val = row.get(col)
            if val is not None and str(val).strip() not in ("", "-", "nan", "None"):
                return val
    return None


def _parse_dividend_plan_to_per_share(plan_text: str) -> Optional[float]:
    """Parse per-share cash dividend from Chinese plan text."""
    text = _safe_str(plan_text)
    if not text:
        return None

    for pattern in (
        r"(?:每)?\s*10\s*股?\s*派(?:发)?\s*([0-9]+(?:\.[0-9]+)?)\s*元",
        r"10\s*派\s*([0-9]+(?:\.[0-9]+)?)\s*元",
    ):
        match = re.search(pattern, text)
        if match:
            parsed = _safe_float(match.group(1))
            if parsed is not None and parsed > 0:
                return parsed / 10.0

    match_per_share = re.search(r"每\s*股\s*派(?:发)?\s*([0-9]+(?:\.[0-9]+)?)\s*元", text)
    if match_per_share:
        parsed = _safe_float(match_per_share.group(1))
        if parsed is not None and parsed > 0:
            return parsed
    return None


def _extract_cash_dividend_per_share(row: pd.Series) -> Optional[float]:
    """Extract pre-tax cash dividend per share from a row."""
    plan_text = _safe_str(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["plan_text"]))
    # Keep pre-tax semantics; skip explicit after-tax plans unless pre-tax marker exists.
    if "税后" in plan_text and "税前" not in plan_text and "含税" not in plan_text:
        return None

    direct = _safe_float(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["per_share"]))
    if direct is not None and direct > 0:
        return direct
    return _parse_dividend_plan_to_per_share(plan_text)


def _filter_rows_by_code(df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    code_cols = [c for c in df.columns if any(k in str(c) for k in ("代码", "股票代码", "证券代码", "symbol", "ts_code"))]
    if not code_cols:
        return df

    target = _normalize_code(stock_code)
    for col in code_cols:
        try:
            series = df[col].astype(str).map(_normalize_code)
            filtered = df[series == target]
            if not filtered.empty:
                return filtered
        except Exception:
            continue
    return pd.DataFrame()


def _normalize_report_date(value: Any) -> Optional[str]:
    parsed = _safe_datetime(value)
    return parsed.date().isoformat() if parsed else None


def _build_dividend_payload(
    dividend_df: pd.DataFrame,
    stock_code: str,
    max_events: int = 5,
) -> Dict[str, Any]:
    work_df = _filter_rows_by_code(dividend_df, stock_code)
    if work_df.empty:
        return {}

    now_date = datetime.now().date()
    ttm_start_date = now_date - timedelta(days=365)
    dedupe_keys = set()
    events: List[Dict[str, Any]] = []

    for _, row in work_df.iterrows():
        if not isinstance(row, pd.Series):
            continue
        ex_dt = _safe_datetime(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["ex_dividend_date"]))
        record_dt = _safe_datetime(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["record_date"]))
        announce_dt = _safe_datetime(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["announce_date"]))
        event_dt = ex_dt or record_dt or announce_dt
        if event_dt is None:
            continue
        event_date = event_dt.date()
        if event_date > now_date:
            continue

        per_share = _extract_cash_dividend_per_share(row)
        if per_share is None or per_share <= 0:
            continue

        dedupe_key = (event_date.isoformat(), round(per_share, 6))
        if dedupe_key in dedupe_keys:
            continue
        dedupe_keys.add(dedupe_key)

        events.append(
            {
                "event_date": event_date.isoformat(),
                "ex_dividend_date": ex_dt.date().isoformat() if ex_dt else None,
                "record_date": record_dt.date().isoformat() if record_dt else None,
                "announcement_date": announce_dt.date().isoformat() if announce_dt else None,
                "cash_dividend_per_share": round(per_share, 6),
                "is_pre_tax": True,
            }
        )

    if not events:
        return {}

    events.sort(key=lambda item: item.get("event_date") or "", reverse=True)
    ttm_events: List[Dict[str, Any]] = []
    for item in events:
        event_dt = _safe_datetime(item.get("event_date"))
        if event_dt is None:
            continue
        event_date = event_dt.date()
        if ttm_start_date <= event_date <= now_date:
            ttm_events.append(item)

    return {
        "events": events[:max(1, max_events)],
        "ttm_event_count": len(ttm_events),
        "ttm_cash_dividend_per_share": (
            round(sum(float(item.get("cash_dividend_per_share") or 0.0) for item in ttm_events), 6)
            if ttm_events else None
        ),
        "coverage": "cash_dividend_pre_tax",
        "as_of": now_date.isoformat(),
    }


def _extract_latest_row(df: pd.DataFrame, stock_code: str) -> Optional[pd.Series]:
    """
    Select the most relevant row for the given stock.
    """
    if df is None or df.empty:
        return None

    code_cols = [c for c in df.columns if any(k in str(c) for k in ("代码", "股票代码", "证券代码", "ts_code", "symbol"))]
    target = _normalize_code(stock_code)
    if code_cols:
        for col in code_cols:
            try:
                series = df[col].astype(str).map(_normalize_code)
                matched = df[series == target]
                if not matched.empty:
                    return matched.iloc[0]
            except Exception:
                continue
        return None

    # Fallback: use latest row
    return df.iloc[0]


# Fork 扩展：同花顺全市场资金流排行表（运行级缓存 TTL）与东财熔断阈值。
# 同花顺表一次拉全市场，21 只股票共享；每次运行新建 adapter → 缓存自动重建，保证数据新鲜。
_THS_FLOW_TTL_SECONDS = 300.0
_EASTMONEY_FLOW_FAIL_THRESHOLD = 1


class AkshareFundamentalAdapter:
    """AkShare adapter for fundamentals, capital flow and dragon-tiger signals."""

    def __init__(self) -> None:
        # 运行级缓存：symbol("即时"/"5日排行"/"10日排行") -> (fetch_time, DataFrame)
        self._ths_flow_cache: Dict[str, Tuple[float, Optional[pd.DataFrame]]] = {}
        # 股东户数：report_period("20260630") -> (fetch_time, DataFrame)
        self._gdhs_cache: Dict[str, Tuple[float, Optional[pd.DataFrame]]] = {}
        self._ths_flow_lock = threading.Lock()
        # 东财资金流连续失败计数：达到阈值后本轮直接走同花顺，避免每只股票都等东财超时
        self._eastmoney_flow_failed = 0

    def _fetch_ths_fund_flow(self, symbol: str) -> Optional[pd.DataFrame]:
        """拉取同花顺全市场资金流排行表，运行级缓存共享；失败不缓存（下次重试）。"""
        now = time.time()
        with self._ths_flow_lock:
            cached = self._ths_flow_cache.get(symbol)
            if cached is not None and now - cached[0] < _THS_FLOW_TTL_SECONDS:
                return cached[1]
        try:
            import akshare as ak

            df = ak.stock_fund_flow_individual(symbol=symbol)
            if isinstance(df, pd.Series):
                df = df.to_frame().T
            if isinstance(df, pd.DataFrame) and not df.empty:
                with self._ths_flow_lock:
                    self._ths_flow_cache[symbol] = (now, df)
                return df
        except Exception as exc:
            logger.warning("[资金流] 同花顺 %s 排行抓取失败: %s", symbol, exc)
        return None

    def _extract_ths_stock_flow(self, stock_code: str) -> Dict[str, Optional[float]]:
        """从同花顺全市场排行表中按代码过滤出个股资金流。"""
        target = _normalize_code(stock_code).zfill(6)
        if len(target) != 6 or not target.isdigit():
            return {}
        flow: Dict[str, Optional[float]] = {
            "main_net_inflow": None,
            "inflow_5d": None,
            "inflow_10d": None,
        }
        for symbol, field in (
            ("即时", "main_net_inflow"),
            ("5日排行", "inflow_5d"),
            ("10日排行", "inflow_10d"),
        ):
            # 串行抓取：同花顺接口底层依赖非线程安全的原生库（mini_racer），并发会崩进程
            df = self._fetch_ths_fund_flow(symbol)
            if df is None or df.empty:
                continue
            code_col = next((c for c in df.columns if any(k in str(c) for k in ("代码", "股票代码"))), None)
            if code_col is None:
                continue
            try:
                matched = df[df[code_col].astype(str).str.zfill(6) == target]
            except Exception:
                continue
            if matched.empty:
                continue
            net_col = next((c for c in matched.columns if any(k in str(c) for k in ("净额", "净流入"))), None)
            if net_col is None:
                continue
            value = _parse_cn_amount(matched.iloc[0][net_col])
            if value is not None:
                flow[field] = value
        if all(v is None for v in flow.values()):
            return {}
        return flow

    def _call_df_candidates(
        self,
        candidates: List[Tuple[str, Dict[str, Any]]],
    ) -> Tuple[Optional[pd.DataFrame], Optional[str], List[str]]:
        errors: List[str] = []
        try:
            import akshare as ak
        except Exception as exc:
            return None, None, [f"import_akshare:{type(exc).__name__}"]

        for func_name, kwargs in candidates:
            fn = getattr(ak, func_name, None)
            if fn is None:
                continue
            try:
                df = fn(**kwargs)
                if isinstance(df, pd.Series):
                    df = df.to_frame().T
                if isinstance(df, pd.DataFrame) and not df.empty:
                    return df, func_name, errors
            except Exception as exc:
                errors.append(f"{func_name}:{type(exc).__name__}")
                continue
        return None, None, errors

    def get_fundamental_bundle(self, stock_code: str) -> Dict[str, Any]:
        """
        Return normalized fundamental blocks from AkShare with partial tolerance.
        """
        result: Dict[str, Any] = {
            "status": "not_supported",
            "growth": {},
            "earnings": {},
            "institution": {},
            "source_chain": [],
            "errors": [],
        }

        # Financial indicators
        fin_df, fin_source, fin_errors = self._call_df_candidates([
            ("stock_financial_abstract", {"symbol": stock_code}),
            ("stock_financial_analysis_indicator", {"symbol": stock_code}),
            ("stock_financial_analysis_indicator", {}),
        ])
        result["errors"].extend(fin_errors)
        if fin_df is not None:
            row = _extract_latest_row(fin_df, stock_code)
            if row is not None:
                revenue_yoy = _safe_float(_pick_by_keywords(row, ["营业收入同比", "营收同比", "收入同比", "同比增长"]))
                profit_yoy = _safe_float(_pick_by_keywords(row, ["净利润同比", "净利同比", "归母净利润同比"]))
                roe = _safe_float(_pick_by_keywords(row, ["净资产收益率", "ROE", "净资产收益"]))
                gross_margin = _safe_float(_pick_by_keywords(row, ["毛利率"]))
                report_date = _normalize_report_date(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["report_date"]))
                revenue = _safe_float(_pick_by_keywords(row, ["营业总收入", "营业收入", "营收"]))
                net_profit_parent = _safe_float(_pick_by_keywords(row, ["归母净利润", "母公司股东净利润", "净利润"]))
                operating_cash_flow = _safe_float(
                    _pick_by_keywords(row, ["经营活动产生的现金流量净额", "经营现金流", "经营活动现金流"])
                )
                result["growth"] = {
                    "revenue_yoy": revenue_yoy,
                    "net_profit_yoy": profit_yoy,
                    "roe": roe,
                    "gross_margin": gross_margin,
                }
                financial_report_payload = {
                    "report_date": report_date,
                    "revenue": revenue,
                    "net_profit_parent": net_profit_parent,
                    "operating_cash_flow": operating_cash_flow,
                    "roe": roe,
                }
                if any(v is not None for v in financial_report_payload.values()):
                    result["earnings"]["financial_report"] = financial_report_payload
                result["source_chain"].append(f"growth:{fin_source}")

        # Earnings forecast
        forecast_df, forecast_source, forecast_errors = self._call_df_candidates([
            ("stock_yjyg_em", {"symbol": stock_code}),
            ("stock_yjyg_em", {}),
            ("stock_yjbb_em", {"symbol": stock_code}),
            ("stock_yjbb_em", {}),
        ])
        result["errors"].extend(forecast_errors)
        if forecast_df is not None:
            row = _extract_latest_row(forecast_df, stock_code)
            if row is not None:
                result["earnings"]["forecast_summary"] = _safe_str(
                    _pick_by_keywords(row, ["预告", "业绩变动", "内容", "摘要", "公告"])
                )[:200]
                result["source_chain"].append(f"earnings_forecast:{forecast_source}")

        # Earnings quick report
        quick_df, quick_source, quick_errors = self._call_df_candidates([
            ("stock_yjkb_em", {"symbol": stock_code}),
            ("stock_yjkb_em", {}),
        ])
        result["errors"].extend(quick_errors)
        if quick_df is not None:
            row = _extract_latest_row(quick_df, stock_code)
            if row is not None:
                result["earnings"]["quick_report_summary"] = _safe_str(
                    _pick_by_keywords(row, ["快报", "摘要", "公告", "说明"])
                )[:200]
                result["source_chain"].append(f"earnings_quick:{quick_source}")

        # Dividend details (cash dividend, pre-tax)
        dividend_df, dividend_source, dividend_errors = self._call_df_candidates([
            ("stock_fhps_detail_em", {"symbol": stock_code}),
            ("stock_history_dividend_detail", {"symbol": stock_code, "indicator": "分红", "date": ""}),
            ("stock_dividend_cninfo", {"symbol": stock_code}),
        ])
        result["errors"].extend(dividend_errors)
        if dividend_df is not None:
            dividend_payload = _build_dividend_payload(dividend_df, stock_code, max_events=5)
            if dividend_payload:
                result["earnings"]["dividend"] = dividend_payload
                result["source_chain"].append(f"dividend:{dividend_source}")

        # Institution / top shareholders
        inst_df, inst_source, inst_errors = self._call_df_candidates([
            ("stock_institute_hold", {}),
            ("stock_institute_recommend", {}),
        ])
        result["errors"].extend(inst_errors)
        if inst_df is not None:
            row = _extract_latest_row(inst_df, stock_code)
            if row is not None:
                inst_change = _safe_float(_pick_by_keywords(row, ["增减", "变化", "变动", "持股变化"]))
                result["institution"]["institution_holding_change"] = inst_change
                result["source_chain"].append(f"institution:{inst_source}")

        top10_df, top10_source, top10_errors = self._call_df_candidates([
            ("stock_gdfx_top_10_em", {"symbol": stock_code}),
            ("stock_gdfx_top_10_em", {}),
            ("stock_zh_a_gdhs_detail_em", {"symbol": stock_code}),
            ("stock_zh_a_gdhs_detail_em", {}),
        ])
        result["errors"].extend(top10_errors)
        if top10_df is not None:
            row = _extract_latest_row(top10_df, stock_code)
            if row is not None:
                holder_change = _safe_float(_pick_by_keywords(row, ["增减", "变化", "持股变化", "变动"]))
                result["institution"]["top10_holder_change"] = holder_change
                result["source_chain"].append(f"top10:{top10_source}")

        has_content = bool(result["growth"] or result["earnings"] or result["institution"])
        result["status"] = "partial" if has_content else "not_supported"
        return result

    def get_capital_flow(self, stock_code: str, top_n: int = 5) -> Dict[str, Any]:
        """
        Return stock + sector capital flow.
        """
        result: Dict[str, Any] = {
            "status": "not_supported",
            "stock_flow": {},
            "sector_rankings": {"top": [], "bottom": []},
            "source_chain": [],
            "errors": [],
        }

        # Fork 扩展：东财资金流连续失败后本轮熔断，直接走同花顺，避免每只股票白等东财超时
        eastmoney_circuit_open = self._eastmoney_flow_failed >= _EASTMONEY_FLOW_FAIL_THRESHOLD

        if not eastmoney_circuit_open:
            stock_df, stock_source, stock_errors = self._call_df_candidates([
                ("stock_individual_fund_flow", {"stock": stock_code}),
                ("stock_individual_fund_flow", {"symbol": stock_code}),
                ("stock_individual_fund_flow", {}),
                ("stock_main_fund_flow", {"symbol": stock_code}),
                ("stock_main_fund_flow", {}),
            ])
            result["errors"].extend(stock_errors)
            if stock_df is not None:
                row = _extract_latest_row(stock_df, stock_code)
                if row is not None:
                    net_inflow = _safe_float(_pick_by_keywords(row, ["主力净流入", "净流入", "净额"]))
                    inflow_5d = _safe_float(_pick_by_keywords(row, ["5日", "五日"]))
                    inflow_10d = _safe_float(_pick_by_keywords(row, ["10日", "十日"]))
                    result["stock_flow"] = {
                        "main_net_inflow": net_inflow,
                        "inflow_5d": inflow_5d,
                        "inflow_10d": inflow_10d,
                    }
                    result["source_chain"].append(f"capital_stock:{stock_source}")
            if not result["stock_flow"]:
                self._eastmoney_flow_failed += 1
                if self._eastmoney_flow_failed >= _EASTMONEY_FLOW_FAIL_THRESHOLD:
                    logger.info(
                        "[资金流] 东财资金流连续失败 %s 次，本轮切换同花顺源",
                        self._eastmoney_flow_failed,
                    )

        # Fork 扩展：东财不可用时用同花顺全市场排行表回退（运行级缓存，多股票共享）
        if not result["stock_flow"]:
            ths_flow = self._extract_ths_stock_flow(stock_code)
            if ths_flow:
                result["stock_flow"] = ths_flow
                result["source_chain"].append("capital_stock:ths_stock_fund_flow_individual")

        sector_df, sector_source, sector_errors = None, None, []
        if not eastmoney_circuit_open:
            sector_df, sector_source, sector_errors = self._call_df_candidates([
            ("stock_sector_fund_flow_rank", {}),
            ("stock_sector_fund_flow_summary", {}),
        ])
        result["errors"].extend(sector_errors)
        if sector_df is not None:
            name_col = next((c for c in sector_df.columns if any(k in str(c) for k in ("板块", "行业", "名称", "name"))), None)
            flow_col = next((c for c in sector_df.columns if any(k in str(c) for k in ("净流入", "主力", "flow", "净额"))), None)
            if name_col and flow_col:
                work_df = sector_df[[name_col, flow_col]].copy()
                work_df[flow_col] = pd.to_numeric(work_df[flow_col], errors="coerce")
                work_df = work_df.dropna(subset=[flow_col])
                top_df = work_df.nlargest(top_n, flow_col)
                bottom_df = work_df.nsmallest(top_n, flow_col)
                result["sector_rankings"] = {
                    "top": [{"name": _safe_str(r[name_col]), "net_inflow": float(r[flow_col])} for _, r in top_df.iterrows()],
                    "bottom": [{"name": _safe_str(r[name_col]), "net_inflow": float(r[flow_col])} for _, r in bottom_df.iterrows()],
                }
                result["source_chain"].append(f"capital_sector:{sector_source}")

        has_content = bool(result["stock_flow"] or result["sector_rankings"]["top"] or result["sector_rankings"]["bottom"])
        result["status"] = "partial" if has_content else "not_supported"
        return result

    def _generate_gdhs_periods(self, n: int = 8) -> List[str]:
        """最近 n 个不晚于今天的季度末报告期（如 20260630/20260331/...）。"""
        now = datetime.now()
        qends = [(3, 31), (6, 30), (9, 30), (12, 31)]
        periods: List[str] = []
        year, quarter = now.year, (now.month - 1) // 3
        while len(periods) < max(1, n):
            mm, dd = qends[quarter]
            qend = datetime(year, mm, dd)
            if qend <= now:
                periods.append(qend.strftime("%Y%m%d"))
            quarter -= 1
            if quarter < 0:
                quarter = 3
                year -= 1
        return periods

    def _fetch_gdhs(self, period: str) -> Optional[pd.DataFrame]:
        """拉取东财股东户数全市场表，运行级缓存共享；失败不缓存（下次重试）。"""
        now = time.time()
        with self._ths_flow_lock:
            cached = self._gdhs_cache.get(period)
            if cached is not None and now - cached[0] < _THS_FLOW_TTL_SECONDS:
                return cached[1]
        try:
            import akshare as ak

            df = ak.stock_zh_a_gdhs(symbol=period)
            if isinstance(df, pd.Series):
                df = df.to_frame().T
            if isinstance(df, pd.DataFrame) and not df.empty:
                with self._ths_flow_lock:
                    self._gdhs_cache[period] = (now, df)
                return df
        except Exception as exc:
            logger.warning("[股东户数] 东财 %s 抓取失败: %s", period, exc)
        return None

    def get_shareholder_count(self, stock_code: str) -> Dict[str, Any]:
        """返回最近报告期股东户数与较上期变化（筹码集中度代理）。"""
        result: Dict[str, Any] = {
            "status": "not_supported",
            "data": {},
            "source_chain": [],
            "errors": [],
        }
        target = _normalize_code(stock_code).zfill(6)
        if len(target) != 6 or not target.isdigit():
            return result
        for period in self._generate_gdhs_periods():
            df = self._fetch_gdhs(period)
            if df is None or df.empty:
                continue
            code_col = next((c for c in df.columns if any(k in str(c) for k in ("代码", "股票代码", "证券代码"))), None)
            if code_col is None:
                continue
            try:
                matched = df[df[code_col].astype(str).str.zfill(6) == target]
            except Exception:
                continue
            if matched.empty:
                continue
            row = matched.iloc[0]
            holder_count = _safe_float(_pick_by_keywords(row, ["股东户数-本次"]))
            prev_count = _safe_float(_pick_by_keywords(row, ["股东户数-上次"]))
            change = _safe_float(_pick_by_keywords(row, ["股东户数-变化"]))
            change_pct = _safe_float(_pick_by_keywords(row, ["变化比例"]))
            # 兜底：列名/值缺失时由本次-上次推导
            if change is None and holder_count is not None and prev_count:
                change = round(holder_count - prev_count, 2)
            if change_pct is None and change is not None and prev_count:
                change_pct = round(change / prev_count * 100.0, 2)
            result["data"] = {
                "report_date": period,
                "holder_count": holder_count,
                "prev_count": prev_count,
                "change": change,
                "change_pct": change_pct,
            }
            result["source_chain"].append(f"shareholder_count:gdhs:{period}")
            result["status"] = "ok" if any(v is not None for v in result["data"].values()) else "partial"
            return result
        return result

    def get_dragon_tiger_flag(self, stock_code: str, lookback_days: int = 20) -> Dict[str, Any]:
        """
        Return dragon-tiger signal in lookback window.
        """
        result: Dict[str, Any] = {
            "status": "not_supported",
            "is_on_list": False,
            "recent_count": 0,
            "latest_date": None,
            "source_chain": [],
            "errors": [],
        }

        df, source, errors = self._call_df_candidates([
            ("stock_lhb_stock_statistic_em", {}),
            ("stock_lhb_detail_em", {}),
            ("stock_lhb_jgmmtj_em", {}),
        ])
        result["errors"].extend(errors)
        if df is None:
            return result

        # Try code filter
        code_cols = [c for c in df.columns if any(k in str(c) for k in ("代码", "股票代码", "证券代码"))]
        target = _normalize_code(stock_code)
        matched = pd.DataFrame()
        for col in code_cols:
            try:
                series = df[col].astype(str).map(_normalize_code)
                cur = df[series == target]
                if not cur.empty:
                    matched = cur
                    break
            except Exception:
                continue
        if matched.empty:
            result["source_chain"].append(f"dragon_tiger:{source}")
            result["status"] = "ok" if code_cols else "partial"
            return result

        date_col = next((c for c in matched.columns if any(k in str(c) for k in ("日期", "上榜", "交易日", "time"))), None)
        parsed_dates: List[datetime] = []
        if date_col is not None:
            for val in matched[date_col].astype(str).tolist():
                try:
                    parsed_dates.append(pd.to_datetime(val).to_pydatetime())
                except Exception:
                    continue
        now = datetime.now()
        start = now - timedelta(days=max(1, lookback_days))
        recent_dates = [d for d in parsed_dates if start <= d <= now]

        result["is_on_list"] = bool(recent_dates)
        result["recent_count"] = len(recent_dates) if recent_dates else int(len(matched))
        result["latest_date"] = max(recent_dates).date().isoformat() if recent_dates else (
            max(parsed_dates).date().isoformat() if parsed_dates else None
        )
        result["status"] = "ok"
        result["source_chain"].append(f"dragon_tiger:{source}")
        return result
