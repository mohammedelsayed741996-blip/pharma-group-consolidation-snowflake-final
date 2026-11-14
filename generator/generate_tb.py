"""
Simulate 36 monthly closes for the 12 Helix Pharma entities.

The simulator works like a tiny ERP: every business event is posted as a *balanced*
journal entry (debit = +, credit = -) in integer cents of the entity's local currency.
Because each journal nets to zero, every entity's trial balance nets to zero for
every period by construction.

Simulated events
----------------
* Third-party sales by business unit with seasonality, growth, market shocks and
  gross-to-net deductions (on-invoice discounts plus accrued rebates and returns).
* Intercompany (IC) flows, all invoiced in EUR:
    - DE02 (factory) sells finished goods to 9 sales companies at cost + 15 %
    - CZ02 (contract manufacturer) invoices DE02 at cost + 8 %
    - DE01 (holding) charges a 2 % management fee to every selling entity
    - DE01 lends to PL01 (opening) and RO01 (drawdown 2024-07), monthly interest
    - ES01, IT01, GB01, CH01 pay dividends to DE01 every May
* Settlement of IC balances, month-end revaluation of EUR-denominated IC balances in
  non-EUR entities (realistic FX gains/losses), production, inventory build, OPEX by
  cost centre, CAPEX incl. assets under construction, depreciation, IFRS 16 leases,
  pensions, bank debt, cash interest and monthly income tax.

Outputs (written below <data_dir>):
    raw/tb/tb_<company>_<yyyymm>.csv    one trial-balance movement file per entity and period
                                        (period 00 of 2023 = opening balances / carry-forward)
    raw/ic/ic_transactions.csv          IC sub-ledger: one row per IC document
    raw/budget/budget_<yyyy>.csv        P&L budget on group accounts (version BUD)
    raw/adjustments/management_adjustments.csv   non-recurring items reclassified in the management view

Run standalone (needs master data first):  python generator/generate_tb.py --out data
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

if __package__:                        # imported as part of the generator package
    from . import config as cfg
else:                                  # executed as a script: python generator/<file>.py
    import config as cfg               # type: ignore

# Personnel cost split across the departments of each function (renormalised per entity)
PERSONNEL_DEPT_WEIGHTS = {
    "S&M": {"SALES": 0.60, "MKT": 0.25, "KAM": 0.15},
    "R&D": {"MEDAFF": 0.35, "REG": 0.25, "PV": 0.20, "RND": 0.20},
    "G&A": {"FIN": 0.35, "HR": 0.15, "IT": 0.20, "MGMT": 0.20, "GRP": 0.10},
}
# DE02 production cost structure (share of production cost at standard)
PRODUCTION_SPLIT = {"CZ02": 0.20, "5000": 0.35, "5030": 0.08, "5100": 0.20, "5110": 0.10, "5120": 0.07}
CZ02_COST_SPLIT = {"5100": 0.40, "5000": 0.35, "5110": 0.15, "5120": 0.10}
DEPRECIATION = {  # asset account: (useful life in years, D&A account)
    "1510": (30, "6950"), "1520": (12, "6960"), "1530": (8, "6960"),
    "1540": (5, "6970"), "1550": (6, "6970"),
}
AMORTISATION = {"1600": (15, "6980"), "1610": (5, "6980")}


# --------------------------------------------------------------------------- #
# Small numeric helpers (all money is handled as integer cents)
# --------------------------------------------------------------------------- #
def cents(x: float) -> int:
    return int(round(x * 100))


def split_cents(total: int, weights) -> list[int]:
    """Split an integer amount by weights so that the parts sum exactly to the total."""
    w = np.asarray(list(weights), dtype=float)
    w = w / w.sum()
    raw = total * w
    base = np.floor(raw).astype(np.int64)
    remainder = int(total - base.sum())
    order = np.argsort(-(raw - base))
    base[order[:remainder]] += 1
    return [int(v) for v in base]


class Simulator:
    """Event-driven general-ledger simulator for one scenario ('ACT' or 'BUD')."""

    def __init__(self, fx_rates: pd.DataFrame, fx_budget: pd.DataFrame, scenario: str, seed: int):
        assert scenario in ("ACT", "BUD")
        self.scenario = scenario
        self.noise_on = scenario == "ACT"
        self.rng = np.random.default_rng(seed + (11 if scenario == "ACT" else 22))
        self.E = cfg.ENTITY_BY_CODE
        self.depts = {e["code"]: {d for d, (_n, _f, types) in cfg.DEPARTMENTS.items() if e["etype"] in types}
                      for e in cfg.ENTITIES}
        # FX lookups --------------------------------------------------------
        self.fx_avg = {(r.currency, r.period_id): float(r.avg_rate) for r in fx_rates.itertuples()}
        self.fx_close = {(r.currency, r.period_id): float(r.closing_rate) for r in fx_rates.itertuples()}
        self.fx_bud = {(r.currency, int(r.fiscal_year)): float(r.budget_rate) for r in fx_budget.itertuples()}
        # Ledger state ------------------------------------------------------
        self.rows: list[tuple] = []                       # posted lines
        self.bal: dict[tuple, int] = defaultdict(int)     # (entity, account, partner) -> LC cents (BS)
        self.eur: dict[tuple, int] = defaultdict(int)     # EUR-denominated IC balances in EUR cents
        self.pl_month: dict[str, int] = defaultdict(int)  # P&L of current month per entity
        self.div_month: dict[str, int] = defaultdict(int) # dividend income of current month (tax-exempt)
        self.net_income: dict[tuple, int] = defaultdict(int)   # (entity, year) -> NI in LC cents
        self.ic_docs: list[dict] = []
        self.doc_seq = 0
        # Operating state ---------------------------------------------------
        self.inv_ic: dict[tuple, int] = defaultdict(int)  # (entity, bu) -> IC inventory in EUR cents
        self.fg_eur = 0                                    # DE02 finished goods at standard (EUR cents)
        self.raw_lc = 0                                    # DE02 raw material inventory (LC cents)
        self.lease: dict[str, dict] = {}
        self.net_sales_eur: dict[str, int] = {}            # third-party net sales of the month (EUR cents)
        self.ic_purchases: dict[tuple, int] = {}           # (buyer, bu) -> EUR cents purchased this month
        self.domestic_need: dict[str, int] = {}            # DE02 own-production standard cost by BU

    # ------------------------------------------------------------------ #
    # FX helpers
    # ------------------------------------------------------------------ #
    def _ccy(self, e: str) -> str:
        return self.E[e]["ccy"]

    def avg(self, e: str, y: int, m: int) -> float:
        if self._ccy(e) == "EUR":
            return 1.0
        if self.scenario == "BUD":
            return self.fx_bud[(self._ccy(e), max(y, cfg.FIRST_YEAR))]
        return self.fx_avg[(self._ccy(e), cfg.fx_period_for(y, m))]

    def close(self, e: str, y: int, m: int) -> float:
        if self._ccy(e) == "EUR":
            return 1.0
        if self.scenario == "BUD":
            return self.fx_bud[(self._ccy(e), max(y, cfg.FIRST_YEAR))]
        if m == 0:
            return self.fx_close[(self._ccy(e), cfg.OPENING_FX_PERIOD)]
        return self.fx_close[(self._ccy(e), cfg.period_id(y, m))]

    def opening_rate(self, e: str) -> float:
        """Closing rate of Dec 2022 (actual) - anchors local-currency price levels in both scenarios."""
        if self._ccy(e) == "EUR":
            return 1.0
        return self.fx_close[(self._ccy(e), cfg.OPENING_FX_PERIOD)]

    @staticmethod
    def to_lc(eur_cents: int, rate: float) -> int:
        return int(round(eur_cents * rate))

    def noise(self, sigma: float) -> float:
        if not self.noise_on:
            return 1.0
        return float(np.exp(self.rng.normal(0.0, sigma) - sigma ** 2 / 2))

    # ------------------------------------------------------------------ #
    # Posting
    # ------------------------------------------------------------------ #
    @staticmethod
    def cc(e: str, dept: str) -> str:
        return f"CC-{e}-{dept}"

    @staticmethod
    def pc(e: str, bu: str) -> str:
        return f"PC-{e}-{bu}"

    def journal(self, e: str, y: int, m: int, lines: list[tuple]) -> None:
        """Post a balanced journal. lines = [(account, cents, partner, cost_center, profit_center)]."""
        lines = [ln for ln in lines if ln[1] != 0]
        total = sum(ln[1] for ln in lines)
        if total != 0:
            raise ValueError(f"Unbalanced journal {e} {y}-{m}: {total} cents\n{lines}")
        for acct, amt, partner, cc, pc in lines:
            self.rows.append((e, y, m, acct, partner, cc, pc, amt))
            if cfg.is_balance_sheet(acct):
                self.bal[(e, acct, partner)] += amt
            else:
                self.pl_month[e] += amt
                if acct == "7100":
                    self.div_month[e] += amt

    def growth(self, e: str, y: int, m: int) -> float:
        t = (y - cfg.FIRST_YEAR) * 12 + m
        g = self.E[e]["growth_act"] if self.scenario == "ACT" else cfg.BUDGET_GROWTH
        return (1 + g) ** (t / 12)

    def base_gross_lc(self, e: str, bu: str) -> float:
        """Monthly gross sales (LC) at the 2022 run-rate, before growth and seasonality."""
        ent = self.E[e]
        return ent["sales_eur_m"] * 1e6 * ent["bu_mix"][bu] / 12 * self.opening_rate(e)

    def shock(self, e: str, bu: str, y: int, m: int) -> float:
        """Market events that the budget did not foresee (actuals only)."""
        if self.scenario != "ACT":
            return 1.0
        f = 1.0
        if e == "PL01" and bu == "RX" and y == 2024 and m >= 7:
            f *= 0.85                     # lost hospital tender in Poland, H2 2024
        if bu == "OTC" and y == 2025 and m in (1, 2):
            f *= 1.20                     # strong cold & flu season, Q1 2025
        if e == "IT01" and bu == "RX" and (y, m) >= (2025, 4):
            f *= 0.95                     # Italian Rx price cut from April 2025
        return f

    # ------------------------------------------------------------------ #
    # Opening balance sheet (period 0)
    # ------------------------------------------------------------------ #
    def _gtn_total(self, bu: str) -> float:
        return sum(cfg.GTN_RATES[bu].values())

    def opening_balances(self) -> None:
        y, m = cfg.OPENING_PERIOD
        cons0: dict[tuple, int] = {}          # (entity, bu) -> monthly IC consumption (EUR cents)
        net0: dict[str, float] = {}           # monthly net sales LC
        for e in cfg.SALES_ENTITIES:
            net0[e] = sum(self.base_gross_lc(e, bu) * (1 - self._gtn_total(bu)) for bu in self.E[e]["bu_mix"])
            for bu in self.E[e]["bu_mix"]:
                if bu in cfg.IC_SOURCED_BUS:
                    net_bu = self.base_gross_lc(e, bu) * (1 - self._gtn_total(bu))
                    cons0[(e, bu)] = cents(net_bu / self.opening_rate(e) * cfg.COGS_RATIO[bu])
        buyers = [e for e in cfg.SALES_ENTITIES if e != "DE02"]
        de02_ic_need = sum(v for (e, _bu), v in cons0.items() if e != "DE02") / (1 + cfg.DE02_MARKUP)
        de02_dom_need = sum(v for (e, _bu), v in cons0.items() if e == "DE02") / (1 + cfg.DE02_MARKUP)
        need0 = int(de02_ic_need + de02_dom_need)
        cz_rev0 = int(need0 * cfg.CZ02_SHARE_OF_PRODUCTION)
        fee0 = {e: cents(net0[e] / self.opening_rate(e) * cfg.MGMT_FEE_RATE) for e in cfg.SALES_ENTITIES}
        # IC goods payables (paid 50 % per month -> steady-state balance = 2 months of purchases)
        ic_pay0: dict[str, int] = {}
        for b in buyers:
            ic_pay0[b] = sum(2 * cons0[(b, bu)] for bu in self.E[b]["bu_mix"] if (b, bu) in cons0)
            for bu in self.E[b]["bu_mix"]:
                if (b, bu) in cons0:
                    self.inv_ic[(b, bu)] = int(2.5 * cons0[(b, bu)])

        for ent in cfg.ENTITIES:
            e, r0 = ent["code"], self.opening_rate(ent["code"])
            L: list[tuple] = []

            def add(acct, lc_cents, partner=""):
                L.append((acct, int(lc_cents), partner, "", ""))

            def eur_item(acct, eur_cents, partner):
                """IC balance denominated in EUR: track EUR and post LC at the opening rate."""
                self.eur[(e, acct, partner)] += eur_cents
                add(acct, self.to_lc(eur_cents, r0), partner)

            monthly_net = net0.get(e, 0.0)
            if e in net0:                                  # selling entities
                rec = sum(self.base_gross_lc(e, bu) * (1 - sum(r for a, r in cfg.GTN_RATES[bu].items() if a in cfg.GTN_ON_INVOICE))
                          for bu in ent["bu_mix"]) * ent["dso"] / 30
                for acct, share in (("1110", 0.70), ("1120", 0.15), ("1100", 0.15)):
                    add(acct, cents(rec * share))
                add("1150", -cents(monthly_net * 0.01))
                stat = sum(self.base_gross_lc(e, bu) * cfg.GTN_RATES[bu]["4050"] for bu in ent["bu_mix"])
                ret = sum(self.base_gross_lc(e, bu) * cfg.GTN_RATES[bu]["4070"] for bu in ent["bu_mix"])
                add("2140", -cents(2 * stat))
                add("2150", -cents(2 * ret))
                add("1000", cents(monthly_net * 1.0))
                add("1010", cents(monthly_net * 0.05))
                add("1220", cents(monthly_net * 0.20))
                add("1850", cents(monthly_net * 0.10))
                add("2000", -cents(monthly_net * 0.27))
                add("2110", -cents(monthly_net * 0.035))
                add("2100", -cents(monthly_net * 0.14))
                add("2130", -cents(monthly_net * 0.01))
                add("2160", -cents(monthly_net * 0.03))
                add("1540", cents(ent["sales_eur_m"] * 1e6 * 0.004 * r0))
                add("1550", cents(ent["sales_eur_m"] * 1e6 * 0.015 * r0))
                add("1610", cents(0.3e6 * r0))
                add("1690", -cents(0.15e6 * r0))
                fee_eur = fee0[e]
                eur_item("2090", -fee_eur, "DE01")          # fee payable to the holding (30 days)
                self.eur[("DE01", "1190", e)] += fee_eur     # mirrored on the holding side below
            if e in buyers:                                # IC inventory and payable to DE02
                ic_inv = sum(v for (b, _bu), v in self.inv_ic.items() if b == e)
                add("1340", self.to_lc(ic_inv, r0), "DE02")
                eur_item("2090", -ic_pay0[e], "DE02")

            if e == "DE01":
                add("1000", cents(25e6))
                add("1540", cents(0.5e6))
                add("1550", cents(0.3e6))
                add("1610", cents(0.6e6))
                add("1690", -cents(0.3e6))
                for sub in cfg.ENTITIES:
                    if sub["parent"] == "DE01":
                        inv = sub["own"] * (sub["share_capital_eur"] + sub["capital_reserve_eur"]) + sub["goodwill_eur"]
                        add("1800", cents(inv), sub["code"])
                add("1400", cents(cfg.IC_LOAN_PL01_OPENING), "PL01")
                self.eur[("DE01", "1400", "PL01")] += cents(cfg.IC_LOAN_PL01_OPENING)
                for sub, fee in fee0.items():
                    add("1190", fee, sub)
                add("2000", -cents(0.3e6))
                add("2300", -cents(80e6))
                add("2400", -cents(3e6))
            if e == "DE02":
                for b in buyers:
                    add("1190", ic_pay0[b], b)
                    self.eur[("DE02", "1190", b)] += ic_pay0[b]
                eur_item("2090", -cz_rev0, "CZ02")
                self.eur[("CZ02", "1190", "DE02")] += cz_rev0
                self.fg_eur = int(1.5 * need0)
                add("1330", self.fg_eur)
                self.raw_lc = int(2 * need0 * PRODUCTION_SPLIT["5000"])
                add("1300", self.raw_lc)
                add("1310", cents(1.2e6))
                for acct, val in (("1500", 5e6), ("1510", 45e6), ("1520", 70e6), ("1530", 12e6), ("1540", 3e6), ("1550", 2e6)):
                    add(acct, cents(val))
                add("1590", -cents(0.30 * (45e6 + 70e6 + 12e6 + 3e6 + 2e6)))
                add("1600", cents(60e6))
                add("1610", cents(1.7e6))           # in addition to the 0.3m added above
                add("1690", -cents(24e6 + 0.85e6))
                add("2400", -cents(12e6))
                add("2500", -cents(2e6))
            if e == "CZ02":
                add("1190", self.to_lc(cz_rev0, r0), "DE02")
                add("1000", cents(2e6 * r0))
                for acct, val in (("1500", 1e6), ("1510", 12e6), ("1520", 20e6), ("1530", 3e6), ("1540", 0.6e6)):
                    add(acct, cents(val * r0))
                add("1590", -cents(0.30 * (12e6 + 20e6 + 3e6 + 0.6e6) * r0))
                add("1610", cents(0.2e6 * r0))
                add("1690", -cents(0.1e6 * r0))
                add("2000", -cents(1.0e6 * r0))
                add("2110", -cents(0.1e6 * r0))
            if e == "PL01":
                eur_item("2200", -cents(cfg.IC_LOAN_PL01_OPENING), "DE01")
            # Sales-company PP&E depreciation reserve
            if e in buyers:
                add("1590", -cents(0.30 * ent["sales_eur_m"] * 1e6 * 0.019 * r0))
            # IFRS 16 leases --------------------------------------------------
            annual_rent_eur = {"DE01": 0.8e6, "DE02": 1.5e6, "CZ02": 0.4e6}.get(e, ent["sales_eur_m"] * 1e6 * 0.005)
            liab = cents(annual_rent_eur * 4 * r0)
            add("1570", liab)
            add("2320", -liab)
            r = 0.04 / 12
            self.lease[e] = {"rou0": liab, "payment": int(round(liab * r / (1 - (1 + r) ** -60)))}
            # Equity: share capital & reserve at historical rate, retained earnings = plug
            add("3000", -cents(ent["share_capital_eur"] * ent["hist_rate"]))
            add("3100", -cents(ent["capital_reserve_eur"] * ent["hist_rate"]))
            add("3200", -sum(ln[1] for ln in L))
            self.journal(e, y, m, L)

    # ------------------------------------------------------------------ #
    # Month: settlements of opening balances
    # ------------------------------------------------------------------ #
    def settlements(self, y: int, m: int) -> None:
        for ent in cfg.ENTITIES:
            e = ent["code"]
            L: list[tuple] = []
            cash = 0

            def settle(acct, fraction, partner=""):
                nonlocal cash
                b = self.bal[(e, acct, partner)]
                amt = int(round(-b * fraction))
                if amt:
                    L.append((acct, amt, partner, "", ""))
                    cash -= amt

            for acct in ("1100", "1110", "1120"):                      # customer collections
                if self.bal[(e, acct, "")] > 0:
                    settle(acct, min(1.0, 30 / ent["dso"]))
            settle("1200", 1.0)                                         # other receivables
            if self.bal[(e, "2000", "")] < 0:
                settle("2000", 30 / 45)                                 # supplier payments (DPO 45)
            settle("2110", 1.0)                                         # social security
            settle("2140", 0.5)                                         # statutory rebates paid
            settle("2150", 0.5)                                         # returns credited
            if m == 3:
                settle("2100", 1.0)                                     # annual bonus payout
            if m in (1, 4, 7, 10):
                settle("2130", 1.0)                                     # quarterly tax payment
            if e in ("DE01", "DE02") and self.bal[(e, "2400", "")] < 0:
                pay = cents(5e3 if e == "DE01" else 15e3)              # pension payments
                L.append(("2400", pay, "", "", ""))
                cash -= pay
            if cash:
                L.append(("1000", cash, "", "", ""))
            self.journal(e, y, m, L)

        # Intercompany settlements in EUR (same EUR amount on both sides)
        for (e, acct, partner), eur_bal in list(self.eur.items()):
            if acct != "2090" or eur_bal >= 0:
                continue
            fraction = 0.5 if partner == "DE02" else 1.0
            pay = int(round(-eur_bal * fraction))
            if not pay:
                continue
            lc_payer = self.to_lc(pay, self.avg(e, y, m))
            lc_payee = self.to_lc(pay, self.avg(partner, y, m))
            self.journal(e, y, m, [("2090", lc_payer, partner, "", ""), ("1000", -lc_payer, "", "", "")])
            self.journal(partner, y, m, [("1000", lc_payee, "", "", ""), ("1190", -lc_payee, e, "", "")])
            self.eur[(e, "2090", partner)] += pay
            self.eur[(partner, "1190", e)] -= pay

    # ------------------------------------------------------------------ #
    # Month: sales, gross-to-net, IC purchases, OPEX of selling entities
    # ------------------------------------------------------------------ #
    def sales_and_cogs(self, e: str, y: int, m: int) -> float:
        ent = self.E[e]
        rate = self.avg(e, y, m)
        L: list[tuple] = []
        net_total_lc = 0.0
        for bu in ent["bu_mix"]:
            gross = self.base_gross_lc(e, bu) * self.growth(e, y, m) * cfg.SEASONALITY[bu][m - 1] \
                * self.shock(e, bu, y, m) * self.noise(0.035)
            g_c = cents(gross)
            p = self.pc(e, bu)
            L.append((cfg.GROSS_SALES_ACCOUNT[bu], -g_c, "", "", p))
            receivable = g_c                      # invoiced amount = gross - on-invoice discounts
            net_c = g_c                           # net sales = gross - all gross-to-net deductions
            for acct, r in cfg.GTN_RATES[bu].items():
                if r == 0:
                    continue
                d = cents(gross * r * (self.noise(0.05) if acct == "4050" else 1.0))
                L.append((acct, d, "", "", p))
                net_c -= d
                if acct in cfg.GTN_ON_INVOICE:
                    receivable -= d
                else:                             # accrued: statutory rebates & returns
                    L.append((cfg.GTN_ACCRUAL_ACCOUNT[acct], -d, "", "", ""))
            for acct, share in zip(("1110", "1120", "1100"), split_cents(receivable, [0.70, 0.15, 0.15])):
                L.append((acct, share, "", "", ""))
            net_lc = net_c / 100.0
            net_total_lc += net_lc
            # Cost of goods -------------------------------------------------
            ratio = cfg.COGS_RATIO[bu] * (self.noise(0.02) if self.noise_on else 0.98)
            if bu in cfg.IC_SOURCED_BUS and e != "DE02":
                cons = cents(net_lc / rate * ratio)                            # EUR cents at transfer price
                writeoff = int(cons * 0.006 * self.noise(0.3))
                target = int(2.5 * cons)
                purchase = max(0, cons + writeoff + target - self.inv_ic[(e, bu)])
                self.inv_ic[(e, bu)] += purchase - cons - writeoff
                self.ic_purchases[(e, bu)] = purchase
                lc_p = self.to_lc(purchase, rate)
                lc_inv = self.to_lc(purchase - cons, rate)
                lc_wo = self.to_lc(writeoff, rate)
                L += [("5010", lc_p, "DE02", "", p), ("2090", -lc_p, "DE02", "", ""),
                      ("1340", lc_inv, "DE02", "", ""), ("5090", -lc_inv, "", "", p),
                      ("5140", lc_wo, "", self.cc(e, "LOG"), p), ("1340", -lc_wo, "DE02", "", "")]
                self.eur[(e, "2090", "DE02")] -= purchase
                self.eur[("DE02", "1190", e)] += purchase
            elif bu in cfg.IC_SOURCED_BUS and e == "DE02":
                self.domestic_need[bu] = cents(net_lc * ratio / (1 + cfg.DE02_MARKUP))
            else:                                                              # third-party merchandise
                c = cents(net_lc * ratio)
                L += [("5020", c, "", "", p), ("2000", -c, "", "", "")]
            freight = cents(net_lc * 0.02 * self.noise(0.05))
            L += [("5130", freight, "", self.cc(e, "LOG"), p), ("2000", -freight, "", "", "")]
        # other operating income and bad debt
        other = cents(net_total_lc * 0.003 * self.noise(0.5))
        bad = cents(net_total_lc * 0.001 * self.noise(0.3))
        L += [("1200", other, "", "", ""), ("4400", -other, "", "", self.pc(e, "CORP")),
              ("6910", bad, "", self.cc(e, "FIN"), self.pc(e, "CORP")), ("1150", -bad, "", "", "")]
        self.journal(e, y, m, L)
        self.net_sales_eur[e] = cents(net_total_lc / rate)
        return net_total_lc

    def _personnel_credit(self, e: str, acct: str, amt: int) -> list[tuple]:
        if acct == "6020":
            return [("2100", -amt, "", "", "")]
        if acct == "6040" and self.E[e]["country"] == "DE":
            return [("2400", -amt, "", "", "")]
        cash, ss = split_cents(amt, [0.75, 0.25])
        return [("1000", -cash, "", "", ""), ("2110", -ss, "", "", "")]

    def opex_pool(self, e: str, y: int, m: int, func: str, pool_lc: float, bu_mix: dict | None) -> None:
        """Post one OPEX pool (S&M, R&D or G&A) across accounts, cost centres and profit centres."""
        have = self.depts[e]
        debits: list[tuple] = []
        credits: dict[str, int] = defaultdict(int)
        for acct, (w, dept) in cfg.OPEX_POOLS[func].items():
            amt = cents(pool_lc * w * self.noise(0.06))
            if dept == "*":
                weights = {d: wt for d, wt in PERSONNEL_DEPT_WEIGHTS[func].items() if d in have} or {"FIN": 1.0}
            else:
                d = "RND" if (e == "DE02" and acct in ("6200", "6210")) else dept
                weights = {d if d in have else "FIN": 1.0}
            bus = bu_mix if bu_mix else {"CORP": 1.0}
            combos = [(d, b) for d in weights for b in bus]
            parts = split_cents(amt, [weights[d] * bus[b] for d, b in combos])
            for (d, b), part in zip(combos, parts):
                debits.append((acct, part, "", self.cc(e, d), self.pc(e, b)))
            cr = self._personnel_credit(e, acct, amt) if acct in cfg.PERSONNEL_ACCOUNTS else [("2000", -amt, "", "", "")]
            for c_acct, c_amt, *_ in cr:
                credits[c_acct] += c_amt
        self.journal(e, y, m, debits + [(a, v, "", "", "") for a, v in credits.items()])

    def operating_expenses(self, e: str, y: int, m: int) -> None:
        ent = self.E[e]
        run_rate_net = sum(self.base_gross_lc(e, bu) * (1 - self._gtn_total(bu)) for bu in ent["bu_mix"]) \
            * self.growth(e, y, m)
        factor = 1.0 if self.scenario == "ACT" else 0.97                  # budget assumes 3 % savings
        for func, ratio in cfg.OPEX_RATIO[ent["etype"]].items():
            self.opex_pool(e, y, m, func, run_rate_net * ratio * factor,
                           ent["bu_mix"] if func == "S&M" else None)

    # ------------------------------------------------------------------ #
    # Month: intercompany documents (sub-ledger), DE02, CZ02, DE01
    # ------------------------------------------------------------------ #
    def _ic_doc(self, flow, seller, buyer, y, m, seller_acct, buyer_acct, bu, eur_cents, n_docs=1):
        if self.scenario != "ACT" or eur_cents == 0:
            return
        weights = self.rng.dirichlet(np.ones(n_docs) * 3) if n_docs > 1 else [1.0]
        last_day = pd.Timestamp(y, m, 1) + pd.offsets.MonthEnd(0)
        for part in split_cents(eur_cents, weights):
            self.doc_seq += 1
            day = int(self.rng.integers(1, last_day.day + 1)) if n_docs > 1 else last_day.day
            self.ic_docs.append({
                "ic_document_id": f"IC{y}{self.doc_seq:07d}", "flow_type": flow,
                "seller_company": seller, "buyer_company": buyer,
                "fiscal_year": y, "fiscal_period": m,
                "document_date": pd.Timestamp(y, m, day).strftime("%Y-%m-%d"),
                "seller_group_account": seller_acct, "buyer_group_account": buyer_acct,
                "business_unit": bu, "document_currency": "EUR", "amount_doc": part / 100,
            })

    def de02_production_and_ic_sales(self, y: int, m: int) -> None:
        e = "DE02"
        need_by_bu: dict[str, int] = defaultdict(int)
        L: list[tuple] = []
        # IC sales of goods to each buyer (seller side; buyer side already posted)
        for (buyer, bu), eur_c in self.ic_purchases.items():
            p = self.pc(e, bu)
            L += [("1190", eur_c, buyer, "", ""), ("4100", -eur_c, buyer, "", p)]
            need_by_bu[bu] += int(eur_c / (1 + cfg.DE02_MARKUP))
            self._ic_doc("GOODS", e, buyer, y, m, "4100", "5010", bu, eur_c, n_docs=int(self.rng.integers(3, 9)) if self.noise_on else 1)
        for bu, v in self.domestic_need.items():
            need_by_bu[bu] += v
        self.journal(e, y, m, L)
        # Production with finished-goods inventory target of 1.5 months
        need = sum(need_by_bu.values())
        target = int(1.5 * need)
        production = max(0, need + target - self.fg_eur)
        delta_fg = production - need
        self.fg_eur += delta_fg
        bus = {bu: v / need for bu, v in need_by_bu.items()} if need else {"CORP": 1.0}
        L = []
        cz_eur = 0
        for key, share in PRODUCTION_SPLIT.items():
            amt = int(production * share * self.noise(0.04))
            parts = split_cents(amt, list(bus.values()))
            if key == "CZ02":
                cz_eur = amt
                for bu, part in zip(bus, parts):
                    L.append(("5010", part, "CZ02", self.cc(e, "PROD"), self.pc(e, bu)))
                L.append(("2090", -amt, "CZ02", "", ""))
                continue
            dept = "QA" if key == "5120" else "PROD"
            for bu, part in zip(bus, parts):
                L.append((key, part, "", self.cc(e, dept), self.pc(e, bu)))
            if key == "5000":
                L.append(("1300", -amt, "", "", ""))
                self.raw_lc -= amt
            elif key == "5100":
                L += self._personnel_credit(e, "5100", amt)
            else:
                L.append(("2000", -amt, "", "", ""))
        for bu, part in zip(bus, split_cents(delta_fg, list(bus.values()))):
            L.append(("5150", -part, "", self.cc(e, "PROD"), self.pc(e, bu)))
        L.append(("1330", delta_fg, "", "", ""))
        # raw material purchases to keep two months of consumption in stock
        materials = int(production * PRODUCTION_SPLIT["5000"])
        purchase = max(0, 2 * materials - self.raw_lc)        # raw_lc is already net of consumption
        self.raw_lc += purchase
        L += [("1300", purchase, "", "", ""), ("2000", -purchase, "", "", "")]
        self.journal(e, y, m, L)
        self.eur[("DE02", "2090", "CZ02")] -= cz_eur
        self.eur[("CZ02", "1190", "DE02")] += cz_eur
        self._ic_doc("CONTRACT_MANUFACTURING", "CZ02", "DE02", y, m, "4100", "5010", "CORP", cz_eur)
        self.cz02(y, m, cz_eur)

    def cz02(self, y: int, m: int, revenue_eur: int) -> None:
        e = "CZ02"
        rate = self.avg(e, y, m)
        rev_lc = self.to_lc(revenue_eur, rate)
        corp = self.pc(e, "CORP")
        L = [("1190", rev_lc, "DE02", "", ""), ("4100", -rev_lc, "DE02", "", corp)]
        cost = rev_lc / (1 + cfg.CZ02_MARKUP) * cfg.CZ02_CASH_COST_FACTOR
        for acct, share in CZ02_COST_SPLIT.items():
            amt = int(cost * share * self.noise(0.03))
            dept = "QA" if acct == "5120" else "PROD"
            L.append((acct, amt, "", self.cc(e, dept), corp))
            L += self._personnel_credit(e, acct, amt) if acct == "5100" else [("2000", -amt, "", "", "")]
        self.journal(e, y, m, L)
        self.opex_pool(e, y, m, "G&A", rev_lc / 100 * 0.02, None)

    def holding(self, y: int, m: int) -> None:
        """DE01: own G&A, management fees to all selling entities, IC loans and bank debt."""
        e = "DE01"
        t = (y - cfg.FIRST_YEAR) * 12 + m
        factor = 1.0 if self.scenario == "ACT" else 0.97
        self.opex_pool(e, y, m, "G&A", 1.1e6 * (1.03 ** (t / 12)) * factor, None)
        # Management fees (2 % of third-party net sales)
        for sub, net_eur in self.net_sales_eur.items():
            fee = int(round(net_eur * cfg.MGMT_FEE_RATE))
            lc = self.to_lc(fee, self.avg(sub, y, m))
            self.journal(e, y, m, [("1190", fee, sub, "", ""), ("4300", -fee, sub, "", self.pc(e, "CORP"))])
            self.journal(sub, y, m, [("6800", lc, "DE01", self.cc(sub, "FIN"), self.pc(sub, "CORP")),
                                     ("2090", -lc, "DE01", "", "")])
            self.eur[(sub, "2090", "DE01")] -= fee
            self.eur[(e, "1190", sub)] += fee
            self._ic_doc("MANAGEMENT_FEE", e, sub, y, m, "4300", "6800", "CORP", fee)
        # IC loan to RO01 drawn in July 2024
        if (y, m) == (2024, 7):
            self._loan_principal("RO01", y, m, cents(cfg.IC_LOAN_RO01_DRAWDOWN))
        # Interest on IC loans (paid monthly in EUR)
        for borrower in ("PL01", "RO01"):
            principal = self.eur[(e, "1400", borrower)]
            if principal <= 0:
                continue
            interest = int(round(principal * cfg.IC_LOAN_RATE / 12))
            lc = self.to_lc(interest, self.avg(borrower, y, m))
            self.journal(e, y, m, [("1000", interest, "", "", ""), ("7010", -interest, borrower, "", self.pc(e, "CORP"))])
            self.journal(borrower, y, m, [("7210", lc, "DE01", self.cc(borrower, "FIN"), self.pc(borrower, "CORP")),
                                          ("1000", -lc, "", "", "")])
            self._ic_doc("LOAN_INTEREST", e, borrower, y, m, "7010", "7210", "CORP", interest)
        # PL01 repays part of its loan every December
        if m == 12 and self.eur[(e, "1400", "PL01")] > 0:
            repay = min(cents(cfg.IC_LOAN_PL01_REPAYMENT), self.eur[(e, "1400", "PL01")])
            self._loan_principal("PL01", y, m, -repay)
        # Bank loan: 5 % interest, EUR 2m amortisation per quarter
        loan = -self.bal[(e, "2300", "")]
        if loan > 0:
            interest = int(round(loan * 0.05 / 12))
            L = [("7200", interest, "", self.cc(e, "FIN"), self.pc(e, "CORP")), ("1000", -interest, "", "", "")]
            if m in (3, 6, 9, 12):
                L += [("2300", cents(2e6), "", "", ""), ("1000", -cents(2e6), "", "", "")]
            self.journal(e, y, m, L)

    def _loan_principal(self, borrower: str, y: int, m: int, eur_cents: int) -> None:
        """Positive amount = drawdown (cash to borrower), negative = repayment."""
        lc = self.to_lc(eur_cents, self.avg(borrower, y, m))
        self.journal("DE01", y, m, [("1400", eur_cents, borrower, "", ""), ("1000", -eur_cents, "", "", "")])
        self.journal(borrower, y, m, [("1000", lc, "", "", ""), ("2200", -lc, "DE01", "", "")])
        self.eur[("DE01", "1400", borrower)] += eur_cents
        self.eur[(borrower, "2200", "DE01")] -= eur_cents
        flow = "LOAN_DRAWDOWN" if eur_cents > 0 else "LOAN_REPAYMENT"
        self._ic_doc(flow, "DE01", borrower, y, m, "1400", "2200", "CORP", abs(eur_cents))

    # ------------------------------------------------------------------ #
    # Month: assets, leases, dividends, month-end close
    # ------------------------------------------------------------------ #
    def fixed_assets(self, e: str, y: int, m: int) -> None:
        ent = self.E[e]
        rate = self.avg(e, y, m)
        corp = self.pc(e, "CORP")
        prod = e in ("DE02", "CZ02")
        L: list[tuple] = []
        # CAPEX (EUR amounts translated at the month's rate)
        capex = {"DE01": {"1540": 10e3},
                 "DE02": {"1520": 250e3, "1530": 40e3, "1540": 25e3},
                 "CZ02": {"1520": 120e3, "1540": 8e3}}.get(
            e, {"1540": ent["sales_eur_m"] * 1e6 * 0.001 / 12, "1550": ent["sales_eur_m"] * 1e6 * 0.003 / 12})
        if e == "DE02" and y == 2024:
            capex = {**capex, "1560": 450e3}           # new packaging line (assets under construction)
        if e == "CZ02" and y == 2025 and 3 <= m <= 11:
            capex = {**capex, "1560": 200e3}           # cleanroom upgrade
        for acct, eur in capex.items():
            amt = cents(eur * rate * self.noise(0.2))
            L += [(acct, amt, "", "", ""), ("2000", -amt, "", "", "")]
        if (e, y, m) in (("DE02", 2024, 12), ("CZ02", 2025, 11)):   # go-live -> capitalise AuC
            auc = self.bal[(e, "1560", "")] + sum(a for acc, a, *_ in L if acc == "1560")
            L += [("1520", auc, "", "", ""), ("1560", -auc, "", "", "")]
        # Depreciation & amortisation on gross balances
        for acct, (life, da_acct) in DEPRECIATION.items():
            gross = self.bal[(e, acct, "")]
            if gross > 0:
                d = int(gross / (life * 12))
                cc = self.cc(e, "PROD" if prod and acct in ("1510", "1520", "1530") else "FIN")
                L += [(da_acct, d, "", cc, corp), ("1590", -d, "", "", "")]
        for acct, (life, da_acct) in AMORTISATION.items():
            gross = self.bal[(e, acct, "")]
            if gross > 0:
                d = int(gross / (life * 12))
                L += [(da_acct, d, "", self.cc(e, "FIN"), corp), ("1690", -d, "", "", "")]
        # IFRS 16: depreciation of right-of-use asset, interest accretion, lease payment
        lease = self.lease[e]
        if self.bal[(e, "1570", "")] > 0:
            dep = min(int(lease["rou0"] / 60), self.bal[(e, "1570", "")])
            L += [("6990", dep, "", self.cc(e, "FIN"), corp), ("1570", -dep, "", "", "")]
        liab = -self.bal[(e, "2320", "")]
        if liab > 0:
            interest = int(round(liab * 0.04 / 12))
            pay = min(lease["payment"], liab + interest)
            L += [("7220", interest, "", self.cc(e, "FIN"), corp), ("2320", -interest, "", "", ""),
                  ("2320", pay, "", "", ""), ("1000", -pay, "", "", "")]
        self.journal(e, y, m, L)

    def one_off_items(self, y: int, m: int) -> None:
        """Non-recurring costs (actuals only) - reclassified by Group Controlling in the management view."""
        if self.scenario != "ACT":
            return
        for item in cfg.ONE_OFF_ITEMS:
            if (item["year"], item["month"]) != (y, m):
                continue
            e = item["company"]
            eur = cents(item["amount_eur"])
            lc = self.to_lc(eur, self.avg(e, y, m))
            if item["credit"] == "1340":                       # inventory write-off of IC-sourced stock
                partner = "DE02"
                self.inv_ic[(e, item["bu"])] -= eur
            else:
                partner = ""
            self.journal(e, y, m, [(item["account"], lc, "", self.cc(e, item["dept"]), self.pc(e, item["bu"])),
                                   (item["credit"], -lc, partner, "", "")])

    def dividends(self, y: int, m: int) -> None:
        if m != cfg.DIVIDEND_MONTH:
            return
        for payer in cfg.DIVIDEND_PAYERS:
            ent = self.E[payer]
            rate = self.avg(payer, y, m)
            if y == cfg.FIRST_YEAR:                      # prior-year profit estimate (pre-migration)
                ni_eur = ent["sales_eur_m"] * 1e6 * 0.08
            else:
                ni_eur = -self.net_income[(payer, y - 1)] / 100 / rate
            if ni_eur <= 0:
                continue
            total_eur = cents(round(ni_eur * cfg.DIVIDEND_PAYOUT, -3))     # rounded to EUR 1,000
            group_eur = int(round(total_eur * ent["own"]))                  # share of the parent DE01
            minority_eur = total_eur - group_eur                            # share of non-controlling holders
            lc_group = self.to_lc(group_eur, rate)
            lc_minority = self.to_lc(minority_eur, rate)
            self.journal(payer, y, m, [("3300", lc_group, "DE01", "", ""),
                                       ("3300", lc_minority, "", "", ""),
                                       ("1000", -(lc_group + lc_minority), "", "", "")])
            self.journal("DE01", y, m, [("1000", group_eur, "", "", ""),
                                        ("7100", -group_eur, payer, "", self.pc("DE01", "CORP"))])
            self._ic_doc("DIVIDEND", payer, "DE01", y, m, "7100", "3300", "CORP", group_eur)

    def month_end(self, y: int, m: int) -> None:
        for ent in cfg.ENTITIES:
            e = ent["code"]
            corp, fin = self.pc(e, "CORP"), self.cc(e, "FIN")
            # Interest on the operating bank account (deposit income or overdraft cost)
            cash = self.bal[(e, "1000", "")]
            if cash > 0:
                i = int(round(cash * 0.01 / 12))
                self.journal(e, y, m, [("1000", i, "", "", ""), ("7000", -i, "", fin, corp)])
            elif cash < 0:
                i = int(round(-cash * 0.06 / 12))
                self.journal(e, y, m, [("7200", i, "", fin, corp), ("1000", -i, "", "", "")])
            # Revaluation of EUR-denominated IC balances at the closing rate (non-EUR entities)
            if ent["ccy"] != "EUR":
                L = []
                for (ee, acct, partner), eur_bal in self.eur.items():
                    if ee != e:
                        continue
                    target = self.to_lc(eur_bal, self.close(e, y, m))
                    diff = target - self.bal[(e, acct, partner)]
                    if diff:
                        L += [(acct, diff, partner, "", ""), ("7300", -diff, "", fin, corp)]
                self.journal(e, y, m, L)
            # Income tax on profit before tax (dividend income tax-exempt)
            pbt = -(self.pl_month[e] - self.div_month[e])
            tax = int(round(pbt * ent["tax"]))
            self.journal(e, y, m, [("8000", tax, "", fin, corp), ("2130", -tax, "", "", "")])
            self.net_income[(e, y)] += self.pl_month[e]   # stored with ledger sign (profit < 0)
            self.pl_month[e] = 0
            self.div_month[e] = 0

    # ------------------------------------------------------------------ #
    # Driver
    # ------------------------------------------------------------------ #
    def run(self) -> None:
        self.opening_balances()
        for y, m in cfg.PERIODS:
            self.ic_purchases, self.domestic_need, self.net_sales_eur = {}, {}, {}
            self.settlements(y, m)
            for e in cfg.SALES_ENTITIES:
                self.sales_and_cogs(e, y, m)
                self.operating_expenses(e, y, m)
            self.de02_production_and_ic_sales(y, m)
            self.holding(y, m)
            for ent in cfg.ENTITIES:
                self.fixed_assets(ent["code"], y, m)
            self.one_off_items(y, m)
            self.dividends(y, m)
            self.month_end(y, m)

    def postings(self) -> pd.DataFrame:
        """Aggregate posted lines to trial-balance grain (group accounts, integer cents)."""
        df = pd.DataFrame(self.rows, columns=["company_code", "fiscal_year", "fiscal_period", "group_account",
                                              "trading_partner", "cost_center", "profit_center", "amount_cents"])
        keys = ["company_code", "fiscal_year", "fiscal_period", "group_account",
                "trading_partner", "cost_center", "profit_center"]
        df = df.groupby(keys, as_index=False)["amount_cents"].sum()
        return df[df["amount_cents"] != 0].reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Group -> local chart of accounts
# --------------------------------------------------------------------------- #
def to_local_accounts(postings: pd.DataFrame, local_accounts: pd.DataFrame) -> pd.DataFrame:
    """Map group accounts back to each entity's local chart (the reverse of what the ETL will do).

    Where a group account has two local accounts, the amount is split 65 / 35 in cents.
    """
    coa = {e["code"]: e["coa"] for e in cfg.ENTITIES}
    la = local_accounts.sort_values(["chart_of_accounts", "group_account", "local_account"]).copy()
    la["rank"] = la.groupby(["chart_of_accounts", "group_account"]).cumcount()
    la["n_local"] = la.groupby(["chart_of_accounts", "group_account"])["local_account"].transform("count")
    df = postings.copy()
    df["chart_of_accounts"] = df["company_code"].map(coa)
    df = df.merge(la[["chart_of_accounts", "group_account", "local_account", "rank", "n_local"]],
                  on=["chart_of_accounts", "group_account"], how="left", validate="many_to_many")
    if df["local_account"].isna().any():
        missing = df.loc[df["local_account"].isna(), "group_account"].unique()
        raise ValueError(f"Group accounts without local mapping: {missing}")
    first = (df["amount_cents"] * cfg.LOCAL_SPLIT_RATIO).round().astype("int64")
    df["amount_cents"] = np.where(df["n_local"] == 1, df["amount_cents"],
                                  np.where(df["rank"] == 0, first, df["amount_cents"] - first))
    return df[df["amount_cents"] != 0].drop(columns=["rank", "n_local", "chart_of_accounts"])


def format_tb(local: pd.DataFrame) -> pd.DataFrame:
    """Shape rows like a source-system extract (one row per account/partner/CC/PC)."""
    ent = {e["code"]: e for e in cfg.ENTITIES}
    out = pd.DataFrame({
        "company_code": local["company_code"],
        "fiscal_year": local["fiscal_year"].astype(int),
        "fiscal_period": local["fiscal_period"].astype(int),
        "gl_account": local["local_account"],
        "trading_partner": local["trading_partner"],
        "cost_center": local["cost_center"],
        "profit_center": local["profit_center"],
        "currency": local["company_code"].map(lambda c: ent[c]["ccy"]),
        "amount_lc": (local["amount_cents"] / 100).map(lambda v: f"{v:.2f}"),
        "source_system": local["company_code"].map(lambda c: ent[c]["erp"]),
    })
    # Extract runs on day 5 after month end; the opening balance migration ran on 5 Jan 2023
    period_end = pd.to_datetime(out["fiscal_year"].astype(str) + "-" +
                                out["fiscal_period"].clip(lower=1).astype(str).str.zfill(2) + "-01") \
        + pd.offsets.MonthEnd(0)
    extract = (period_end + pd.Timedelta(days=5)).dt.strftime("%Y-%m-%dT18:00:00")
    extract = extract.where(out["fiscal_period"] != 0, f"{cfg.FIRST_YEAR}-01-05T18:00:00")
    out["extract_timestamp"] = extract
    return out.sort_values(["company_code", "fiscal_year", "fiscal_period", "gl_account",
                            "trading_partner", "cost_center", "profit_center"]).reset_index(drop=True)


def tb_file_name(company: str, year: int, period: int) -> str:
    return f"tb_{company}_{year}{period:02d}.csv"


def write_tb_files(tb: pd.DataFrame, data_dir: Path) -> int:
    folder = data_dir / "raw" / "tb"
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("tb_*.csv"):
        old.unlink()
    n = 0
    for (c, y, p), g in tb.groupby(["company_code", "fiscal_year", "fiscal_period"], sort=True):
        g.to_csv(folder / tb_file_name(c, y, p), index=False)
        n += 1
    return n


def build_budget(fx_rates: pd.DataFrame, fx_budget: pd.DataFrame, seed: int) -> pd.DataFrame:
    sim = Simulator(fx_rates, fx_budget, "BUD", seed)
    sim.run()
    p = sim.postings()
    p = p[(p["fiscal_period"] > 0) & (~p["group_account"].map(cfg.is_balance_sheet))].copy()
    ccy = {e["code"]: e["ccy"] for e in cfg.ENTITIES}
    return pd.DataFrame({
        "version": "BUD",
        "company_code": p["company_code"],
        "fiscal_year": p["fiscal_year"],
        "fiscal_period": p["fiscal_period"],
        "group_account": p["group_account"],
        "trading_partner": p["trading_partner"],
        "cost_center": p["cost_center"],
        "profit_center": p["profit_center"],
        "currency": p["company_code"].map(ccy),
        "amount_lc": (p["amount_cents"] / 100).map(lambda v: f"{v:.2f}"),
    }).reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def generate_tb(master: dict[str, pd.DataFrame], data_dir: Path, seed: int = cfg.SEED) -> dict:
    """Simulate actuals and budget, write raw files and return in-memory results."""
    act = Simulator(master["fx_rates"], master["fx_budget"], "ACT", seed)
    act.run()
    postings = act.postings()
    local = to_local_accounts(postings, master["local_accounts"])
    tb = format_tb(local)
    n_files = write_tb_files(tb, data_dir)

    ic = pd.DataFrame(act.ic_docs)
    (data_dir / "raw" / "ic").mkdir(parents=True, exist_ok=True)
    ic.to_csv(data_dir / "raw" / "ic" / "ic_transactions.csv", index=False)

    # Management adjustments file (manual journal upload by Group Controlling, in EUR)
    adj = pd.DataFrame([{
        "adjustment_id": i["adjustment_id"], "company_code": i["company"], "fiscal_year": i["year"],
        "fiscal_period": i["month"], "group_account": i["account"],
        "cost_center": f"CC-{i['company']}-{i['dept']}", "profit_center": f"PC-{i['company']}-{i['bu']}",
        "amount_eur": f"{i['amount_eur']:.2f}", "adjustment_type": "NON_RECURRING", "description": i["description"],
    } for i in cfg.ONE_OFF_ITEMS])
    (data_dir / "raw" / "adjustments").mkdir(parents=True, exist_ok=True)
    adj.to_csv(data_dir / "raw" / "adjustments" / "management_adjustments.csv", index=False)

    budget = build_budget(master["fx_rates"], master["fx_budget"], seed)
    bud_dir = data_dir / "raw" / "budget"
    bud_dir.mkdir(parents=True, exist_ok=True)
    for y, g in budget.groupby("fiscal_year"):
        g.to_csv(bud_dir / f"budget_{y}.csv", index=False)

    return {"postings": postings, "tb": tb, "ic_transactions": ic, "budget": budget,
            "tb_files": n_files, "eur_balances": dict(act.eur)}


def _load_master(data_dir: Path) -> dict[str, pd.DataFrame]:
    return {
        "fx_rates": pd.read_csv(data_dir / "fx" / "fx_rates.csv", dtype={"period_id": str}),
        "fx_budget": pd.read_csv(data_dir / "fx" / "fx_budget.csv"),
        "local_accounts": pd.read_csv(data_dir / "master" / "local_accounts.csv",
                                      dtype={"local_account": str, "group_account": str}),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=cfg.DEFAULT_DATA_DIR)
    parser.add_argument("--seed", type=int, default=cfg.SEED)
    args = parser.parse_args()
    res = generate_tb(_load_master(args.out), args.out, args.seed)
    print(f"TB files: {res['tb_files']}, TB rows: {len(res['tb']):,}, "
          f"IC documents: {len(res['ic_transactions']):,}, budget rows: {len(res['budget']):,}")


if __name__ == "__main__":
    main()
