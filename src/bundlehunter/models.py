"""Plain data types shared by the parser, analyzer, strategy and storage."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Trade:
    """One wallet's swap of one token inside one transaction."""

    signature: str
    slot: int
    block_time: int | None
    mint: str
    wallet: str
    side: str  # "buy" | "sell"
    token_amount: float  # UI units, always positive
    sol_amount: float  # SOL swapped, excluding network fee and Jito tip; positive
    fee_sol: float = 0.0
    tip_sol: float = 0.0

    @property
    def price(self) -> float:
        """SOL per token (UI units)."""
        return self.sol_amount / self.token_amount if self.token_amount else 0.0

    @property
    def cash_flow(self) -> float:
        """Signed SOL effect on the wallet incl. fee and tip (buys are negative)."""
        if self.side == "buy":
            return -(self.sol_amount + self.fee_sol + self.tip_sol)
        return self.sol_amount - self.fee_sol - self.tip_sol


@dataclass(frozen=True, slots=True)
class TokenTransfer:
    """Tokens moved wallet -> wallet without a SOL leg (a strong 'same owner' hint)."""

    signature: str
    slot: int
    mint: str
    src: str
    dst: str
    amount: float


@dataclass(frozen=True, slots=True)
class SolTransfer:
    src: str
    dst: str
    lamports: int


@dataclass(slots=True)
class ParsedTx:
    signature: str
    slot: int
    block_time: int | None
    failed: bool
    fee_payer: str
    signers: list[str]
    created_mints: list[str] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    token_transfers: list[TokenTransfer] = field(default_factory=list)
    sol_transfers: list[SolTransfer] = field(default_factory=list)
    tips_lamports: int = 0


@dataclass(frozen=True, slots=True)
class MarketEvent:
    """A normalized live (or replayed) event the strategy reacts to."""

    kind: str  # "create" | "buy" | "sell"
    mint: str
    wallet: str
    ts: float  # unix seconds
    sol_amount: float
    token_amount: float
    price: float  # SOL per token right after this event
    signature: str = ""
    name: str = ""
    symbol: str = ""
    uri: str = ""  # metadata URI (creates only)
