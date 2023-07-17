"""Strategy reserve currency management."""

import datetime
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, Dict, List, Iterable

from dataclasses_json import dataclass_json

from tradeexecutor.state.balance_update import BalanceUpdate
from tradeexecutor.state.generic_position import GenericPosition, BalanceUpdateEventAlreadyAdded
from tradeexecutor.state.identifier import AssetIdentifier
from tradeexecutor.state.types import USDollarAmount
from tradeexecutor.utils.accuracy import sum_decimal


@dataclass_json
@dataclass(slots=True)
class ReservePosition(GenericPosition):
    """Manage reserve currency of a portfolio.

    - One portfolio can have multiple reserve currencies,
      but currently the code is simplified to handle only one reserve currency

    """

    #: What is our reserve currency
    asset: AssetIdentifier

    #: How much reserves we have currently
    quantity: Decimal

    #: When we processed deposits/withdraws last time
    last_sync_at: datetime.datetime

    #: What was the US dollar exchange rate of our reserves
    reserve_token_price: USDollarAmount

    #: When we fetched the US dollar exchange rate of our reserves last time
    last_pricing_at: datetime.datetime

    #: What was the first deposit amount.
    #:
    #: Used to shortcut the backtest performance benchmark.
    #:
    #: TODO: Remove in future versions as SyncModel has been rewritten.
    initial_deposit: Optional[Decimal] = None

    #: What was the first deposit exchange rate.
    #:
    #: Used to shortcut the backtest performance benchmark.
    #:
    #: TODO: Remove in future versions as SyncModel has been rewritten.
    initial_deposit_reserve_token_price: Optional[USDollarAmount] = None

    #: BalanceUpdate.id -> BalanceUpdate mapping
    #:
    balance_updates: Dict[int, BalanceUpdate] = field(default_factory=dict)

    def __repr__(self) -> str:
        return f"<ReservePosition {self.asset} at {self.quantity}>"

    def __post_init__(self):
        assert self.asset.decimals > 0, f"Looks like we have improper reserve asset: {self.asset}"

    def get_identifier(self) -> str:
        return self.asset.get_identifier()

    def get_value(self) -> USDollarAmount:
        """Approximation of current value of this reserve."""
        return float(self.quantity) * self.reserve_token_price

    def get_quantity(self) -> Decimal:
        """Get the absolute amount of reserve tokens held."""
        return self.quantity

    def get_total_equity(self) -> USDollarAmount:
        """Approximation of total equity of this reserve."""
        return self.get_value()

    def get_balance_update_quantity(self) -> Decimal:
        """Get quantity of all balance udpdates for this position.

        :return:
            How much deposit and in-kind redemptions events have affected this position.

            Decimal zero epsilon noted.
        """
        return sum_decimal([b.quantity for b in self.balance_updates.values()])

    def update_value(self,
             exchange_rate: float = 1,
             ):
        """Updated portfolio's reserve balance.

        Read all balance update events and sets the current denormalised value of the reserves.

        This is read in :py:meth:`tradeeexecutor.state.portfolio.Portfolio.get_default_reserve`
        and used by strategy in various positions.

        :param exchange_rate:
            USD exchange rate of the reserve asset
        """
        quantity = self.get_balance_update_quantity()
        self.quantity = quantity
        self.reserve_token_price = exchange_rate
        self.last_pricing_at = datetime.datetime.utcnow()
        self.last_sync_at = datetime.datetime.utcnow()

    def calculate_quantity_usd_value(self, quantity: Decimal) -> USDollarAmount:
        """Return the quantity

        Now hardwired all reserves are 1:1 USDC.

        :return:
            Dollar amount
        """
        return float(quantity)

    def get_balance_update_events(self) -> Iterable[BalanceUpdate]:
        return self.balance_updates.values()

    def add_balance_update_event(self, event: BalanceUpdate):
        """Include a new balance update event

        :raise BalanceUpdateEventAlreadyAdded:
            In the case of a duplicate and event id is already used.
        """
        if event.balance_update_id in self.balance_updates:
            raise BalanceUpdateEventAlreadyAdded(f"Duplicate balance update: {event}")

        self.balance_updates[event.balance_update_id] = event
