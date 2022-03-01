import datetime
from typing import List

from web3 import Web3

from eth_hentai.hotwallet import HotWallet
from eth_hentai.uniswap_v2 import UniswapV2Deployment
from tradeexecutor.ethereum.execution import approve_tokens, prepare_swaps, confirm_approvals, broadcast, \
    wait_trades_to_complete, resolve_trades
from tradeexecutor.state.state import TradeExecution, State
from tradeexecutor.strategy.execution import ExecutionModel


class UniswapV2ExecutionModel(ExecutionModel):
    """Run order execution for uniswap v2 style exchanges."""

    def __init__(self, state: State, uniswap: UniswapV2Deployment, hot_wallet: HotWallet, stop_on_execution_failure=True):
        """

        :param state:
        :param uniswap:
        :param hot_wallet:
        :param stop_on_execution_failure: Raise an exception if any of the trades fail top execute
        """
        self.state = state
        self.web3 = uniswap.web3
        self.uniswap = uniswap
        self.hot_wallet = hot_wallet
        self.stop_on_execution_failure = stop_on_execution_failure

    def execute_trades(self, ts: datetime.datetime, trades: List[TradeExecution]):

        assert isinstance(ts, datetime.datetime)

        # 2. Capital allocation
        # Approvals
        approvals = approve_tokens(
            self.web3,
            self.uniswap,
            self.hot_wallet,
            trades
        )

        # 2: prepare
        # Prepare transactions
        prepare_swaps(
            self.web3,
            self.hot_wallet,
            self.uniswap,
            ts,
            self.state,
            trades,
            underflow_check=False,
        )

        #: 3 broadcast

        # Handle approvals separately for now
        confirm_approvals(self.web3, approvals)

        broadcasted = broadcast(self.web3, ts, trades)
        #assert trade.get_status() == TradeStatus.broadcasted

        # Resolve
        receipts = wait_trades_to_complete(self.web3, trades)
        resolve_trades(
            self.web3,
            self.uniswap,
            ts,
            self.state,
            broadcasted,
            receipts)
