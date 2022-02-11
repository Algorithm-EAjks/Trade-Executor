"""Test trading against faux Uniswap pool."""

import datetime
import secrets
from decimal import Decimal
from typing import List

import pytest
from eth_account import Account
from eth_account.signers.local import LocalAccount
from eth_typing import HexAddress
from hexbytes import HexBytes
from web3 import EthereumTesterProvider, Web3
from web3.contract import Contract

from smart_contracts_for_testing.abi import get_deployed_contract
from smart_contracts_for_testing.hotwallet import HotWallet
from smart_contracts_for_testing.token import create_token
from smart_contracts_for_testing.uniswap_v2 import UniswapV2Deployment, deploy_uniswap_v2_like, deploy_trading_pair, \
    estimate_received_quantity
from tradeexecutor.ethereum.execution import prepare_swaps, broadcast, wait_trades_to_complete, resolve_trades, \
    approve_tokens, confirm_approvals
from tradeexecutor.ethereum.wallet import sync_reserves, sync_portfolio
from tradeexecutor.state.state import AssetIdentifier, Portfolio, State, TradingPairIdentifier, TradeStatus
from tradeexecutor.testing.ethereumtrader import EthereumTestTrader
from tradeexecutor.testing.trader import TestTrader


@pytest.fixture
def tester_provider():
    # https://web3py.readthedocs.io/en/stable/examples.html#contract-unit-tests-in-python
    return EthereumTesterProvider()


@pytest.fixture
def eth_tester(tester_provider):
    # https://web3py.readthedocs.io/en/stable/examples.html#contract-unit-tests-in-python
    return tester_provider.ethereum_tester


@pytest.fixture
def web3(tester_provider):
    """Set up a local unit testing blockchain."""
    # https://web3py.readthedocs.io/en/stable/examples.html#contract-unit-tests-in-python
    return Web3(tester_provider)


@pytest.fixture
def chain_id(web3) -> int:
    """The test chain id (67)."""
    return web3.eth.chain_id


@pytest.fixture()
def deployer(web3) -> HexAddress:
    """Deploy account.

    Do some account allocation for tests.
    """
    return web3.eth.accounts[0]


@pytest.fixture()
def hot_wallet_private_key(web3) -> HexBytes:
    """Generate a private key"""
    return HexBytes(secrets.token_bytes(32))


@pytest.fixture
def usdc_token(web3, deployer: HexAddress) -> Contract:
    """Create USDC with 10M supply."""
    token = create_token(web3, deployer, "Fake USDC coin", "USDC", 10_000_000 * 10**6, 6)
    return token


@pytest.fixture
def usdc(usdc_token, web3) -> AssetIdentifier:
    """Mock some assets"""
    return AssetIdentifier(web3.eth.chain_id, usdc_token.address, "USDC", 6)


@pytest.fixture
def weth(web3) -> AssetIdentifier:
    """Mock some assets"""
    return AssetIdentifier(web3.eth.chain_id, "0x1", "WETH", 18)


@pytest.fixture()
def uniswap_v2(web3, deployer) -> UniswapV2Deployment:
    """Uniswap v2 deployment."""
    deployment = deploy_uniswap_v2_like(web3, deployer)
    return deployment


@pytest.fixture
def weth_token(uniswap_v2: UniswapV2Deployment) -> Contract:
    """Mock some assets"""
    return uniswap_v2.weth


@pytest.fixture
def asset_usdc(usdc_token, chain_id) -> AssetIdentifier:
    """Mock some assets"""
    return AssetIdentifier(chain_id, usdc_token.address, usdc_token.functions.symbol().call(), usdc_token.functions.decimals().call())


@pytest.fixture
def asset_weth(weth_token, chain_id) -> AssetIdentifier:
    """Mock some assets"""
    return AssetIdentifier(chain_id, weth_token.address, weth_token.functions.symbol().call(), weth_token.functions.decimals().call())


@pytest.fixture
def uniswap_trading_pair(web3, deployer, uniswap_v2, weth_token, usdc_token) -> HexAddress:
    """WETH-USDC pool with 1.7M liquidity."""
    pair_address = deploy_trading_pair(
        web3,
        deployer,
        uniswap_v2,
        weth_token,
        usdc_token,
        1000 * 10**18,  # 1000 ETH liquidity
        1_700_000 * 10**6,  # 1.7M USDC liquidity
    )
    return pair_address


@pytest.fixture
def weth_usdc_pair(uniswap_trading_pair, asset_usdc, asset_weth) -> TradingPairIdentifier:
    return TradingPairIdentifier(asset_weth, asset_usdc, uniswap_trading_pair)


@pytest.fixture
def start_ts() -> datetime.datetime:
    """Timestamp of action started"""
    return datetime.datetime(2022, 1, 1, tzinfo=None)


@pytest.fixture
def supported_reserves(usdc) -> List[AssetIdentifier]:
    """Timestamp of action started"""
    return [usdc]


@pytest.fixture()
def hot_wallet(web3: Web3, usdc_token: Contract, hot_wallet_private_key: HexBytes, deployer: HexAddress) -> HotWallet:
    """Our trading Ethereum account.

    Start with 10,000 USDC cash and 2 ETH.
    """
    account = Account.from_key(hot_wallet_private_key)
    web3.eth.send_transaction({"from": deployer, "to": account.address, "value": 2*10**18})
    usdc_token.functions.transfer(account.address, 10_000 * 10**6).transact({"from": deployer})
    wallet = HotWallet(account)
    wallet.sync_nonce(web3)
    return wallet


@pytest.fixture
def supported_reserves(usdc) -> List[AssetIdentifier]:
    """The reserve currencies we support."""
    return [usdc]


@pytest.fixture()
def portfolio(web3, usdc, hot_wallet, start_ts, supported_reserves) -> Portfolio:
    """A portfolio loaded with the initial cash"""
    portfolio = Portfolio()
    events = sync_reserves(web3, 1, start_ts, hot_wallet.address, [], supported_reserves)
    sync_portfolio(portfolio, events)
    return portfolio


@pytest.fixture()
def state(portfolio) -> State:
    return State(portfolio=portfolio)


def test_execute_trade_instructions_buy_weth(
        web3: Web3,
        state: State,
        uniswap_v2: UniswapV2Deployment,
        hot_wallet: HotWallet,
        usdc_token: AssetIdentifier,
        weth_token: AssetIdentifier,
        weth_usdc_pair: TradingPairIdentifier,
        start_ts: datetime.datetime):
    """Sync reserves from one deposit."""

    portfolio = state.portfolio

    # We have everything in cash
    assert portfolio.get_total_equity() == 10_000
    assert portfolio.get_current_cash() == 10_000

    # Buy 500 USDC worth of WETH
    trader = TestTrader(state)

    buy_amount = 500

    # Estimate price
    raw_assumed_quantity = estimate_received_quantity(web3, uniswap_v2, weth_token, usdc_token, buy_amount * 10 ** 6)
    assumed_quantity = Decimal(raw_assumed_quantity) / Decimal(10**18)
    assert assumed_quantity == pytest.approx(Decimal(0.293149332386944192))

    # 1: plan
    position, trade = trader.prepare_buy(weth_usdc_pair, assumed_quantity, 1700)
    assert state.portfolio.get_total_equity() == pytest.approx(10000.0)
    assert trade.get_status() == TradeStatus.planned

    ts = start_ts + datetime.timedelta(seconds=1)

    # Approvals
    approvals = approve_tokens(
        web3,
        uniswap_v2,
        hot_wallet,
        [trade]
    )

    # 2: prepare
    # Prepare transactions
    prepare_swaps(
        web3,
        hot_wallet,
        uniswap_v2,
        ts,
        state,
        [trade]
    )

    # approve() + swapExactTokensForTokens()
    assert hot_wallet.current_nonce == 2

    assert trade.get_status() == TradeStatus.started
    assert trade.tx_info.tx_hash is not None
    assert trade.tx_info.details["from"] == hot_wallet.address
    assert trade.tx_info.signed_bytes is not None
    assert trade.tx_info.nonce == 1

    #: 3 broadcast

    # Handle approvals separately for now
    confirm_approvals(web3, approvals)

    ts = start_ts + datetime.timedelta(seconds=1)
    broadcasted = broadcast(web3, ts, [trade])
    assert trade.get_status() == TradeStatus.broadcasted
    assert trade.broadcasted_at is not None

    #: 4 process results
    ts = start_ts + datetime.timedelta(seconds=1)
    receipts = wait_trades_to_complete(web3, [trade])
    resolve_trades(
        web3,
        uniswap_v2,
        ts,
        state,
        broadcasted,
        receipts)

    assert trade.get_status() == TradeStatus.success
    assert trade.executed_price == pytest.approx(Decimal(1705.6136999031144))
    assert trade.executed_quantity == pytest.approx(Decimal(0.292184487629472304))


def test_execute_trade_instructions_buy_weth_with_tester(
        web3: Web3,
        state: State,
        uniswap_v2: UniswapV2Deployment,
        hot_wallet: HotWallet,
        usdc_token: AssetIdentifier,
        weth_token: AssetIdentifier,
        weth_usdc_pair: TradingPairIdentifier,
        start_ts: datetime.datetime):
    """Same as above but with the tester class.."""

    portfolio = state.portfolio

    # We have everything in cash
    assert portfolio.get_total_equity() == 10_000
    assert portfolio.get_current_cash() == 10_000

    # Buy 500 USDC worth of WETH
    trader = EthereumTestTrader(web3, uniswap_v2, hot_wallet, state)
    position, trade = trader.buy(weth_usdc_pair, Decimal(500), 1700.0)

    assert trade.planned_price == 1700
    assert trade.planned_quantity == pytest.approx(Decimal('0.293149332386944181'))

    assert trade.get_status() == TradeStatus.success
    assert trade.executed_price == pytest.approx(1705.6136999031144)
    assert trade.executed_quantity == pytest.approx(Decimal('0.292184487629472304'))

    # Cash balance has been deducted
    assert portfolio.get_current_cash() == pytest.approx(9501.646134942195)

    # Portfolio is correctly valued
    assert portfolio.get_total_equity() == pytest.approx(9998.359763912298)


def test_buy_sell_buy(
        web3: Web3,
        state: State,
        uniswap_v2: UniswapV2Deployment,
        hot_wallet: HotWallet,
        usdc_token: AssetIdentifier,
        weth_token: AssetIdentifier,
        weth_usdc_pair: TradingPairIdentifier,
        start_ts: datetime.datetime):
    """Execute three trades on a position."""

    # 0: start
    assert state.portfolio.get_total_equity() == 10_000

    # 1: buy 1
    position, trade = trader.buy(weth_usdc_pair, Decimal(0.1), 1700)
    assert state.portfolio.get_total_equity() == 998.3
    assert position.get_equity_for_position() == pytest.approx(Decimal(0.099))

    # 2: Sell half of the tokens
    half_1 = position.get_equity_for_position() / 2
    position, trade = trader.sell(weth_usdc, half_1, 1700)
    assert position.get_equity_for_position() == pytest.approx(Decimal(0.0495))
    assert len(position.trades) == 2

    # 3: buy more
    position, trade = trader.buy(weth_usdc, Decimal(0.1), 1700)
    assert position.get_equity_for_position() == pytest.approx(Decimal(0.1485))

    # All done
    assert len(position.trades) == 3
    assert len(state.portfolio.open_positions) == 1
