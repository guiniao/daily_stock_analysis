# -*- coding: utf-8 -*-
"""Regression tests for pipeline data-fetch error handling."""

from datetime import date, datetime, timezone
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

from src.analyzer import AnalysisResult
from src.core.pipeline import StockAnalysisPipeline


class PipelineFetchErrorTestCase(unittest.TestCase):
    """`fetch_and_save_stock_data` should preserve the original exception."""

    def test_fetch_and_save_handles_stock_name_lookup_failure(self):
        pipeline = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
        pipeline.fetcher_manager = MagicMock()
        pipeline.db = MagicMock()
        pipeline.fetcher_manager.get_stock_name.side_effect = RuntimeError("name lookup failed")

        success, error = StockAnalysisPipeline.fetch_and_save_stock_data(pipeline, "600519")

        self.assertFalse(success)
        self.assertIn("name lookup failed", error or "")

    @patch.object(
        StockAnalysisPipeline,
        "_resolve_resume_target_date",
        return_value=date(2026, 3, 27),
    )
    def test_fetch_and_save_uses_effective_trading_date_for_resume_check(self, _mock_target):
        pipeline = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
        pipeline.config = MagicMock(technical_history_days=260)
        pipeline.fetcher_manager = MagicMock()
        pipeline.db = MagicMock()
        pipeline.fetcher_manager.get_stock_name.return_value = "贵州茅台"
        pipeline.db.has_today_data.return_value = True
        # 历史深度足够（>=130条），命中断点续传跳过
        pipeline.db.get_data_range.return_value = [MagicMock()] * 200
        current_time = datetime(2026, 3, 28, 1, 0, tzinfo=timezone.utc)

        success, error = StockAnalysisPipeline.fetch_and_save_stock_data(
            pipeline,
            "600519",
            current_time=current_time,
        )

        self.assertTrue(success)
        self.assertIsNone(error)
        _mock_target.assert_called_once_with("600519", current_time=current_time)
        pipeline.db.has_today_data.assert_called_once_with("600519", date(2026, 3, 27))
        pipeline.fetcher_manager.get_daily_data.assert_not_called()

    def test_fetch_and_save_refetches_when_history_depth_shallow(self):
        """已有今日数据但历史深度不足（新股票）时，重新拉取补齐，否则周线/月线算不出。"""
        pipeline = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
        pipeline.config = MagicMock(technical_history_days=260)
        pipeline.fetcher_manager = MagicMock()
        pipeline.db = MagicMock()
        pipeline.fetcher_manager.get_stock_name.return_value = "中巨芯"
        pipeline.db.has_today_data.return_value = True
        # 只有 40 条，远低于 130 阈值 -> 应重新拉取
        pipeline.db.get_data_range.return_value = [MagicMock()] * 40
        fake_df = pd.DataFrame({"close": [1.0]})
        pipeline.fetcher_manager.get_daily_data.return_value = (fake_df, "tushare")
        pipeline.db.save_daily_data.return_value = 260

        success, error = StockAnalysisPipeline.fetch_and_save_stock_data(
            pipeline, "688549", current_time=datetime(2026, 8, 18, 1, 0, tzinfo=timezone.utc)
        )

        self.assertTrue(success)
        # 浅历史 -> 走了网络拉取，且窗口对齐 technical_history_days
        pipeline.fetcher_manager.get_daily_data.assert_called_once_with("688549", days=260)
        pipeline.db.save_daily_data.assert_called_once()

    def test_run_retries_failed_stocks_sequentially(self):
        """并发阶段失败的股票，结束后串行重试并纳入汇总，不再静默丢弃。"""
        pipeline = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
        pipeline.config = MagicMock()
        pipeline.config.single_stock_notify = False
        pipeline.config.report_type = "simple"
        pipeline.config.analysis_delay = 0
        pipeline.max_workers = 1
        pipeline.fetcher_manager = MagicMock()
        pipeline.fetcher_manager.prefetch_daily_klines.return_value = 0
        pipeline.fetcher_manager.prefetch_realtime_quotes.return_value = 0
        pipeline.fetcher_manager.prefetch_stock_names = MagicMock()
        pipeline.db = MagicMock()
        pipeline._save_local_report = MagicMock()
        pipeline._send_notifications = MagicMock()

        ok = AnalysisResult(
            code="600519",
            name="贵州茅台",
            sentiment_score=60,
            trend_prediction="看多",
            operation_advice="持有",
            success=True,
        )
        # 第一次调用（并发）返回 None 表示失败；第二次调用（串行重试）成功
        with patch.object(
            StockAnalysisPipeline,
            "process_single_stock",
            side_effect=[None, ok],
        ) as mock_process:
            results = pipeline.run(
                stock_codes=["600519"],
                dry_run=False,
                send_notification=False,
                merge_notification=False,
            )

        self.assertEqual(len(results), 1)
        self.assertIs(results[0], ok)
        # 并发 1 次 + 串行重试 1 次
        self.assertEqual(mock_process.call_count, 2)

    def test_run_recovers_failed_and_successful_stocks(self):
        """混合场景：并发失败 1 只 + 成功 1 只，重试后两只都进 results。"""
        pipeline = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
        pipeline.config = MagicMock()
        pipeline.config.single_stock_notify = False
        pipeline.config.report_type = "simple"
        pipeline.config.analysis_delay = 0
        pipeline.max_workers = 2
        pipeline.fetcher_manager = MagicMock()
        pipeline.fetcher_manager.prefetch_daily_klines.return_value = 0
        pipeline.fetcher_manager.prefetch_realtime_quotes.return_value = 0
        pipeline.fetcher_manager.prefetch_stock_names = MagicMock()
        pipeline.db = MagicMock()
        pipeline._save_local_report = MagicMock()
        pipeline._send_notifications = MagicMock()

        ok_a = AnalysisResult(
            code="600519", name="贵州茅台", sentiment_score=60,
            trend_prediction="看多", operation_advice="持有",
        )
        ok_b = AnalysisResult(
            code="000001", name="平安银行", sentiment_score=50,
            trend_prediction="震荡", operation_advice="持有",
        )
        # 600519 并发失败(None) -> 串行重试成功；000001 并发直接成功
        with patch.object(
            StockAnalysisPipeline,
            "process_single_stock",
            side_effect=[None, ok_a, ok_b],
        ) as mock_process:
            results = pipeline.run(
                stock_codes=["600519", "000001"],
                dry_run=False,
                send_notification=False,
                merge_notification=False,
            )

        codes = {r.code for r in results}
        self.assertEqual(codes, {"600519", "000001"})
        self.assertEqual(mock_process.call_count, 3)

    def test_resolve_resume_target_date_normalizes_supported_a_share_formats(self):
        with patch("src.core.pipeline.get_market_for_stock", return_value="cn") as mock_market, patch(
            "src.core.pipeline.get_effective_trading_date",
            return_value=date(2026, 3, 27),
        ) as mock_target:
            for code in ("SH600519", "000001.SZ", "BJ920748"):
                result = StockAnalysisPipeline._resolve_resume_target_date(code)
                self.assertEqual(result, date(2026, 3, 27))

        self.assertEqual(
            [args.args[0] for args in mock_market.call_args_list],
            ["600519", "000001", "920748"],
        )
        self.assertEqual(mock_target.call_count, 3)


if __name__ == "__main__":
    unittest.main()
