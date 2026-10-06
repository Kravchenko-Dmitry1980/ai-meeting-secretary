"""The same approved policy and SQLite path for all local cloud consumers."""
from secretary.application.monthly_budget import MonthlyBudget
from secretary.domain.cloud_budget import APPROVED_MONTHLY_MICRO, to_micro
from secretary.infrastructure.budget_repository import BudgetRepository
from secretary.infrastructure.polza_account import PolzaAccountClient


def build_monthly_budget(settings, *, account_reader=None, clock=None, billing_path=None,
                         maintenance=None, participant_id=None):
    approved = APPROVED_MONTHLY_MICRO - to_micro(settings.approved_monthly_external_costs_rub)
    return MonthlyBudget(BudgetRepository(billing_path if billing_path is not None else settings.data_dir / "billing.sqlite3",
                                         maintenance=maintenance, participant_id=participant_id),
                         approved_budget_micro=approved, clock=clock,
                         account_reader=account_reader or PolzaAccountClient(settings, clock=clock))
