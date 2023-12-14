"""Describe strategy modules and their loading."""
import datetime
import logging
import runpy
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Protocol, List, Optional, Union
from urllib.parse import urlparse

from packaging import version

import pandas
import pandas as pd

from tradeexecutor.strategy.engine_version import SUPPORTED_TRADING_STRATEGY_ENGINE_VERSIONS, TradingStrategyEngineVersion
from tradingstrategy.chain import ChainId
from tradeexecutor.state.state import State
from tradeexecutor.state.trade import TradeExecution
from tradeexecutor.strategy.cycle import CycleDuration
from tradeexecutor.strategy.default_routing_options import TradeRouting
from tradeexecutor.strategy.execution_context import ExecutionContext
from tradeexecutor.strategy.factory import StrategyFactory
from tradeexecutor.strategy.pricing_model import PricingModel
from tradeexecutor.strategy.reserve_currency import ReserveCurrency
from tradeexecutor.strategy.strategy_type import StrategyType
from tradeexecutor.strategy.trading_strategy_universe import TradingStrategyUniverse
from tradingstrategy.client import Client
from tradingstrategy.timebucket import TimeBucket
from tradingstrategy.universe import Universe

from tradeexecutor.strategy.universe_model import UniverseOptions

#: As set for StrategyModuleInformation.trading_strategy_engine_version
CURRENT_ENGINE_VERSION = "0.3"


logger = logging.getLogger()


class StrategyModuleNotValid(Exception):
    """Raised when we cannot load a strategy module."""


class DecideTradesProtocol(Protocol):
    """A call signature protocol for user's decide_trades() functions.

    This describes the `decide_trades` function parameters
    using Python's `callback protocol <https://peps.python.org/pep-0544/#callback-protocols>`_ feature.

    See also :ref:`strategy examples`.

    Example decide_trades function:

    .. code-block:: python

        def decide_trades(
            timestamp: pd.Timestamp,
            universe: Universe,
            state: State,
            pricing_model: PricingModel,
            cycle_debug_data: Dict) -> List[TradeExecution]:

            # The pair we are trading
            pair = universe.pairs.get_single()

            # How much cash we have in the hand
            cash = state.portfolio.get_current_cash()

            # Get OHLCV candles for our trading pair as Pandas Dataframe.
            # We could have candles for multiple trading pairs in a different strategy,
            # but this strategy only operates on single pair candle.
            # We also limit our sample size to N latest candles to speed up calculations.
            candles: pd.DataFrame = universe.candles.get_single_pair_data(timestamp, sample_count=batch_size)

            # We have data for open, high, close, etc.
            # We only operate using candle close values in this strategy.
            close = candles["close"]

            # Calculate exponential moving averages based on slow and fast sample numbers.
            slow_ema_series = ema(close, length=slow_ema_candle_count)
            fast_ema_series = ema(close, length=fast_ema_candle_count)

            if slow_ema_series is None or fast_ema_series is None:
                # Cannot calculate EMA, because
                # not enough samples in backtesting
                return []

            slow_ema = slow_ema_series.iloc[-1]
            fast_ema = fast_ema_series.iloc[-1]

            # Get the last close price from close time series
            # that's Pandas's Series object
            # https://pandas.pydata.org/docs/reference/api/pandas.Series.iat.html
            current_price = close.iloc[-1]

            # List of any trades we decide on this cycle.
            # Because the strategy is simple, there can be
            # only zero (do nothing) or 1 (open or close) trades
            # decides
            trades = []

            # Create a position manager helper class that allows us easily to create
            # opening/closing trades for different positions
            position_manager = PositionManager(timestamp, universe, state, pricing_model)

            if current_price >= slow_ema:
                # Entry condition:
                # Close price is higher than the slow EMA
                if not position_manager.is_any_open():
                    buy_amount = cash * position_size
                    trades += position_manager.open_1x_long(pair, buy_amount)
            elif fast_ema >= slow_ema:
                # Exit condition:
                # Fast EMA crosses slow EMA
                if position_manager.is_any_open():
                    trades += position_manager.close_all()

            # Visualize strategy
            # See available Plotly colours here
            # https://community.plotly.com/t/plotly-colours-list/11730/3?u=miohtama
            visualisation = state.visualisation
            visualisation.plot_indicator(timestamp, "Slow EMA", PlotKind.technical_indicator_on_price, slow_ema, colour="darkblue")
            visualisation.plot_indicator(timestamp, "Fast EMA", PlotKind.technical_indicator_on_price, fast_ema, colour="#003300")

            return trades
    """

    def __call__(self,
            timestamp: pandas.Timestamp,
            universe: Universe,
            state: State,
            pricing_model: PricingModel,
            cycle_debug_data: Dict) -> List[TradeExecution]:
            """The brain function to decide the trades on each trading strategy cycle.

            - Reads incoming execution state (positions, past trades),
              usually by creating a :py:class:`~tradingstrategy.strategy.pandas_trades.position_manager.PositionManager`.

            - Reads the price and volume status of the current trading universe, or OHLCV candles

            - Decides what to do next, by calling `PositionManager` to tell what new trading positions open
              or close

            - Outputs strategy thinking for visualisation and debug messages

            See also :ref:`strategy examples`

            :param timestamp:
                The Pandas timestamp object for this cycle. Matches
                trading_strategy_cycle division.
                Always truncated to the zero seconds and minutes, never a real-time clock.

            :param universe:
                Trading universe that was constructed earlier.

                .. note ::

                    In version 0.3 this was changed from `Universe` -> `TradingStrategyUniverse`.

            :param state:
                The current trade execution state.
                Contains current open positions and all previously executed trades.

            :param pricing_model:
                Position manager helps to create trade execution instructions to open and close positions.

            :param cycle_debug_data:
                Python dictionary for various debug variables you can read or set, specific to this trade cycle.
                This data is discarded at the end of the trade cycle.

            :return:
                List of trade instructions in the form of :py:class:`TradeExecution` instances.
                The trades can be generated using `position_manager` but strategy could also handcraft its trades.
            """

class DecideTradesProtocol2(Protocol):
    """New decide_trades() function signature.

    `universe` -> `strategy_universe` decide_trades() argument change.

    See :py:class:`DecideTradesProtocol`
    """

    def __call__(self,
            timestamp: pandas.Timestamp,
            strategy_universe: TradingStrategyUniverse,
            state: State,
            pricing_model: PricingModel,
            cycle_debug_data: Dict) -> List[TradeExecution]:
        """The brain function to decide the trades on each"""
        raise NotImplementedError()


class CreateTradingUniverseProtocol(Protocol):
    """A call signature protocol for user's create_trading_universe() functions.

    This describes the `create_trading_universe` function in trading strategies
    using Python's `callback protocol <https://peps.python.org/pep-0544/#callback-protocols>`_ feature.

    See also :ref:`strategy examples`.

    Example `create_trading_universe` function:

    .. code-block:: python

        def create_trading_universe(
                ts: datetime.datetime,
                client: Client,
                execution_context: ExecutionContext,
                candle_time_frame_override: Optional[TimeBucket]=None,
        ) -> TradingStrategyUniverse:

            # Load all datas we can get for our candle time bucket
            dataset = load_pair_data_for_single_exchange(
                client,
                execution_context,
                candle_time_bucket,
                chain_id,
                exchange_slug,
                [trading_pair_ticker],
                stop_loss_time_bucket=stop_loss_time_bucket,
                )

            # Filter down to the single pair we are interested in
            universe = TradingStrategyUniverse.create_single_pair_universe(
                dataset,
                chain_id,
                exchange_slug,
                trading_pair_ticker[0],
                trading_pair_ticker[1],
            )

            return universe
    """

    def __call__(self,
            timestamp: pandas.Timestamp,
            client: Optional[Client],
            execution_context: ExecutionContext,
            universe_options: UniverseOptions) -> TradingStrategyUniverse:
        """Creates the trading universe where the strategy trades.

        See also :ref:`strategy examples`

        If `execution_context.live_trading` is true then this function is called for
        every execution cycle. If we are backtesting, then this function is
        called only once at the start of backtesting and the `decide_trades`
        need to deal with new and deprecated trading pairs.

        As we are only trading a single pair, load data for the single pair only.

        :param ts:
            The timestamp of the trading cycle. For live trading,
            `create_trading_universe` is called on every cycle.
            For backtesting, it is only called at the start

        :param client:
            Trading Strategy Python client instance.

        :param execution_context:
            Information how the strategy is executed. E.g.
            if we are live trading or not.

        :param options:
            Allow the backtest framework override what candle size is used to backtest the strategy
            without editing the strategy Python source code file.

        :return:
            This function must return :py:class:`TradingStrategyUniverse` instance
            filled with the data for exchanges, pairs and candles needed to decide trades.
            The trading universe also contains information about the reserve asset,
            usually stablecoin, we use for the strategy.
            """


def pregenerated_create_trading_universe(universe: TradingStrategyUniverse) -> CreateTradingUniverseProtocol:
    """Wrap existing trading universe, so it can be passed around for universe generators."""

    def _inner(timestamp: pandas.Timestamp,
            client: Optional[Client],
            execution_context: ExecutionContext,
            candle_time_frame_override: Optional[TimeBucket]=None):
        return universe

    return _inner


@dataclass
class StrategyModuleInformation:
    """Describe elements that we need to have in a strategy module.

    The class variables are the same name as found in the Python strategy module.
    They can be uppercase or lowercase - all strategy module variables
    are exported as lowercase.

    """

    #: Path to the module
    #:
    path: Path

    #: The source code of the strategy
    #:
    #: Can be set `None` for strategies that are not public.
    #:
    source_code: Optional[str]

    #: The engine version this strategy requires.
    #:
    #: Function signatures and results may be changed depending
    #: on for engine version the strategy is for.
    #:
    #: See :py:data:`SUPPORTED_TRADING_STRATEGY_ENGINE_VERSIONS`.
    #:
    trading_strategy_engine_version: TradingStrategyEngineVersion

    #: Enable different strategy scripts.
    #:
    #: If not set default to ``StrategyType.managed_positions``.
    #:
    trading_strategy_type: StrategyType

    trading_strategy_cycle: CycleDuration

    decide_trades: DecideTradesProtocol | DecideTradesProtocol2

    #: If `execution_context.live_trading` is true then this function is called for
    #: every execution cycle. If we are backtesting, then this function is
    #: called only once at the start of backtesting and the `decide_trades`
    #: need to deal with new and deprecated trading pairs.
    create_trading_universe: CreateTradingUniverseProtocol

    #: Routing hinting.
    #:
    #: Legacy option: most strategies can set this in
    #: ``create_trading_universe()``. Default to ``TradeRouting.default``.
    #:
    trade_routing: Optional[TradeRouting] = None

    #: Blockchain id on which this strategy operates
    #:
    #: Valid for single chain strategies only
    chain_id: Optional[ChainId] = None

    #: What currency we use for the strategy.
    #:
    #: Can be left out in new versions
    #: and is set in ``create_trading_universe()``
    #:
    reserve_currency: Optional[ReserveCurrency] = None

    #: Only needed for backtests
    backtest_start: Optional[datetime.datetime] = None

    #: Only needed for backtests
    backtest_end: Optional[datetime.datetime] = None

    #: Only needed for backtests
    initial_cash: Optional[float] = None

    #: Strategy name
    #:
    #: Note that this must be also separately configured in the
    #: frontend user interface, as name
    #: is used before the strategy metadata is loaded.
    #:
    name: Optional[str] = None

    #: Strategy short description
    #:
    #: 1 sentence description
    #:
    short_description: Optional[str] = None

    #: Strategy long description
    #:
    #: Two paragraph description, may contain HTML.
    #:
    long_description: Optional[str] = None

    #: Strategy icon
    #:
    #: Two paragraph description, may contain HTML.
    #:
    icon: Optional[str] = None

    def is_version_greater_or_equal_than(self, major: int, minor: int, patch: int) -> bool:
        """Check strategy module for its version compatibility."""
        assert self.trading_strategy_engine_version, f"Strategy module does not contain trading_strategy_engine_version varible: {self.path}"
        required_version = f"{major}.{minor}.{patch}"
        return version.parse(self.trading_strategy_engine_version) >= version.parse(required_version)

    def validate(self):
        """Check that the user inputted variable names look good.

        :raise StrategyModuleNotValid:
            If we could not load/parse strategy module for some reason
        """

        if not self.trading_strategy_engine_version:
            raise StrategyModuleNotValid(f"trading_strategy_engine_version missing in the module {self.path}")

        if not type(self.trading_strategy_engine_version) == str:
            raise StrategyModuleNotValid(f"trading_strategy_engine_version is not string")

        if self.trading_strategy_engine_version not in SUPPORTED_TRADING_STRATEGY_ENGINE_VERSIONS:
            raise StrategyModuleNotValid(f"Only versions {SUPPORTED_TRADING_STRATEGY_ENGINE_VERSIONS} supported, got {self.trading_strategy_engine_version}")

        if not self.trading_strategy_type:
            raise StrategyModuleNotValid(f"trading_strategy_type missing in the module")

        if not isinstance(self.trading_strategy_type, StrategyType):
            raise StrategyModuleNotValid(f"trading_strategy_type not StrategyType instance")

        if not isinstance(self.trading_strategy_cycle, CycleDuration):
            raise StrategyModuleNotValid(f"trading_strategy_cycle not CycleDuration instance, got {self.trading_strategy_cycle}")

        if self.trade_routing is None:
            raise StrategyModuleNotValid(f"trade_routing missing on the strategy")

        if not isinstance(self.trade_routing, TradeRouting):
            raise StrategyModuleNotValid(f"trade_routing not TradeRouting instance, got {self.trade_routing}")

        if not isinstance(self.decide_trades, Callable):
            raise StrategyModuleNotValid(f"decide_trades function missing/invalid")

        if not isinstance(self.create_trading_universe, Callable):
            raise StrategyModuleNotValid(f"create_trading_universe function missing/invalid")

        if self.trade_routing is None:
            raise StrategyModuleNotValid(f"trade_routing missing on the strategy")

        if self.chain_id:
            assert isinstance(self.chain_id, ChainId), f"Strategy module chain_in varaible expected ChainId instance, got {self.chain_id}"

        if self.backtest_start:
            assert isinstance(self.backtest_start, datetime.datetime), f"Strategy module backtest_start variable, expected datetime.datetime instance, got {self.backtest_start}"

        if self.backtest_end:
            assert isinstance(self.backtest_end, datetime.datetime), f"Strategy module backtest_start variable, expected datetime.datetime instance, got {self.backtest_end}"

        if self.initial_cash:
            assert type(self.initial_cash) in (int, float), f"Strategy module initial_cash variable, expected float instance, got {self.initial_cash.__class__}"

        if self.name:
            assert type(self.name) == str

        if self.short_description:
            assert type(self.short_description) == str

        if self.long_description:
            assert type(self.long_description) == str

        if self.icon:
            result = urlparse(self.icon)
            assert all([result.scheme, result.netloc]), f"Bad icon URL: {self.icon}"

    def validate_backtest(self):
        """Validate that the module is backtest runnable."""
        self.validate()
        assert self.backtest_start is not None, f"BACKTEST_START variable is not set in the strategy module {self.path}"
        assert self.backtest_end is not None, f"BACKTEST_END variable is not set in the strategy module {self.path}"
        assert self.initial_cash is not None, f"INITIAL_CASH variable is not set in the strategy module {self.path}"

    def get_universe_options(self) -> UniverseOptions:
        """What backtest range this strategy defaults to"""
        return UniverseOptions(start_at=self.backtest_start, end_at=self.backtest_end)


def parse_strategy_module(
        path,
        python_module_exports: dict,
        source_code: Optional[str]=None) -> StrategyModuleInformation:
    """Parse a loaded .py module that describes a trading strategy.

    :param python_module_exports:
        Python module

    """

    assert isinstance(path, Path)

    return StrategyModuleInformation(
        path,
        source_code,
        trading_strategy_engine_version=python_module_exports.get("trading_strategy_engine_version"),
        trading_strategy_type=python_module_exports.get("trading_strategy_type", StrategyType.managed_positions),
        trading_strategy_cycle=python_module_exports.get("trading_strategy_cycle"),
        trade_routing=python_module_exports.get("trade_routing", TradeRouting.default),
        reserve_currency=python_module_exports.get("reserve_currency"),
        decide_trades=python_module_exports.get("decide_trades"),
        create_trading_universe=python_module_exports.get("create_trading_universe"),
        chain_id=python_module_exports.get("chain_id"),
        backtest_start=_ensure_dt(python_module_exports.get("backtest_start")),
        backtest_end=_ensure_dt(python_module_exports.get("backtest_end")),
        initial_cash=python_module_exports.get("initial_cash"),
        name=python_module_exports.get("name"),
        short_description=python_module_exports.get("short_description"),
        long_description=python_module_exports.get("long_description"),
    )


def read_strategy_module(path: Path) -> Union[StrategyModuleInformation, StrategyFactory]:
    """Loads a strategy module and returns its factor function.

    Reads .py file, checks it is a valid strategy module
    and then returns :py:class`StrategyModuleInformation` describing
    the strategy.

    :return:
        StrategyModuleInformation instance. For legacy strategies
        (used for unit test coverage only), we return a factory.

    """
    logger.info("Reading strategy %s", path)

    assert isinstance(path, Path)

    strategy_exports = runpy.run_path(path.as_posix())

    with open(path.as_posix(), "rt") as inp:
        source_code = inp.read()

    # For user convenience, make everything case-insentitive,
    # assume lowercase from no on
    strategy_exports = {k.lower(): v for k, v in strategy_exports.items()}

    version = strategy_exports.get("trading_strategy_engine_version")

    if version is None:
        # Legacy path.
        # Legacy strategy modules lack version information.
        # TODO: Remove when all unit tests have been migrated to new strategy files.
        strategy_runner = strategy_exports.get("strategy_factory")
        if strategy_runner is None:
            raise StrategyModuleNotValid(f"{path} Python module does not declare trading_strategy_engine_version or strategy_factory variables")

        return strategy_runner

    logger.info("Strategy module %s, engine version %s", path, version)

    mod_info = parse_strategy_module(path, strategy_exports, source_code)
    mod_info.validate()

    return mod_info


def _ensure_dt(v: datetime.datetime | pd.Timestamp | None) -> datetime.datetime | None:
    """Allow pd.Timestamp in a file but use datetime internally"""
    if isinstance(v, pd.Timestamp):
        return v.to_pydatetime()
    return v
