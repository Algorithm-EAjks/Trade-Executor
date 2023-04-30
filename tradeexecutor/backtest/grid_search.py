"""Perform a grid search ove strategy parameters to find optimal parameters."""
import datetime
import itertools
import logging
import os
import pickle
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Dict, List, Tuple, Any, Optional

import pandas as pd

import futureproof
from tradingstrategy.client import Client

from tradeexecutor.analysis.advanced_metrics import calculate_advanced_metrics
from tradeexecutor.analysis.trade_analyser import TradeSummary, build_trade_analysis
from tradeexecutor.backtest.backtest_routing import BacktestRoutingIgnoredModel
from tradeexecutor.backtest.backtest_runner import run_backtest_inline
from tradeexecutor.state.state import State
from tradeexecutor.state.types import USDollarAmount
from tradeexecutor.strategy.cycle import CycleDuration
from tradeexecutor.strategy.default_routing_options import TradeRouting
from tradeexecutor.strategy.routing import RoutingModel
from tradeexecutor.strategy.strategy_module import DecideTradesProtocol
from tradeexecutor.strategy.trading_strategy_universe import TradingStrategyUniverse
from tradeexecutor.visual.equity_curve import calculate_equity_curve, calculate_returns

logger = logging.getLogger(__name__)


@dataclass
class GridParameter:
    name: str
    value: Any

    def __post_init__(self):
        pass

    def __hash__(self):
        return hash((self.name, self.value))

    def __eq__(self, other):
        return self.name == other.name and self.value == other.value

    def to_path(self) -> str:
        """"""
        value = self.value
        if type(value) in (float, int, str):
            return f"{self.name}={self.value}"
        else:
            raise NotImplementedError(f"We do not support filename conversion for value {type(value)}={value}")


@dataclass()
class GridCombination:
    """One combination line in grid search."""

    #: In which folder we store the result files of all grid search runs
    #:
    #: Each individual combination will have its subfolder based on its parameter.
    result_path: Path

    #: Alphabetically sorted list of parameters
    parameters: Tuple[GridParameter]

    def __post_init__(self):
        assert len(self.parameters) > 0

        assert isinstance(self.result_path, Path), f"Expected Path, got {type(self.result_path)}"
        assert self.result_path.exists() and self.result_path.is_dir(), f"Not a dir: {self.result_path}"

    def __hash__(self):
        return hash(self.parameters)

    def __eq__(self, other):
        return self.parameters == other.parameters

    def get_relative_result_path(self) -> Path:
        """Get the path where the resulting state file is stored.

        Try to avoid messing with 256 character limit on filenames, thus break down as folders.
        """
        path_parts = [p.to_path() for p in self.parameters]
        return Path(os.path.join(*path_parts))

    def get_full_result_path(self) -> Path:
        """Get the path where the resulting state file is stored."""
        return self.result_path.joinpath(self.get_relative_result_path())

    def validate(self):
        """Check arguments can be serialised as fs path."""
        assert isinstance(self.get_relative_result_path(), Path)

    def as_dict(self) -> dict:
        """Get as kwargs mapping."""
        return {p.name: p.value for p in self.parameters}

    def get_label(self) -> str:
        """Human readable label for this combination"""
        return ", ".join([f"{p.name}: {p.value}" for p in self.parameters])

    def destructure(self) -> List[Any]:
        """Open parameters dict.

        This will return the arguments in the same order you pass them to :py:func:`prepare_grid_combinations`.
        """
        return [p.value for p in self.parameters]



@dataclass(slots=True, frozen=False)
class GridSearchResult:
    """Result for one grid combination."""

    #: For which grid combination this result is
    combination: GridCombination

    #: The full back test state
    state: State

    #: Calculated trade summary
    summary: TradeSummary

    #: Performance metrics
    metrics: pd.DataFrame

    #: Was this result read from the earlier run save
    cached: bool = False

    @staticmethod
    def has_result(combination: GridCombination):
        base_path = combination.result_path
        return base_path.joinpath(combination.get_full_result_path()).joinpath("result.pickle").exists()

    @staticmethod
    def load(combination: GridCombination):
        """Deserialised from the cached Python pickle."""

        base_path = combination.get_full_result_path()

        with open(base_path.joinpath("result.pickle"), "rb") as inp:
            result: GridSearchResult = pickle.load(inp)

        result.cached = True
        return result

    def save(self):
        """Serialise as Python pickle."""
        base_path = self.combination.get_full_result_path()
        base_path.mkdir(parents=True, exist_ok=True)
        with open(base_path.joinpath("result.pickle"), "wb") as out:
            pickle.dump(self, out)


class GridSearchWorker(Protocol):
    """Define how to create different strategy bodies."""

    def __call__(self, universe: TradingStrategyUniverse, combination: GridCombination) -> GridSearchResult:
        """Run a new decide_trades() strategy body based over the serach parameters.

        :param args:
        :param kwargs:
        :return:
        """


def prepare_grid_combinations(
        parameters: Dict[str, List[Any]],
        result_path: Path,
        clear_cached_results=False,
) -> List[GridCombination]:
    """Get iterable search matrix of all parameter combinations.

    - Make sure we preverse the original order of the grid search parameters.

    - Set up the folder to store the results

    :param parameters:
        A grid of parameters we will search.

    :param result_path:
        A folder where resulting state files will be stored.

    :param clear_cached_results:
        Clear any existing result files from the saved result cache.

        You need to do this if you change the strategy logic outside
        the given combination parameters, as the framework will otherwise
        serve you the old cached results.

    """

    assert isinstance(result_path, Path)

    logger.info("Preparing %d grid combinations, caching results in %s", len(parameters), result_path)

    if clear_cached_results:
        if result_path.exists():
            result_path.rmdir()

    result_path.mkdir(parents=True, exist_ok=True)

    args_lists: List[list] = []
    for name, values in parameters.items():
        args = [GridParameter(name, v) for v in values]
        args_lists.append(args)

    combinations = itertools.product(*args_lists)

    # Maintain the orignal parameter order over itertools.product()
    order = tuple(parameters.keys())
    def sort_by_order(combination: List[GridParameter]) -> Tuple[GridParameter]:
        temp = {p.name: p for p in combination}
        return tuple([temp[o] for o in order])

    combinations = [GridCombination(parameters=sort_by_order(c), result_path=result_path) for c in combinations]
    for c in combinations:
        c.validate()
    return combinations


def run_grid_combination(
        grid_search_worker: GridSearchWorker,
        universe: TradingStrategyUniverse,
        combination: GridCombination,
):
    if GridSearchResult.has_result(combination):
        result = GridSearchResult.load(combination)
        return result

    result = grid_search_worker(universe, combination)

    # Cache result for the future runs
    result.save()

    return result


def perform_grid_search(
        grid_search_worker: GridSearchWorker,
        universe: TradingStrategyUniverse,
        combinations: List[GridCombination],
        max_workers=16,
        clear_cached_results=False,
        stats: Optional[Counter] = None,
) -> List[GridSearchResult]:
    """Search different strategy parameters over a grid.

    - Run using parallel processing via threads.
      `Numoy should release GIL for threads <https://stackoverflow.com/a/40630594/315168>`__.

    - Save the resulting state files to a directory structure
      for invidual run analysis

    - If a result exists, do not perform the backtest again.
      However we still load the summary

    - Trading Strategy Universe is shared across threads to save memory.

    :param combinations:
        Prepared grid combinations.

        See :py:func:`prepare_grid_combinations`

    :param stats:
        If passed, collect run-time and unit testing statistics to this dictionary.

    :return:
        Grid search results for different combinations.

    """

    start = datetime.datetime.utcnow()

    logger.info("Performing a grid search over %s combinations, with %d threads",
                len(combinations),
                max_workers,
                )

    task_args = [(grid_search_worker, universe, c) for c in combinations]

    if max_workers > 1:

        logger.info("Doing a multiprocess grid search")
        # Do a parallel scan for the maximum speed
        #
        # Set up a futureproof task manager
        #
        # For futureproof usage see
        # https://github.com/yeraydiazdiaz/futureproof
        executor = futureproof.ThreadPoolExecutor(max_workers=max_workers)
        tm = futureproof.TaskManager(executor, error_policy=futureproof.ErrorPolicyEnum.RAISE)

        # Run the checks parallel using the thread pool
        tm.map(run_grid_combination, task_args)

        # Extract results from the parallel task queue
        results = [task.result for task in tm.as_completed()]

    else:
        logger.info("Doing a single thread grid search")
        # Do single thread - good for debuggers like pdb/ipdb
        #
        iter = itertools.starmap(run_grid_combination, task_args)

        # Force workers to finish
        results = list(iter)

    duration = datetime.datetime.utcnow() - start
    logger.info("Grid search finished in %s", duration)

    return results



def run_grid_search_backtest(
        combination: GridCombination,
        decide_trades: DecideTradesProtocol,
        universe: TradingStrategyUniverse,
        cycle_duration: Optional[CycleDuration] = None,
        start_at: Optional[datetime.datetime] = None,
        end_at: Optional[datetime.datetime] = None,
        initial_deposit: USDollarAmount = 5000.0,
        trade_routing: Optional[TradeRouting] = None,
        data_delay_tolerance: Optional[pd.Timedelta] = None,
        name: str = "backtest",
        routing_model: Optional[RoutingModel] = None,
) -> GridSearchResult:
    assert isinstance(universe, TradingStrategyUniverse)

    universe_range = universe.universe.candles.get_timestamp_range()
    if not start_at:
        start_at = universe_range[0]

    if not end_at:
        end_at = universe_range[1]

    if not cycle_duration:
        cycle_duration = CycleDuration.from_timebucket(universe.universe.candles.time_bucket)
    else:
        assert isinstance(cycle_duration, CycleDuration)

    if not routing_model:
        routing_model = BacktestRoutingIgnoredModel(universe.get_reserve_asset().address)

    # Run the test
    state, universe, debug_dump = run_backtest_inline(
        name="No stop loss",
        start_at=start_at.to_pydatetime(),
        end_at=end_at.to_pydatetime(),
        client=None,
        cycle_duration=cycle_duration,
        decide_trades=decide_trades,
        create_trading_universe=None,
        universe=universe,
        initial_deposit=initial_deposit,
        reserve_currency=None,
        trade_routing=TradeRouting.user_supplied_routing_model,
        routing_model=routing_model,
        allow_missing_fees=True,
        data_delay_tolerance=data_delay_tolerance,
    )

    analysis = build_trade_analysis(state.portfolio)
    equity = calculate_equity_curve(state)
    returns = calculate_returns(equity)
    metrics = calculate_advanced_metrics(returns)
    summary = analysis.calculate_summary_statistics()

    return GridSearchResult(
        combination=combination,
        state=state,
        summary=summary,
        metrics=metrics,
    )
