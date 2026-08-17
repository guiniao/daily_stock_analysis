# -*- coding: utf-8 -*-
"""
analyze_trend 工具必须返回周线/月线跨周期字段（日K重采样结果）。
"""

import os
import sys
import unittest
from datetime import date, timedelta
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.agent.tools.analysis_tools import _handle_analyze_trend


def _make_history_df(days: int = 240, last_date: date = None) -> pd.DataFrame:
    if last_date is None:
        last_date = date.today() - timedelta(days=1)
    dates = [last_date - timedelta(days=i) for i in range(days - 1, -1, -1)]
    base = 100.0
    rows = []
    for i, d in enumerate(dates):
        close = base + i * 0.5
        rows.append(
            {
                "date": d,
                "open": close - 0.3,
                "high": close + 0.5,
                "low": close - 0.5,
                "close": close,
                "volume": 1000000 + i * 1000,
                "amount": 100000000 + i * 100000,
            }
        )
    df = pd.DataFrame(rows)
    return df


class TestAnalyzeTrendWeeklyMonthly(unittest.TestCase):
    def test_analyze_trend_returns_weekly_monthly_fields(self) -> None:
        df = _make_history_df(days=240)
        with patch(
            "src.agent.tools.analysis_tools._fetch_trend_data", return_value=df
        ):
            result = _handle_analyze_trend("002049")

        self.assertIn("weekly_trend", result)
        self.assertIn("monthly_trend", result)
        # 240 个交易日足够重采样出月线 MA3/MA6
        self.assertTrue(result.get("weekly_trend") or result.get("weekly_ma5") is not None)
        self.assertTrue(result.get("monthly_trend") or result.get("monthly_ma6") is not None)

    def test_analyze_trend_short_history_keeps_weekly_optional(self) -> None:
        """数据窗口不足时字段仍存在（可为空），工具不报错。"""
        df = _make_history_df(days=30)
        with patch(
            "src.agent.tools.analysis_tools._fetch_trend_data", return_value=df
        ):
            result = _handle_analyze_trend("002049")

        self.assertIn("weekly_trend", result)
        self.assertIn("monthly_trend", result)


if __name__ == "__main__":
    unittest.main()
