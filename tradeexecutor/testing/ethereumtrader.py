"""Ethereum test trading."""

import datetime
from decimal import Decimal
from typing import Tuple

from web3 import Web3

from smart_contracts_for_testing.abi import get_deployed_contract
from smart_contracts_for_testing.hotwallet import HotWallet
from smart_contracts_for_testing.uniswap_v2 import UniswapV2Deployment, estimate_received_quantity
from tradeexecutor.ethereum.execution import approve_tokens, prepare_swaps, confirm_approvals, broadcast, \
    wait_trades_to_complete, resolve_trades
from tradeexecutor.state.state import State, TradingPairIdentifier, TradeType, TradeExecution, TradeStatus
from tradingstrategy.analysis.tradeanalyzer import TradePosition


class EthereumTestTrader:
    """Helper class to trade against EthereumTester unit testing network."""

    def __init__(self, web3: Web3, uniswap: UniswapV2Deployment, hot_wallet: HotWallet, state: State):
        self.web3 = web3
        self.uniswap = uniswap
        self.state = state
        self.hot_wallet = hot_wallet

        self.ts = datetime.datetime(2022, 1, 1, tzinfo=None)
        self.lp_fees = 2.50  # $2.5
        self.gas_units_consumed = 150_000  # 150k gas units per swap
        self.gas_price = 15 * 10**9  # 15 Gwei/gas unit

        self.native_token_price = 1

    def execute(self, trade: TradeExecution) -> Tuple[TradePosition, TradeExecution]:

        # 2. Capital allocation

        # Approvals
        approvals = approve_tokens(
            self.web3,
            self.uniswap,
            self.hot_wallet,
            [trade]
        )

        # 2: prepare
        # Prepare transactions
        prepare_swaps(
            self.web3,
            self.hot_wallet,
            self.uniswap,
            self.ts,
            self.state,
            [trade]
        )

        #: 3 broadcast

        # Handle approvals separately for now
        confirm_approvals(self.web3, approvals)

        self.ts += datetime.timedelta(seconds=1)

        broadcasted = broadcast(self.web3, self.ts, [trade])
        assert trade.get_status() == TradeStatus.broadcasted

        # Resolve
        self.ts += datetime.timedelta(seconds=1)
        receipts = wait_trades_to_complete(self.web3, [trade])
        resolve_trades(
            self.web3,
            self.uniswap,
            self.ts,
            self.state,
            broadcasted,
            receipts)

    def buy(self, pair: TradingPairIdentifier, amount_in_usd: Decimal, assumed_price: float) -> Tuple[TradePosition, TradeExecution]:
        # Estimate buy price
        base_token = get_deployed_contract(self.web3, "ERC20MockDecimals.json", pair.base.address)
        quote_token = get_deployed_contract(self.web3, "ERC20MockDecimals.json", pair.quote.address)

        #base_token_decimals = base_token.functions.decimals().call()
        #quote_token_decimals = quote_token.functions.decimals().call()
        raw_assumed_quantity = estimate_received_quantity(self.web3, self.uniswap, base_token, quote_token, amount_in_usd * (10**pair.quote.decimals))
        # assumed_quantity = Decimal(raw_assumed_quantity) / Decimal(10**18)

        # Estimate price
        assumed_quantity = Decimal(raw_assumed_quantity) / Decimal(10**pair.base.decimals)

        # 1. Plan
        position, trade = self.state.create_trade(
            ts=self.ts,
            pair=pair,
            quantity=assumed_quantity,
            assumed_price=assumed_price,
            trade_type=TradeType.rebalance,
            reserve_currency=pair.quote,
            reserve_currency_price=1.0)

        self.execute(trade)
        return position, trade

    def sell(self, pair, quantity, price) -> Tuple[TradePosition, TradeExecution]:
        return self.create_and_execute(pair, -quantity, price)
