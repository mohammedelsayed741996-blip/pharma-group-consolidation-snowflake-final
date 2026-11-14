"""
Shared configuration for the Helix Pharma Group synthetic data generator.

Everything that defines the *shape* of the fictional group lives here:
entities, currencies, business units, departments (cost centres), the
120-account group chart of accounts and the commercial drivers used by the
trial balance simulator.

Helix Pharma Group is entirely fictional. Any resemblance to a real company is
coincidental; all figures are synthetic.

Sign convention used everywhere in this repository
---------------------------------------------------
* Amounts are stored as signed numbers: debit = positive, credit = negative.
* A trial balance (TB) of one entity for one period therefore sums to zero.
* Exchange rates follow the ECB convention: units of local currency per 1 EUR,
  so  amount_EUR = amount_LC / rate.
"""
from __future__ import annotations

from pathlib import Path

# --------------------------------------------------------------------------- #
# General settings
# --------------------------------------------------------------------------- #
SEED = 20_260_101                      # master seed -> fully reproducible output
GROUP_CURRENCY = "EUR"
FIRST_YEAR, LAST_YEAR = 2023, 2025
YEARS = list(range(FIRST_YEAR, LAST_YEAR + 1))
PERIODS = [(y, m) for y in YEARS for m in range(1, 13)]   # 36 monthly closes

# SAP-style balance carry-forward: period 0 of the first fiscal year holds the
# opening balance sheet (data migration). It is translated at the closing rate
# of the month before the first period (FX period 202212).
OPENING_PERIOD = (FIRST_YEAR, 0)
OPENING_FX_PERIOD = f"{FIRST_YEAR - 1}12"

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = REPO_ROOT / "data"


def period_id(year: int, month: int) -> str:
    """Return the 6-character period id used across the repo, e.g. 202403 (or 202300 for opening)."""
    return f"{year}{month:02d}"


def fx_period_for(year: int, month: int) -> str:
    """FX period used to translate a fiscal period (period 0 uses the prior December closing rate)."""
    return OPENING_FX_PERIOD if month == 0 else period_id(year, month)


# --------------------------------------------------------------------------- #
# Currencies: start rate = units per EUR on 1 Dec 2022 (rounded, realistic level)
# drift / vol are annualised parameters of the simulated daily random walk.
# --------------------------------------------------------------------------- #
CURRENCIES = [
    # code, name,                start,   drift,  vol,  decimals
    ("EUR", "Euro",               1.0,     0.00,  0.00, 2),
    ("CZK", "Czech koruna",       24.30,   0.005, 0.04, 2),
    ("PLN", "Polish zloty",       4.69,   -0.010, 0.05, 2),
    ("HUF", "Hungarian forint",   405.0,   0.010, 0.07, 2),
    ("RON", "Romanian leu",       4.92,    0.005, 0.02, 2),
    ("GBP", "Pound sterling",     0.860,   0.000, 0.07, 2),
    ("CHF", "Swiss franc",        0.985,  -0.030, 0.06, 2),
    ("SEK", "Swedish krona",      11.00,   0.000, 0.06, 2),
]

# --------------------------------------------------------------------------- #
# Business units (management segments)
# --------------------------------------------------------------------------- #
BUSINESS_UNITS = [
    # code,     name,                         segment
    ("RX",     "Prescription medicines (Rx)", "Pharmaceuticals"),
    ("OTC",    "Over-the-counter (OTC)",      "Consumer Health"),
    ("MEDDEV", "Medical devices",             "Consumer Health"),
    ("HERBAL", "Herbal preparations",         "Consumer Health"),
    ("CORP",   "Corporate & unallocated",     "Corporate"),
]
SELLING_BUS = ["RX", "OTC", "MEDDEV", "HERBAL"]

# Gross sales account per business unit
GROSS_SALES_ACCOUNT = {"RX": "4000", "OTC": "4001", "MEDDEV": "4002", "HERBAL": "4003"}

# Gross-to-net deductions as share of gross sales (illustrative, parameterised).
# on_invoice deductions reduce the receivable, accrued ones go to accruals.
GTN_RATES = {
    #           statutory  wholesaler  returns  tender
    "RX":     {"4050": 0.080, "4060": 0.040, "4070": 0.015, "4080": 0.030},
    "OTC":    {"4050": 0.000, "4060": 0.060, "4070": 0.025, "4080": 0.000},
    "MEDDEV": {"4050": 0.000, "4060": 0.030, "4070": 0.010, "4080": 0.050},
    "HERBAL": {"4050": 0.000, "4060": 0.050, "4070": 0.020, "4080": 0.000},
}
GTN_ON_INVOICE = {"4060", "4080"}            # wholesaler & tender discounts
GTN_ACCRUAL_ACCOUNT = {"4050": "2140", "4070": "2150"}

# Cost of goods sold (at transfer price for sales companies) as share of net sales
COGS_RATIO = {"RX": 0.42, "OTC": 0.48, "MEDDEV": 0.55, "HERBAL": 0.50}
# Which business units are sourced from the group's own factory (DE02)
IC_SOURCED_BUS = {"RX", "OTC", "HERBAL"}     # medical devices are bought from third parties

# Transfer pricing
DE02_MARKUP = 0.15          # DE02 sells finished goods at standard cost + 15 %
CZ02_MARKUP = 0.08          # CZ02 contract manufacturing at cost + 8 %
CZ02_SHARE_OF_PRODUCTION = 0.20
CZ02_CASH_COST_FACTOR = 0.88   # share of (revenue / (1 + markup)) that is cash cost; rest covers D&A and G&A
MGMT_FEE_RATE = 0.02        # DE01 charges 2 % of third-party net sales
IC_LOAN_RATE = 0.045        # annual interest on intercompany loans
IC_LOAN_PL01_OPENING = 6.0e6     # DE01 -> PL01 loan outstanding at migration (EUR)
IC_LOAN_PL01_REPAYMENT = 1.0e6   # repaid every December
IC_LOAN_RO01_DRAWDOWN = 5.0e6    # DE01 -> RO01 loan drawn in July 2024

# Monthly seasonality per business unit (index = calendar month - 1)
SEASONALITY = {
    "RX":     [1.02, 1.00, 1.04, 1.00, 0.99, 1.00, 0.97, 0.90, 1.01, 1.03, 1.03, 1.01],
    "OTC":    [1.35, 1.25, 1.05, 0.90, 0.80, 0.75, 0.70, 0.75, 0.95, 1.10, 1.25, 1.40],
    "MEDDEV": [1.00, 1.00, 1.02, 1.00, 1.00, 0.98, 0.96, 0.92, 1.02, 1.04, 1.04, 1.02],
    "HERBAL": [1.20, 1.15, 1.05, 0.95, 0.90, 0.85, 0.80, 0.85, 0.95, 1.05, 1.15, 1.10],
}

# --------------------------------------------------------------------------- #
# Legal entities (12 entities, 10 countries, 8 currencies)
# sales_eur_m       = annual third-party gross sales in EUR m (2022 run-rate)
# bu_mix            = share of gross sales by business unit
# growth_act        = actual annual volume growth (budget assumes BUDGET_GROWTH)
# --------------------------------------------------------------------------- #
ENTITIES = [
    dict(code="DE01", name="Helix Pharma Holding GmbH", country="DE", country_name="Germany",
         city="Berlin", ccy="EUR", etype="HOLDING", parent="", own=1.00, acq="2008-03-01",
         goodwill_eur=0.0, coa="DE_SKR04", tax=0.30, region="DACH", erp="SAP_S4HANA",
         sales_eur_m=0.0, bu_mix={}, growth_act=0.0, dso=30, hist_rate=1.0,
         share_capital_eur=50e6, capital_reserve_eur=20e6),
    dict(code="DE02", name="Helix Pharma Produktion & Vertrieb GmbH", country="DE",
         country_name="Germany", city="Leipzig", ccy="EUR", etype="MANUFACTURING", parent="DE01",
         own=1.00, acq="2008-03-01", goodwill_eur=0.0, coa="DE_SKR04", tax=0.30, region="DACH",
         erp="SAP_S4HANA", sales_eur_m=120.0,
         bu_mix={"RX": 0.55, "OTC": 0.25, "MEDDEV": 0.08, "HERBAL": 0.12}, growth_act=0.055,
         dso=45, hist_rate=1.0, share_capital_eur=30e6, capital_reserve_eur=20e6),
    dict(code="ES01", name="Helix Pharma Iberia S.L.", country="ES", country_name="Spain",
         city="Torrejon de Ardoz", ccy="EUR", etype="SALES", parent="DE01", own=1.00,
         acq="2012-01-01", goodwill_eur=0.0, coa="ES_PGC", tax=0.25, region="Southern Europe",
         erp="SAP_ECC", sales_eur_m=55.0,
         bu_mix={"RX": 0.60, "OTC": 0.22, "MEDDEV": 0.10, "HERBAL": 0.08}, growth_act=0.072,
         dso=75, hist_rate=1.0, share_capital_eur=8e6, capital_reserve_eur=4e6),
    dict(code="IT01", name="Helix Pharma Italia S.r.l.", country="IT", country_name="Italy",
         city="Milan", ccy="EUR", etype="SALES", parent="DE01", own=1.00, acq="2013-06-01",
         goodwill_eur=0.0, coa="IT_PDC", tax=0.279, region="Southern Europe", erp="SAP_ECC",
         sales_eur_m=60.0, bu_mix={"RX": 0.58, "OTC": 0.20, "MEDDEV": 0.12, "HERBAL": 0.10},
         growth_act=0.035, dso=90, hist_rate=1.0, share_capital_eur=10e6, capital_reserve_eur=5e6),
    dict(code="CZ01", name="Helix Pharma CZ s.r.o.", country="CZ", country_name="Czech Republic",
         city="Prague", ccy="CZK", etype="SALES", parent="DE01", own=1.00, acq="2012-04-01",
         goodwill_eur=0.0, coa="CZ_UCT", tax=0.21, region="CEE", erp="SAP_ECC", sales_eur_m=25.0,
         bu_mix={"RX": 0.50, "OTC": 0.30, "MEDDEV": 0.08, "HERBAL": 0.12}, growth_act=0.081,
         dso=55, hist_rate=25.10, share_capital_eur=3e6, capital_reserve_eur=1e6),
    dict(code="CZ02", name="Helix Manufacturing Brno s.r.o.", country="CZ",
         country_name="Czech Republic", city="Brno", ccy="CZK", etype="CMO", parent="DE01",
         own=1.00, acq="2015-09-01", goodwill_eur=0.0, coa="CZ_UCT", tax=0.21, region="CEE",
         erp="SAP_ECC", sales_eur_m=0.0, bu_mix={}, growth_act=0.0, dso=30, hist_rate=27.00,
         share_capital_eur=5e6, capital_reserve_eur=3e6),
    dict(code="PL01", name="Helix Pharma Polska Sp. z o.o.", country="PL", country_name="Poland",
         city="Warsaw", ccy="PLN", etype="SALES", parent="DE01", own=0.80, acq="2019-07-01",
         goodwill_eur=8.0e6, coa="PL_ZPK", tax=0.19, region="CEE", erp="SAP_ECC",
         sales_eur_m=40.0, bu_mix={"RX": 0.52, "OTC": 0.28, "MEDDEV": 0.08, "HERBAL": 0.12},
         growth_act=0.064, dso=60, hist_rate=4.27, share_capital_eur=2e6, capital_reserve_eur=1.5e6),
    dict(code="HU01", name="Helix Pharma Hungary Kft.", country="HU", country_name="Hungary",
         city="Budapest", ccy="HUF", etype="SALES", parent="DE01", own=1.00, acq="2014-02-01",
         goodwill_eur=0.0, coa="HU_SZT", tax=0.09, region="CEE", erp="MS_DYNAMICS_NAV",
         sales_eur_m=18.0, bu_mix={"RX": 0.55, "OTC": 0.27, "MEDDEV": 0.06, "HERBAL": 0.12},
         growth_act=0.047, dso=60, hist_rate=310.0, share_capital_eur=2e6, capital_reserve_eur=1e6),
    dict(code="RO01", name="Helix Pharma Romania S.R.L.", country="RO", country_name="Romania",
         city="Bucharest", ccy="RON", etype="SALES", parent="DE01", own=1.00, acq="2016-05-01",
         goodwill_eur=0.0, coa="RO_PCG", tax=0.16, region="CEE", erp="MS_DYNAMICS_NAV",
         sales_eur_m=15.0, bu_mix={"RX": 0.60, "OTC": 0.25, "MEDDEV": 0.05, "HERBAL": 0.10},
         growth_act=0.095, dso=90, hist_rate=4.49, share_capital_eur=1.5e6, capital_reserve_eur=0.5e6),
    dict(code="GB01", name="Helix Pharma UK Ltd", country="GB", country_name="United Kingdom",
         city="London", ccy="GBP", etype="SALES", parent="DE01", own=1.00, acq="2011-10-01",
         goodwill_eur=0.0, coa="GB_UKGAAP", tax=0.25, region="UK & Nordics", erp="SAP_B1",
         sales_eur_m=35.0, bu_mix={"RX": 0.50, "OTC": 0.30, "MEDDEV": 0.12, "HERBAL": 0.08},
         growth_act=0.028, dso=45, hist_rate=0.87, share_capital_eur=2e6, capital_reserve_eur=1e6),
    dict(code="CH01", name="Helix Pharma Schweiz AG", country="CH", country_name="Switzerland",
         city="Zug", ccy="CHF", etype="SALES", parent="DE01", own=1.00, acq="2013-03-01",
         goodwill_eur=0.0, coa="CH_KMU", tax=0.14, region="DACH", erp="SAP_B1", sales_eur_m=22.0,
         bu_mix={"RX": 0.62, "OTC": 0.26, "MEDDEV": 0.12}, growth_act=0.041, dso=40,
         hist_rate=1.23, share_capital_eur=1e6, capital_reserve_eur=0.5e6),
    dict(code="SE01", name="Helix Pharma Nordic AB", country="SE", country_name="Sweden",
         city="Stockholm", ccy="SEK", etype="SALES", parent="DE01", own=1.00, acq="2017-01-01",
         goodwill_eur=0.0, coa="SE_BAS", tax=0.206, region="UK & Nordics", erp="SAP_B1",
         sales_eur_m=12.0, bu_mix={"RX": 0.58, "OTC": 0.30, "HERBAL": 0.12}, growth_act=0.052,
         dso=45, hist_rate=9.60, share_capital_eur=1e6, capital_reserve_eur=0.5e6),
]
ENTITY_BY_CODE = {e["code"]: e for e in ENTITIES}
SALES_ENTITIES = [e["code"] for e in ENTITIES if e["etype"] in ("SALES", "MANUFACTURING")]
# Subsidiaries distribute DIVIDEND_PAYOUT of prior-year net income every May. PL01 is 80 % owned,
# so 20 % of its dividend goes to the minority shareholder (a non-group, third-party cash outflow).
DIVIDEND_PAYERS = ["DE02", "ES01", "IT01", "CZ01", "PL01", "HU01", "RO01", "GB01", "CH01", "SE01"]
DIVIDEND_PAYOUT = 0.70
DIVIDEND_MONTH = 5
BUDGET_GROWTH = 0.06

# One-off (non-recurring) costs in the ACTUALS only. Group Controlling reclassifies them in the
# management view through raw/adjustments/management_adjustments.csv (layer MGMT_ADJ).
ONE_OFF_ITEMS = [
    dict(adjustment_id="MA01", company="DE01", year=2024, month=11, account="6380", dept="MGMT", bu="CORP",
         amount_eur=1_500_000, credit="2000", description="M&A due diligence - project Danube"),
    dict(adjustment_id="MA02", company="PL01", year=2024, month=9, account="5140", dept="LOG", bu="RX",
         amount_eur=450_000, credit="1340", description="Write-off of tender stock after loss of a hospital tender"),
    dict(adjustment_id="MA03", company="DE02", year=2025, month=2, account="6320", dept="PROD", bu="CORP",
         amount_eur=2_200_000, credit="2000", description="Flood damage repairs at the Leipzig site"),
    dict(adjustment_id="MA04", company="IT01", year=2025, month=6, account="6360", dept="MGMT", bu="CORP",
         amount_eur=600_000, credit="2000", description="Settlement of a pricing dispute with a regional health authority"),
]

# Local chart of accounts per country: (numbering width, prefix per group account class digit)
LOCAL_CHARTS = {
    "DE_SKR04":  dict(width=4, prefix={"1": "1", "2": "3", "3": "2", "4": "4", "5": "5", "6": "6", "7": "7", "8": "7"}),
    "ES_PGC":    dict(width=6, prefix={"1": "4", "2": "4", "3": "1", "4": "7", "5": "6", "6": "6", "7": "7", "8": "6"}),
    "IT_PDC":    dict(width=8, prefix={"1": "1", "2": "2", "3": "3", "4": "4", "5": "5", "6": "6", "7": "7", "8": "8"}),
    "CZ_UCT":    dict(width=6, prefix={"1": "2", "2": "3", "3": "4", "4": "6", "5": "5", "6": "5", "7": "6", "8": "5"}),
    "PL_ZPK":    dict(width=6, prefix={"1": "1", "2": "2", "3": "8", "4": "7", "5": "4", "6": "4", "7": "7", "8": "8"}),
    "HU_SZT":    dict(width=6, prefix={"1": "3", "2": "4", "3": "4", "4": "9", "5": "5", "6": "5", "7": "9", "8": "8"}),
    "RO_PCG":    dict(width=6, prefix={"1": "4", "2": "4", "3": "1", "4": "7", "5": "6", "6": "6", "7": "7", "8": "6"}),
    "GB_UKGAAP": dict(width=5, prefix={"1": "1", "2": "2", "3": "3", "4": "4", "5": "5", "6": "6", "7": "7", "8": "8"}),
    "CH_KMU":    dict(width=4, prefix={"1": "1", "2": "2", "3": "2", "4": "3", "5": "4", "6": "6", "7": "6", "8": "8"}),
    "SE_BAS":    dict(width=4, prefix={"1": "1", "2": "2", "3": "2", "4": "3", "5": "4", "6": "6", "7": "8", "8": "8"}),
}
LOCAL_SPLIT_SHARE = 0.15       # share of eligible group accounts split into two local accounts
LOCAL_SPLIT_RATIO = 0.65       # first local account receives 65 % of the amount

# --------------------------------------------------------------------------- #
# Departments -> cost centres. function_before / function_after implement the
# SCD Type 2 reorganisation: Medical Affairs moves from R&D to Sales & Marketing
# on REORG_DATE.
# --------------------------------------------------------------------------- #
REORG_DATE = "2024-07-01"
DEPARTMENTS = {
    # dept:   (name,                         function,  types the dept exists in)
    "SALES":  ("Sales force",                 "S&M",  {"SALES", "MANUFACTURING"}),
    "MKT":    ("Marketing",                   "S&M",  {"SALES", "MANUFACTURING"}),
    "KAM":    ("Key account & market access", "S&M",  {"SALES", "MANUFACTURING"}),
    "MEDAFF": ("Medical affairs",             "R&D",  {"SALES", "MANUFACTURING"}),   # SCD2 -> S&M
    "REG":    ("Regulatory affairs",          "R&D",  {"SALES", "MANUFACTURING"}),
    "PV":     ("Pharmacovigilance",           "R&D",  {"SALES", "MANUFACTURING"}),
    "RND":    ("R&D centre",                  "R&D",  {"MANUFACTURING"}),
    "PROD":   ("Production",                  "COGS", {"MANUFACTURING", "CMO"}),
    "QA":     ("Quality assurance & control", "COGS", {"MANUFACTURING", "CMO"}),
    "LOG":    ("Logistics & supply chain",    "COGS", {"SALES", "MANUFACTURING"}),
    "FIN":    ("Finance & controlling",       "G&A",  {"HOLDING", "SALES", "MANUFACTURING", "CMO"}),
    "HR":     ("Human resources",             "G&A",  {"HOLDING", "SALES", "MANUFACTURING"}),
    "IT":     ("IT",                          "G&A",  {"HOLDING", "SALES", "MANUFACTURING"}),
    "MGMT":   ("Management",                  "G&A",  {"HOLDING", "SALES", "MANUFACTURING", "CMO"}),
    "GRP":    ("Group functions",             "G&A",  {"HOLDING"}),
}

# Operating expense pools: share of third-party net sales per function, plus the
# account mix inside each pool as {account: (weight, department or "*personnel")}.
# "*" means the amount is split across all departments of that function.
OPEX_RATIO = {
    "SALES":         {"S&M": 0.215, "R&D": 0.030, "G&A": 0.065},
    "MANUFACTURING": {"S&M": 0.175, "R&D": 0.060, "G&A": 0.040},
}
OPEX_POOLS = {
    "S&M": {
        "6000": (0.22, "*"), "6020": (0.05, "*"), "6030": (0.06, "*"), "6040": (0.02, "*"),
        "6050": (0.05, "SALES"), "6060": (0.01, "*"),
        "6100": (0.09, "SALES"), "6110": (0.08, "MKT"), "6120": (0.13, "MKT"),
        "6130": (0.07, "MKT"), "6140": (0.05, "MKT"), "6150": (0.04, "SALES"),
        "6160": (0.03, "MKT"), "6170": (0.05, "KAM"), "6300": (0.02, "SALES"),
        "6340": (0.02, "SALES"), "6430": (0.01, "KAM"),
    },
    "R&D": {
        "6000": (0.28, "*"), "6020": (0.04, "*"), "6030": (0.07, "*"), "6040": (0.02, "*"),
        "6060": (0.02, "*"), "6200": (0.10, "REG"), "6210": (0.10, "MEDAFF"),
        "6220": (0.14, "REG"), "6230": (0.12, "PV"), "6250": (0.08, "MEDAFF"),
        "6110": (0.03, "MEDAFF"),
    },
    "G&A": {
        "6000": (0.30, "*"), "6020": (0.05, "*"), "6030": (0.07, "*"), "6040": (0.02, "*"),
        "6060": (0.02, "HR"), "6300": (0.10, "FIN"), "6310": (0.04, "FIN"),
        "6320": (0.03, "FIN"), "6330": (0.04, "FIN"), "6340": (0.10, "IT"),
        "6360": (0.04, "MGMT"), "6370": (0.06, "FIN"), "6380": (0.07, "MGMT"),
        "6400": (0.01, "FIN"), "6430": (0.05, "FIN"),
    },
}
PERSONNEL_ACCOUNTS = {"6000", "6010", "6020", "6030", "6040", "6050", "6060",
                      "5100"}

# --------------------------------------------------------------------------- #
# Group chart of accounts: exactly 120 accounts.
# (account, name, fs_group, fs_line, translation_method, cashflow_line,
#  ic_pair_type, ic_side, consolidation_only)
# translation_method:
#   CLOSING        balance sheet balance at period-end closing rate
#   AVG            P&L monthly movement at monthly average rate (YTD = sum)
#   HISTORICAL     equity at historical rate (fx_historical.csv)
#   HISTORICAL_TXN equity movement at the average rate of the month it was booked
#   CARRYFORWARD   derived: prior fiscal years' translated results (no postings)
#   CONSOLIDATION  only posted by the consolidation engine (CTA, NCI, goodwill)
# --------------------------------------------------------------------------- #
GROUP_ACCOUNTS = [
    # ---------------- Current assets ----------------
    ("1000", "Cash at bank - operating accounts", "Current assets", "Cash and cash equivalents", "CLOSING", "CASH", "", "", False),
    ("1010", "Cash at bank - payroll account", "Current assets", "Cash and cash equivalents", "CLOSING", "CASH", "", "", False),
    ("1100", "Trade receivables - pharmacies & other", "Current assets", "Trade receivables", "CLOSING", "OP_WC_RECEIVABLES", "", "", False),
    ("1110", "Trade receivables - wholesalers", "Current assets", "Trade receivables", "CLOSING", "OP_WC_RECEIVABLES", "", "", False),
    ("1120", "Trade receivables - hospitals & tenders", "Current assets", "Trade receivables", "CLOSING", "OP_WC_RECEIVABLES", "", "", False),
    ("1150", "Allowance for doubtful receivables", "Current assets", "Trade receivables", "CLOSING", "OP_WC_RECEIVABLES", "", "", False),
    ("1190", "Receivables from group companies", "Current assets", "Intercompany receivables", "CLOSING", "OP_WC_RECEIVABLES", "AR_AP", "A", False),
    ("1200", "Other receivables", "Current assets", "Other current assets", "CLOSING", "OP_OTHER", "", "", False),
    ("1220", "Prepaid expenses", "Current assets", "Other current assets", "CLOSING", "OP_OTHER", "", "", False),
    ("1300", "Raw materials (APIs & excipients)", "Current assets", "Inventories", "CLOSING", "OP_WC_INVENTORY", "", "", False),
    ("1310", "Packaging materials", "Current assets", "Inventories", "CLOSING", "OP_WC_INVENTORY", "", "", False),
    ("1330", "Finished goods - own production", "Current assets", "Inventories", "CLOSING", "OP_WC_INVENTORY", "", "", False),
    ("1340", "Finished goods - purchased from group companies", "Current assets", "Inventories", "CLOSING", "OP_WC_INVENTORY", "UPI_BASE", "", False),
    ("1350", "Merchandise - third-party products", "Current assets", "Inventories", "CLOSING", "OP_WC_INVENTORY", "", "", False),
    ("1390", "Allowance for obsolete & short-dated stock", "Current assets", "Inventories", "CLOSING", "OP_WC_INVENTORY", "", "", False),
    # ---------------- Non-current assets ----------------
    ("1400", "Loans to group companies", "Non-current assets", "Intercompany loans", "CLOSING", "INV_IC_LOANS", "LOAN", "A", False),
    ("1500", "Land", "Non-current assets", "Property, plant & equipment", "CLOSING", "INV_CAPEX_PPE", "", "", False),
    ("1510", "Buildings", "Non-current assets", "Property, plant & equipment", "CLOSING", "INV_CAPEX_PPE", "", "", False),
    ("1520", "Plant & machinery", "Non-current assets", "Property, plant & equipment", "CLOSING", "INV_CAPEX_PPE", "", "", False),
    ("1530", "Laboratory equipment", "Non-current assets", "Property, plant & equipment", "CLOSING", "INV_CAPEX_PPE", "", "", False),
    ("1540", "IT hardware", "Non-current assets", "Property, plant & equipment", "CLOSING", "INV_CAPEX_PPE", "", "", False),
    ("1550", "Vehicles", "Non-current assets", "Property, plant & equipment", "CLOSING", "INV_CAPEX_PPE", "", "", False),
    ("1560", "Assets under construction", "Non-current assets", "Property, plant & equipment", "CLOSING", "INV_CAPEX_PPE", "", "", False),
    ("1570", "Right-of-use assets (IFRS 16)", "Non-current assets", "Right-of-use assets", "CLOSING", "OP_DA_ADDBACK", "", "", False),
    ("1590", "Accumulated depreciation - PP&E", "Non-current assets", "Property, plant & equipment", "CLOSING", "OP_DA_ADDBACK", "", "", False),
    ("1600", "Marketing authorisations & product licences", "Non-current assets", "Intangible assets", "CLOSING", "INV_CAPEX_INTANGIBLES", "", "", False),
    ("1610", "Software", "Non-current assets", "Intangible assets", "CLOSING", "INV_CAPEX_INTANGIBLES", "", "", False),
    ("1690", "Accumulated amortisation - intangible assets", "Non-current assets", "Intangible assets", "CLOSING", "OP_DA_ADDBACK", "", "", False),
    ("1700", "Goodwill", "Non-current assets", "Goodwill", "CONSOLIDATION", "", "", "", True),
    ("1800", "Investments in subsidiaries", "Non-current assets", "Financial assets", "CLOSING", "INV_INVESTMENTS", "INVESTMENT", "A", False),
    ("1850", "Deferred tax assets", "Non-current assets", "Deferred tax assets", "CLOSING", "OP_TAX", "", "", False),
    # ---------------- Current liabilities ----------------
    ("2000", "Trade payables - third parties", "Current liabilities", "Trade payables", "CLOSING", "OP_WC_PAYABLES", "", "", False),
    ("2090", "Payables to group companies", "Current liabilities", "Intercompany payables", "CLOSING", "OP_WC_PAYABLES", "AR_AP", "B", False),
    ("2100", "Accrued personnel costs (bonus, vacation)", "Current liabilities", "Accruals", "CLOSING", "OP_PROVISIONS_ACCRUALS", "", "", False),
    ("2110", "Payroll taxes & social security payable", "Current liabilities", "Other current liabilities", "CLOSING", "OP_PROVISIONS_ACCRUALS", "", "", False),
    ("2130", "Income tax payable", "Current liabilities", "Tax liabilities", "CLOSING", "OP_TAX", "", "", False),
    ("2140", "Accrued statutory rebates & chargebacks", "Current liabilities", "Gross-to-net accruals", "CLOSING", "OP_PROVISIONS_ACCRUALS", "", "", False),
    ("2150", "Returns provision", "Current liabilities", "Gross-to-net accruals", "CLOSING", "OP_PROVISIONS_ACCRUALS", "", "", False),
    ("2160", "Other accrued expenses", "Current liabilities", "Accruals", "CLOSING", "OP_PROVISIONS_ACCRUALS", "", "", False),
    ("2310", "Bank loans - current portion", "Current liabilities", "Borrowings", "CLOSING", "FIN_DEBT", "", "", False),
    # ---------------- Non-current liabilities ----------------
    ("2200", "Loans from group companies", "Non-current liabilities", "Intercompany loans", "CLOSING", "FIN_IC_LOANS", "LOAN", "B", False),
    ("2300", "Bank loans - non-current", "Non-current liabilities", "Borrowings", "CLOSING", "FIN_DEBT", "", "", False),
    ("2320", "Lease liabilities (IFRS 16)", "Non-current liabilities", "Lease liabilities", "CLOSING", "FIN_LEASES", "", "", False),
    ("2400", "Pension provisions", "Non-current liabilities", "Provisions", "CLOSING", "OP_PROVISIONS_ACCRUALS", "", "", False),
    ("2500", "Deferred tax liabilities", "Non-current liabilities", "Deferred tax liabilities", "CLOSING", "OP_TAX", "", "", False),
    # ---------------- Equity ----------------
    ("3000", "Share capital", "Equity", "Share capital", "HISTORICAL", "FIN_EQUITY", "", "", False),
    ("3100", "Capital reserve", "Equity", "Reserves", "HISTORICAL", "FIN_EQUITY", "", "", False),
    ("3200", "Retained earnings - brought forward at migration", "Equity", "Retained earnings", "HISTORICAL", "", "", "", False),
    ("3210", "Retained earnings - prior fiscal years' results (system)", "Equity", "Retained earnings", "CARRYFORWARD", "", "", "", False),
    ("3300", "Dividends distributed", "Equity", "Retained earnings", "HISTORICAL_TXN", "FIN_DIVIDENDS", "DIVIDEND", "B", False),
    ("3900", "Currency translation reserve (CTA)", "Equity", "Currency translation reserve", "CONSOLIDATION", "", "", "", True),
    ("3950", "Non-controlling interests", "Equity", "Non-controlling interests", "CONSOLIDATION", "", "", "", True),
    # ---------------- Revenue ----------------
    ("4000", "Gross sales - Rx (prescription medicines)", "Revenue", "Gross sales", "AVG", "", "", "", False),
    ("4001", "Gross sales - OTC", "Revenue", "Gross sales", "AVG", "", "", "", False),
    ("4002", "Gross sales - Medical devices", "Revenue", "Gross sales", "AVG", "", "", "", False),
    ("4003", "Gross sales - Herbal products", "Revenue", "Gross sales", "AVG", "", "", "", False),
    ("4050", "Statutory rebates (health insurance)", "Revenue", "Gross-to-net deductions", "AVG", "", "", "", False),
    ("4060", "Wholesaler discounts", "Revenue", "Gross-to-net deductions", "AVG", "", "", "", False),
    ("4070", "Returns", "Revenue", "Gross-to-net deductions", "AVG", "", "", "", False),
    ("4080", "Tender & hospital discounts", "Revenue", "Gross-to-net deductions", "AVG", "", "", "", False),
    ("4100", "Intercompany sales of goods", "Revenue", "Intercompany revenue", "AVG", "", "SALES_COGS", "A", False),
    ("4300", "Intercompany service revenue - management fees", "Revenue", "Intercompany revenue", "AVG", "", "MGMT_FEE", "A", False),
    ("4400", "Other operating income", "Revenue", "Other operating income", "AVG", "", "", "", False),
    # ---------------- Cost of sales ----------------
    ("5000", "Raw materials consumed", "Cost of sales", "Materials", "AVG", "", "", "", False),
    ("5010", "Intercompany purchases of goods", "Cost of sales", "Intercompany purchases", "AVG", "", "SALES_COGS", "B", False),
    ("5020", "Purchases of merchandise - third parties", "Cost of sales", "Materials", "AVG", "", "", "", False),
    ("5030", "Packaging materials consumed", "Cost of sales", "Materials", "AVG", "", "", "", False),
    ("5090", "Change in inventory - goods purchased from group", "Cost of sales", "Change in inventories", "AVG", "", "", "", False),
    ("5100", "Direct labour - production", "Cost of sales", "Production costs", "AVG", "", "", "", False),
    ("5110", "Manufacturing overhead", "Cost of sales", "Production costs", "AVG", "", "", "", False),
    ("5120", "Quality control & batch release", "Cost of sales", "Production costs", "AVG", "", "", "", False),
    ("5130", "Freight & distribution", "Cost of sales", "Distribution costs", "AVG", "", "", "", False),
    ("5140", "Inventory write-offs (expiry & damage)", "Cost of sales", "Inventory write-offs", "AVG", "", "", "", False),
    ("5150", "Change in inventory - own finished goods", "Cost of sales", "Change in inventories", "AVG", "", "", "", False),
    # ---------------- Operating expenses ----------------
    ("6000", "Salaries", "Operating expenses", "Personnel expenses", "AVG", "", "", "", False),
    ("6010", "Wages", "Operating expenses", "Personnel expenses", "AVG", "", "", "", False),
    ("6020", "Bonuses & incentives", "Operating expenses", "Personnel expenses", "AVG", "", "", "", False),
    ("6030", "Social security contributions", "Operating expenses", "Personnel expenses", "AVG", "", "", "", False),
    ("6040", "Pension costs", "Operating expenses", "Personnel expenses", "AVG", "", "", "", False),
    ("6050", "Company cars", "Operating expenses", "Personnel expenses", "AVG", "", "", "", False),
    ("6060", "Training & development", "Operating expenses", "Personnel expenses", "AVG", "", "", "", False),
    ("6100", "Sales force travel & expenses", "Operating expenses", "Selling & marketing expenses", "AVG", "", "", "", False),
    ("6110", "Medical congresses & scientific events", "Operating expenses", "Selling & marketing expenses", "AVG", "", "", "", False),
    ("6120", "Consumer advertising (OTC)", "Operating expenses", "Selling & marketing expenses", "AVG", "", "", "", False),
    ("6130", "Digital marketing", "Operating expenses", "Selling & marketing expenses", "AVG", "", "", "", False),
    ("6140", "Promotional material", "Operating expenses", "Selling & marketing expenses", "AVG", "", "", "", False),
    ("6150", "Product samples", "Operating expenses", "Selling & marketing expenses", "AVG", "", "", "", False),
    ("6160", "Market research", "Operating expenses", "Selling & marketing expenses", "AVG", "", "", "", False),
    ("6170", "KOL & speaker fees", "Operating expenses", "Selling & marketing expenses", "AVG", "", "", "", False),
    ("6200", "External R&D services (CRO)", "Operating expenses", "R&D, regulatory & medical expenses", "AVG", "", "", "", False),
    ("6210", "Clinical & non-interventional studies", "Operating expenses", "R&D, regulatory & medical expenses", "AVG", "", "", "", False),
    ("6220", "Regulatory fees & marketing authorisation renewals", "Operating expenses", "R&D, regulatory & medical expenses", "AVG", "", "", "", False),
    ("6230", "Pharmacovigilance services", "Operating expenses", "R&D, regulatory & medical expenses", "AVG", "", "", "", False),
    ("6250", "Medical information services", "Operating expenses", "R&D, regulatory & medical expenses", "AVG", "", "", "", False),
    ("6300", "Rent & short-term leases", "Operating expenses", "General & administrative expenses", "AVG", "", "", "", False),
    ("6310", "Utilities & energy", "Operating expenses", "General & administrative expenses", "AVG", "", "", "", False),
    ("6320", "Maintenance & repairs", "Operating expenses", "General & administrative expenses", "AVG", "", "", "", False),
    ("6330", "Insurance", "Operating expenses", "General & administrative expenses", "AVG", "", "", "", False),
    ("6340", "IT licences & cloud services", "Operating expenses", "General & administrative expenses", "AVG", "", "", "", False),
    ("6360", "Legal fees", "Operating expenses", "General & administrative expenses", "AVG", "", "", "", False),
    ("6370", "Audit & tax advisory", "Operating expenses", "General & administrative expenses", "AVG", "", "", "", False),
    ("6380", "Consulting fees", "Operating expenses", "General & administrative expenses", "AVG", "", "", "", False),
    ("6400", "Bank charges", "Operating expenses", "General & administrative expenses", "AVG", "", "", "", False),
    ("6430", "Other administrative expenses", "Operating expenses", "General & administrative expenses", "AVG", "", "", "", False),
    ("6800", "Intercompany management fees", "Operating expenses", "Intercompany charges", "AVG", "", "MGMT_FEE", "B", False),
    ("6910", "Bad debt expense", "Operating expenses", "Other operating expenses", "AVG", "", "", "", False),
    # ---------------- Depreciation & amortisation ----------------
    ("6950", "Depreciation - buildings", "Depreciation & amortisation", "Depreciation", "AVG", "", "", "", False),
    ("6960", "Depreciation - plant, machinery & lab equipment", "Depreciation & amortisation", "Depreciation", "AVG", "", "", "", False),
    ("6970", "Depreciation - IT & vehicles", "Depreciation & amortisation", "Depreciation", "AVG", "", "", "", False),
    ("6980", "Amortisation - intangible assets", "Depreciation & amortisation", "Amortisation", "AVG", "", "", "", False),
    ("6990", "Depreciation - right-of-use assets", "Depreciation & amortisation", "Depreciation", "AVG", "", "", "", False),
    # ---------------- Financial result ----------------
    ("7000", "Interest income - third parties", "Financial result", "Interest income", "AVG", "", "", "", False),
    ("7010", "Intercompany interest income", "Financial result", "Interest income", "AVG", "", "INTEREST", "A", False),
    ("7100", "Intercompany dividend income", "Financial result", "Dividend income", "AVG", "", "DIVIDEND", "A", False),
    ("7200", "Interest expense - bank & overdraft", "Financial result", "Interest expense", "AVG", "", "", "", False),
    ("7210", "Intercompany interest expense", "Financial result", "Interest expense", "AVG", "", "INTEREST", "B", False),
    ("7220", "Interest expense - leases", "Financial result", "Interest expense", "AVG", "", "", "", False),
    ("7300", "Foreign exchange gains & losses", "Financial result", "FX result", "AVG", "", "", "", False),
    # ---------------- Income tax ----------------
    ("8000", "Current income tax", "Income tax", "Income tax", "AVG", "", "", "", False),
    ("8100", "Deferred tax", "Income tax", "Income tax", "AVG", "", "", "", False),
]
ACCOUNT_INDEX = {a[0]: a for a in GROUP_ACCOUNTS}

# Intercompany matching pairs: pair type -> (side A account, side B account, basis)
IC_PAIRS = {
    "SALES_COGS": ("4100", "5010", "PL"),
    "MGMT_FEE":   ("4300", "6800", "PL"),
    "INTEREST":   ("7010", "7210", "PL"),
    "DIVIDEND":   ("7100", "3300", "PL"),   # 3300 is equity but translated at transaction rate
    "AR_AP":      ("1190", "2090", "BS"),
    "LOAN":       ("1400", "2200", "BS"),
}


def is_balance_sheet(account: str) -> bool:
    """Accounts starting with 1, 2 or 3 are balance sheet accounts."""
    return account[0] in "123"


def account_class(account: str) -> str:
    return {"1": "ASSET", "2": "LIABILITY", "3": "EQUITY"}.get(
        account[0], "REVENUE" if account[0] == "4" else "EXPENSE")

# --------------------------------------------------------------------------- #
# Data-quality rule catalogue (implemented in Snowflake in Batch 2)
# --------------------------------------------------------------------------- #
DQ_RULES = [
    # rule_id, rule_name,               severity,   layer,         description
    ("DQ01", "TB_BALANCED",            "BLOCKING", "STAGING",     "Local trial balance nets to zero per entity and period"),
    ("DQ02", "ACCOUNT_MAPPED",         "BLOCKING", "STAGING",     "Every local G/L account maps to a group account"),
    ("DQ03", "FX_COMPLETE",            "BLOCKING", "INTEGRATION", "Average and closing rate exist for every currency and period"),
    ("DQ04", "FX_PLAUSIBLE",           "WARNING",  "INTEGRATION", "Closing/average rate moves less than 10 % versus prior month"),
    ("DQ05", "SUBMISSION_COMPLETE",    "BLOCKING", "STAGING",     "All 12 entities delivered a trial balance for the period"),
    ("DQ06", "NO_DUPLICATES",          "WARNING",  "STAGING",     "No exact duplicate rows (row hash) within a delivery"),
    ("DQ07", "PARTNER_VALID",          "BLOCKING", "STAGING",     "Trading partner is an existing group company and not the entity itself"),
    ("DQ08", "IC_MATCHED",             "WARNING",  "INTEGRATION", "Intercompany pairs match within EUR 1,000 or 0.5 %"),
    ("DQ09", "ELIMINATIONS_BALANCED",  "BLOCKING", "INTEGRATION", "Elimination journal nets to zero per period"),
    ("DQ10", "BS_BALANCED",            "BLOCKING", "MARTS",       "Consolidated assets = liabilities + equity (incl. NCI and CTA)"),
    ("DQ11", "CASHFLOW_RECONCILED",    "BLOCKING", "MARTS",       "Cash-flow statement reconciles to the balance-sheet cash movement"),
    ("DQ12", "SOURCE_TO_REPORT",       "BLOCKING", "MARTS",       "Row count and amount control totals match the source manifest"),
]
DQ_RULE_INDEX = {r[0]: r for r in DQ_RULES}
