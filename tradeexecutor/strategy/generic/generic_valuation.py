"""Value model based on their selling price on generic routing"""
import datetime

from tradeexecutor.state.position import TradingPosition
from tradeexecutor.state.valuation import ValuationUpdate
from tradeexecutor.strategy.generic.pair_configurator import PairConfigurator
from tradeexecutor.strategy.valuation import ValuationModel


class GenericValuation(ValuationModel):
    """Position is valued depending on its type.

    - Spot: What is the current spot close price,
      ask directly from the chain

    - Leveraged position: Use the refreshed loan
      value from the last `sync_interests()`,
      but don't ask anything from the chain here
    """

    def __init__(
            self,
            pair_configurator: PairConfigurator,
    ):
        self.pair_configurator = pair_configurator

    def __call__(
            self,
            ts: datetime.datetime,
            position: TradingPosition,
    ) -> ValuationUpdate:
        valuation_model = self.pair_configurator.get_valuation(position.pair)
        return valuation_model(ts, position)


