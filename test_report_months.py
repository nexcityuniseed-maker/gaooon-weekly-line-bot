"""
months_in_range() の単体テスト（Playwright不要・ネットワーク不要）。
実行: python test_report_months.py
"""
from datetime import date
from daily_report import months_in_range


def test_within_single_month():
    # 5/25(月)〜5/31(日) は5月のみ
    assert months_in_range(date(2026, 5, 25), date(2026, 5, 31)) == [(2026, 5)]


def test_thursday_run_same_month():
    # 5/18〜5/24 も5月のみ（5/28実行時に動いていたケース）
    assert months_in_range(date(2026, 5, 18), date(2026, 5, 24)) == [(2026, 5)]


def test_spans_two_months():
    # 6/29(月)〜7/5(日) は6月と7月をまたぐ
    assert months_in_range(date(2026, 6, 29), date(2026, 7, 5)) == [(2026, 6), (2026, 7)]


def test_spans_year_boundary():
    # 12/29(月)〜1/4(日) は2025年12月と2026年1月をまたぐ
    assert months_in_range(date(2025, 12, 29), date(2026, 1, 4)) == [(2025, 12), (2026, 1)]


if __name__ == "__main__":
    test_within_single_month()
    test_thursday_run_same_month()
    test_spans_two_months()
    test_spans_year_boundary()
    print("OK: all months_in_range tests passed")
