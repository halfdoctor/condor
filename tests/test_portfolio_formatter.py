"""
Test for format_portfolio_as_table formatting with options contract symbols.
"""
from mcp_servers.hummingbot_api.formatters.portfolio import format_portfolio_as_table


def test_format_portfolio_as_table_options():
    sample_portfolio = {
        "master_account": {
            "derive": [
                {
                    "token": "USDC",
                    "units": 1000.5,
                    "available_units": 1000.5,
                    "price": 1.0,
                    "value": 1000.5,
                },
                {
                    "token": "ETH-20260925-3000-C",
                    "units": 2.5,
                    "available_units": 2.5,
                    "price": 250.75,
                    "value": 626.875,
                },
                {
                    "token": "BTC-20241227-100000-P",
                    "units": 1.0,
                    "available_units": 1.0,
                    "price": 1200.0,
                    "value": 1200.0,
                },
            ]
        }
    }

    formatted = format_portfolio_as_table(sample_portfolio)
    print("Formatted portfolio table:\n")
    print(formatted)

    assert "ETH-20260925-3000-C" in formatted
    assert "BTC-20241227-100000-P" in formatted
    assert "626.88" in formatted or "626.87" in formatted or "626.8" in formatted
    print("\nPortfolio table test passed successfully!")


if __name__ == "__main__":
    test_format_portfolio_as_table_options()
