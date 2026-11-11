"""Sector-to-ETF mapping for divergence pre-filter.

Maps stock ticker symbols to their representative sector ETFs.
Used by Gap & Go and other strategies for sector divergence checks.
"""
from __future__ import annotations

# =============================================================================
# [ADDED 2026-11-11]: B2 - Sector Divergence Pre-Filter Constants
# ORIGINAL INTENT: No sector mapping existed; each strategy made ad-hoc checks.
# REASON FOR CHANGE: Centralize sector-to-ETF mapping for reuse across strategies.
#   Gap & Go uses this to suppress signals when the underlying sector ETF is
#   breaking down (negative intraday return > -1.5% AND EMA slope < 0).
# SAFE ROLLBACK: Remove this file and the enable_sector_divergence_filter param.
# -----------------------------------------------------------------------------
# [NEW CODE]:

# Sector ETF proxy for each GICS sector.
# When a stock is in one of these sectors and the ETF has a negative
# intraday return > -1.5% with a downward EMA slope, signals are suppressed.
SECTOR_ETF_MAP: dict[str, str] = {
    # Technology
    "XLK": "XLK",   # Technology SPDR
    "SMH": "SMH",   # VanEck Semiconductor
    "SOXX": "SOXX", # iShares PHLX Semiconductor
    "SOXL": "SOXL", # Direxion Semi 3x Bull

    # Financials
    "XLF": "XLF",   # Financials SPDR
    "KBE": "KBE",   # SPDR Bank ETF
    "KRE": "KRE",   # SPDR Regional Bank

    # Healthcare
    "XLV": "XLV",   # Healthcare SPDR
    "IBB": "IBB",   # iShares Biotech
    "XBI": "XBI",   # SPDR Biotech

    # Energy
    "XLE": "XLE",   # Energy SPDR
    "OIH": "OIH",   # VanEck Oil Services

    # Consumer Discretionary
    "XLY": "XLY",   # Consumer Disc SPDR
    "RTH": "RTH",   # VanEck Retail

    # Consumer Staples
    "XLP": "XLP",   # Consumer Staples SPDR

    # Industrials
    "XLI": "XLI",   # Industrials SPDR

    # Materials
    "XLB": "XLB",   # Materials SPDR
    "GDX": "GDX",   # VanEck Gold Miners

    # Real Estate
    "XLRE": "XLRE", # Real Estate SPDR
    "IYR": "IYR",   # iShares Real Estate

    # Utilities
    "XLU": "XLU",   # Utilities SPDR

    # Communication Services
    "XLC": "XLC",   # Communication Services SPDR

    # Broad Market / Index
    "SPY": "SPY",   # S&P 500
    "QQQ": "QQQ",   # Nasdaq 100
    "IWM": "IWM",   # Russell 2000
}

# Stock ticker → sector ETF proxy.
# This is a representative (not exhaustive) list; expand as needed.
# When a ticker is not found, the gap & go sector filter is skipped for that symbol.
TICKER_SECTOR_ETF: dict[str, str] = {
    # Semiconductors → SOXX
    "NVDA": "SOXX", "AMD": "SOXX", "INTC": "SOXX", "QCOM": "SOXX",
    "MU": "SOXX", "AMAT": "SOXX", "LRCX": "SOXX", "KLAC": "SOXX",
    "MRVL": "SOXX", "ON": "SOXX", "TXN": "SOXX", "AVGO": "SOXX",
    "TSM": "SOXX", "ASML": "SOXX", "MPWR": "SOXX", "SWKS": "SOXX",
    "CRUS": "SOXX", "ENTG": "SOXX", "TER": "SOXX",

    # Technology → XLK
    "AAPL": "XLK", "MSFT": "XLK", "CRM": "XLK", "ADBE": "XLK",
    "ORCL": "XLK", "ACN": "XLK", "IBM": "XLK", "INTU": "XLK",
    "NOW": "XLK", "SNOW": "XLK", "PLTR": "XLK", "PANW": "XLK",
    "CRWD": "XLK", "ZS": "XLK", "OKTA": "XLK", "DDOG": "XLK",
    "NET": "XLK", "MDB": "XLK", "SHOP": "XLK", "DOCU": "XLK",

    # Financials → XLF
    "JPM": "XLF", "BAC": "XLF", "WFC": "XLF", "GS": "XLF",
    "MS": "XLF", "C": "XLF", "BLK": "XLF", "SCHW": "XLF",
    "AXP": "XLF", "V": "XLF", "MA": "XLF", "PYPL": "XLF",
    "SQ": "XLF", "COIN": "XLF",

    # Biotech → XBI
    "MRNA": "XBI", "BNTX": "XBI", "REGN": "XBI", "VRTX": "XBI",
    "BIIB": "XBI", "GILD": "XBI", "SGEN": "XBI", "BMRN": "XBI",
    "EXAS": "XBI", "FATE": "XBI", "BEAM": "XBI",

    # Healthcare → XLV
    "JNJ": "XLV", "UNH": "XLV", "PFE": "XLV", "LLY": "XLV",
    "ABBV": "XLV", "TMO": "XLV", "MDT": "XLV", "ABT": "XLV",
    "CVS": "XLV", "CI": "XLV", "HUM": "XLV",

    # Energy → XLE
    "XOM": "XLE", "CVX": "XLE", "COP": "XLE", "EOG": "XLE",
    "SLB": "XLE", "PXD": "XLE", "MPC": "XLE", "VLO": "XLE",
    "PSX": "XLE", "HAL": "XLE",

    # Consumer Discretionary → XLY
    "AMZN": "XLY", "TSLA": "XLY", "HD": "XLY", "MCD": "XLY",
    "NKE": "XLY", "SBUX": "XLY", "LOW": "XLY", "TJX": "XLY",
    "BKNG": "XLY", "EXPE": "XLY", "GM": "XLY", "F": "XLY",

    # Communication Services → XLC
    "GOOGL": "XLC", "GOOG": "XLC", "META": "XLC", "NFLX": "XLC",
    "DIS": "XLC", "CMCSA": "XLC", "T": "XLC", "VZ": "XLC",
    "SNAP": "XLC", "PINS": "XLC", "RBLX": "XLC",

    # Industrials → XLI
    "GE": "XLI", "HON": "XLI", "CAT": "XLI", "DE": "XLI",
    "MMM": "XLI", "RTX": "XLI", "LMT": "XLI", "BA": "XLI",
    "FDX": "XLI", "UPS": "XLI",

    # Materials → XLB
    "FCX": "XLB", "NEM": "XLB", "DOW": "XLB", "LIN": "XLB",
    "APD": "XLB", "NUE": "XLB", "GOLD": "GDX",

    # Utilities → XLU
    "NEE": "XLU", "DUK": "XLU", "SO": "XLU", "AEP": "XLU",
    "EXC": "XLU", "XEL": "XLU",
}


def get_sector_etf(symbol: str) -> str | None:
    """Return the representative sector ETF for a given stock ticker.

    Returns None if no mapping exists (sector filter will be skipped).
    """
    return TICKER_SECTOR_ETF.get(symbol.upper())
# =============================================================================
