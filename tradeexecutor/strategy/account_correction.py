"""Apply accounting corrections on the strategy state.

- Read on-chain balances

- Compare them to the balances seen in the state

- Adjust statebalacnes to match chain based ones

- Generate the accounting events to reflect these changes

"""
import logging
import datetime
import enum
from _decimal import Decimal
from dataclasses import dataclass
from typing import List, Iterable, Collection, Tuple

import pandas as pd
from eth_defi.enzyme.erc20 import prepare_transfer
from eth_defi.enzyme.vault import Vault
from eth_defi.hotwallet import HotWallet
from eth_defi.token import fetch_erc20_details
from eth_defi.trace import assert_transaction_success_with_explanation
from eth_typing import HexAddress
from tradingstrategy.pair import PandasPairUniverse

from tradeexecutor.ethereum.enzyme.vault import EnzymeVaultSyncModel
from tradeexecutor.state.balance_update import BalanceUpdate, BalanceUpdatePositionType, BalanceUpdateCause
from tradeexecutor.state.identifier import AssetIdentifier
from tradeexecutor.state.position import TradingPosition
from tradeexecutor.state.reserve import ReservePosition
from tradeexecutor.state.state import State
from tradeexecutor.state.sync import BalanceEventRef
from tradeexecutor.state.types import USDollarAmount
from tradeexecutor.strategy.asset import get_relevant_assets, map_onchain_asset_to_position
from tradeexecutor.strategy.sync_model import SyncModel


logger = logging.getLogger(__name__)


#: The amount of token units that is considered "dust" or rounding error.
#:
DUST_EPSILON = Decimal(10**-5)


class UnexpectedAccountingCorrectionIssue(Exception):
    """Something wrong in the token accounting we do not expect to be automatically correct."""


class AccountingCorrectionType(enum.Enum):

    #: Do not know what caused the incorrect amount
    unknown = "unknown"

    #: aUSDC
    rebase = "rebase"


class AccountingCorrectionAborted(Exception):
    """User presses n"""


@dataclass
class AccountingBalanceCheck:
    """Accounting correction applied to a balance.

    Any irregular accounting correction will cause the position profit calcualtions
    and such to become invalid. Such positions should be separately market
    and not included in the profit calculations.
    """

    type: AccountingCorrectionType

    #: Related on-chain asset
    asset: AssetIdentifier

    #: Related position
    #:
    #: Set none if no open position was found
    position: TradingPosition | ReservePosition | None

    expected_amount: Decimal

    actual_amount: Decimal

    #: Used epsilon
    epsilon: Decimal

    block_number: int | None

    timestamp: datetime.datetime | None

    #: Keep track of monetary value of corrections.
    #:
    #: An estimated value at the time of the correction creation.
    #:
    #: Negative for negative corrections
    #:
    #: `None` if the the tokens are for a new position and we do not have pricing information yet availble.
    #:
    usd_value: USDollarAmount | None

    #: Is this correction for reserve asset
    #:
    reserve_asset: bool

    #: Was there a balance mismatch that is larger than the epsilon
    #:
    mismatch: bool

    def __repr__(self):
        return f"<Accounting correction type {self.type.value} for {self.position}, expected {self.expected_amount}, actual {self.actual_amount} at {self.timestamp}>"

    @property
    def quantity(self):
        """How many tokens we corrected"""
        return self.actual_amount - self.expected_amount


def calculate_account_corrections(
    pair_universe: PandasPairUniverse,
    reserve_assets: Collection[AssetIdentifier],
    state: State,
    sync_model: SyncModel,
    epsilon=DUST_EPSILON,
    all_balances=False,
) -> Iterable[AccountingBalanceCheck]:
    """Figure out differences between our internal ledger (state) and on-chain balances.

    :param pair_universe:
        Needed to know what asses we are looking for


    :param reserve_assets:
        Needed to know what asses we are looking for

    :param state:
        The current state of the internal ledger

    :param sync_model:
        How ot access on-chain balances

    :param epsilon:
        Minimum amount of token (abs quantity) before it is considered as a rounding error

    :param all_balances:
        If `True` iterate all balances even if there are no mismatch.

    :raise UnexpectedAccountingCorrectionIssue:
        If we find on-chain tokens we do not know how to map any of our strategy positions

    :return:
        Difference in balances or all balances if `all_balances` is true.
    """

    assert isinstance(pair_universe, PandasPairUniverse)
    assert isinstance(state, State)
    assert len(state.portfolio.reserves) > 0, "No reserve positions. Did you run init for the strategy?"

    logger.info("Scanning for account corrections")

    assets = get_relevant_assets(pair_universe, reserve_assets, state)
    asset_balances = list(sync_model.fetch_onchain_balances(assets))

    logger.info("Found %d on-chain tokens", len(asset_balances))

    for ab in asset_balances:

        reserve = ab.asset in reserve_assets

        position = map_onchain_asset_to_position(ab.asset, state)

        if isinstance(position, TradingPosition):
            if position.is_closed():
                raise UnexpectedAccountingCorrectionIssue(f"Mapped found tokens to already closed position:\n"
                                                          f"{ab}\n"
                                                          f"{position}")

        actual_amount = ab.amount
        expected_amount = position.get_quantity() if position else 0
        diff = actual_amount - expected_amount

        usd_value = position.calculate_quantity_usd_value(diff) if position else None

        logger.debug("Correction check worth of %s worth of %f USD, actual amount %s, expected amount %s", ab.asset, usd_value or 0, actual_amount, expected_amount)

        mismatch = abs(diff) > epsilon

        if mismatch or all_balances:
            yield AccountingBalanceCheck(
                AccountingCorrectionType.unknown,
                ab.asset,
                position,
                expected_amount,
                actual_amount,
                epsilon,
                ab.block_number,
                ab.timestamp,
                usd_value,
                reserve,
                mismatch,
            )


def apply_accounting_correction(
        state: State,
        correction: AccountingBalanceCheck,
        strategy_cycle_included_at: datetime.datetime | None,
):
    """Update the state to reflect the true on-chain balances."""

    assert correction.type == AccountingCorrectionType.unknown, f"Not supported: {correction}"
    assert correction.timestamp

    portfolio = state.portfolio
    asset = correction.asset
    position = correction.position
    block_number = correction.block_number

    event_id = portfolio.next_balance_update_id
    portfolio.next_balance_update_id += 1

    logger.info("Corrected %s", position)

    if isinstance(position, TradingPosition):
        position_type = BalanceUpdatePositionType.open_position
        position_id = correction.position.position_id
    elif isinstance(position, ReservePosition):
        position_type = BalanceUpdatePositionType.reserve
        position_id = None
    elif position is None:
        # Tokens were for a trading position, but no position was open.
        # Open a new position
        portfolio.create_trade(
            strategy_cycle_at=strategy_cycle_included_at,
        )
    else:
        raise NotImplementedError()

    notes = f"Accounting correction based on the actual on-chain balances.\n" \
        f"The internal ledger balance was  {correction.expected_amount} {asset.token_symbol}\n" \
        f"On-chain balance was {correction.actual_amount} {asset.token_symbol} at block {block_number or 0:,}\n" \
        f"Balance was updated {correction.quantity} {asset.token_symbol}\n"

    evt = BalanceUpdate(
        balance_update_id=event_id,
        position_type=position_type,
        cause=BalanceUpdateCause.correction,
        asset=correction.asset,
        block_mined_at=correction.timestamp,
        strategy_cycle_included_at=strategy_cycle_included_at,
        chain_id=asset.chain_id,
        old_balance=correction.actual_amount,
        usd_value=correction.usd_value,
        quantity=correction.quantity,
        owner_address=None,
        tx_hash=None,
        log_index=None,
        position_id=position_id,
        block_number=correction.block_number,
        notes=notes,
    )

    assert evt.balance_update_id not in position.balance_updates, f"Alreaddy written: {evt}"
    position.balance_updates[evt.balance_update_id] = evt

    ref = BalanceEventRef(
        balance_event_id=evt.balance_update_id,
        strategy_cycle_included_at=strategy_cycle_included_at,
        cause=evt.cause,
        position_type=position_type,
        position_id=evt.position_id,
        usd_value=evt.usd_value,
    )

    if isinstance(position, TradingPosition):
        # Balance_updates toggle is enough
        position.balance_updates[evt.balance_update_id] = evt

        # TODO: Close position if the new balance is zero
        assert position.get_quantity() > 0, "Position closing logic missing"

    elif isinstance(position, ReservePosition):
        # No fancy method to correct reserves
        position.quantity += correction.quantity
    else:
        raise NotImplementedError()

    # Bump our last updated date
    accounting = state.sync.accounting
    accounting.balance_update_refs.append(ref)
    accounting.last_updated_at = datetime.datetime.utcnow()
    accounting.last_block_scanned = evt.block_number

    return evt


def correct_accounts(
        state: State,
        corrections: List[AccountingBalanceCheck],
        strategy_cycle_included_at: datetime.datetime | None,
        interactive=True,
        vault: Vault | None = None,
        hot_wallet: HotWallet | None = None,
        unknown_token_receiver: HexAddress | str | None = None,
) -> Iterable[BalanceUpdate]:
    """Apply the accounting corrections on the state (internal ledger).

    - Change values of the underlying positions

    - Create BalanceUpdate events and store them in the state

    - Create BalanceUpdateRefs and store them in the state

    .. note::

        You need to iterate the returend iterator to have any of the corrections applied.

    :return:
        Iterator of corrections.
    """

    if vault is not None:
        assert vault.generic_adapter is not None
        assert hot_wallet is not None

    if interactive:

        for c in corrections:
            print("Correction needed:", c)

        confirmation = input("Attempt to repair [y/n]").lower()
        if confirmation != "y":
            raise AccountingCorrectionAborted()

    for correction in corrections:

        # Could not map to open position,
        # but we do not have code to open new positions yet.
        # Just deal with it by transferring away.
        if correction.position is None:
            transfer_away_assets_without_position(
                correction,
                unknown_token_receiver,
                vault,
                hot_wallet,
            )
        else:
            yield apply_accounting_correction(state, correction, strategy_cycle_included_at)


def transfer_away_assets_without_position(
    correction: AccountingBalanceCheck,
    unknown_token_receiver: HexAddress | str,
    vault: Vault,
    hot_wallet: HotWallet,
):
    """Transfer away non-reserve assets that cannot be mapped to an open position.

    TODO: Correct approach would be to open a new trading position
    directly in the correction, but it's complicated and we do not want to get there yet.

    :param correction:

    :param unknown_token_receiver:
    """

    assert correction.position is None
    assert not correction.reserve_asset

    web3 = vault.web3
    asset = correction.asset

    token = fetch_erc20_details(
        web3,
        asset.address,
    )

    logger.info(f"Transfering %s %s to %s as we could not map it to open position",
                correction.actual_amount,
                asset.token_symbol,
                unknown_token_receiver)

    args_bound_func = prepare_transfer(
        vault.deployment,
        vault,
        vault.generic_adapter,
        token.contract,
        unknown_token_receiver,
        token.convert_to_raw(correction.actual_amount),
    )

    hot_wallet.sync_nonce(web3)

    tx = args_bound_func.build_transaction({
        "chainId": web3.eth.chain_id,
        "from": hot_wallet.address,
        "gas": 450_000,
    })

    signed_tx = hot_wallet.sign_transaction_with_new_nonce(tx)

    tx_hash = web3.eth.send_raw_transaction(signed_tx.rawTransaction)
    assert_transaction_success_with_explanation(web3, tx_hash)


def check_accounts(
    pair_universe: PandasPairUniverse,
    reserve_assets: Collection[AssetIdentifier],
    state: State,
    sync_model: SyncModel,
    epsilon=DUST_EPSILON,
) -> Tuple[bool, pd.DataFrame]:
    """Get a table output of accounting corrections needed.

    :return:

        Tuple (accounts clean, accounting clean Dataframe that can be printed to the console)
    """

    clean = True
    corrections = calculate_account_corrections(
        pair_universe,
        reserve_assets,
        state,
        sync_model,
        epsilon,
        all_balances=True,
    )

    idx = []
    items = []
    for c in corrections:
        idx.append(c.asset.token_symbol)

        match c.position:
            case None:
                position_label = "No open position"
            case ReservePosition():
                position_label = "Reserves"
            case TradingPosition():
                position_label = c.position.pair.get_ticker()
            case _:
                raise NotImplementedError()

        dust = abs(c.quantity) <= DUST_EPSILON and c.quantity > 0

        items.append({
            "Address": c.asset.address,
            "Position": position_label,
            "Actual amount": c.actual_amount,
            "Expected amount": c.expected_amount,
            "Diff": c.quantity,
            "Dusty": "Y" if dust else "N",
            "Mismatch": "Y" if c.mismatch else "N",
        })

        if c.mismatch:
            clean = False

    df = pd.DataFrame(items, index=idx)
    df = df.fillna("")
    df = df.replace({pd.NaT: ""})
    return clean, df
