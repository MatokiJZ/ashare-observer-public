import pandas as pd

from ashare.storage import bars, bars_many, save_bars


def test_bulk_scan_preserves_individual_history_and_optional_columns():
    save_bars("600000", pd.DataFrame([{"date": "2026-10-13", "close": 11.0, "amount": 150e6}, {"date": "2026-10-12", "close": 10.0, "amount": 140e6}]))
    save_bars("000001", pd.DataFrame([{"date": "2026-10-13", "close": 12.0, "change_pct": 1.2}]))
    result = bars_many(["600000", "000001", "600999"])
    assert set(result) == {"600000", "000001"}
    assert result["600000"]["date"].tolist() == bars("600000")["date"].tolist()
    assert result["600000"]["close"].tolist() == [10, 11]
    assert result["000001"]["change_pct"].iloc[0] == 1.2
