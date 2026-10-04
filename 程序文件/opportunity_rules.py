"""Pure V1 opportunity-fund rules shared by live advice and backtests.

The module deliberately contains no database, network, pandas, or UI code.  It
implements the versioned price rules from ``VOO / QQQM Opportunity Fund Rules
V1.0`` and the conservative combined-stage policy used by the local tool:

* VOO and QQQM drawdowns are classified independently.
* The current combined stage is the higher of the two symbol stages.
* Each numbered stage can be executed only once in a combined drawdown cycle.
* A skipped move opens only the lowest still-unexecuted stage.
* A used cycle resets only after every configured symbol has subsequently made
  a strict new 52-week closing high.
* Every deployment is allocated by the profile weights (80/20 in V1), rather
  than by current portfolio drift.

All monetary and percentage arithmetic uses :class:`~decimal.Decimal`.  Stage
deployment fractions apply to the opportunity cash available immediately before
that stage is executed.  With the V1 fractions, executing every stage therefore
leaves 31.5% of the starting cash before later monthly contributions.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
from types import MappingProxyType
from typing import Mapping, Sequence


RULE_FAMILY = "voo_qqqm_opportunity_fund"
RULE_VERSION = "1.1"
DEFAULT_PROFILE_KEY = "default"
DEFAULT_WINDOW = 252
CENT = Decimal("0.01")
ZERO = Decimal("0")
ONE = Decimal("1")


def _decimal(value: object, label: str = "value") -> Decimal:
    """Convert a scalar to a finite Decimal without accepting booleans."""
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not result.is_finite():
        raise ValueError(f"{label} must be a finite number")
    return result


def _decimal_text(value: Decimal) -> str:
    """Return one canonical, non-exponent Decimal representation."""
    if value == 0:
        return "0"
    text = format(value.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _market_day(value: date | str | None) -> str | None:
    if value is None:
        return None
    try:
        parsed = value if isinstance(value, date) else date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError("market_date must be YYYY-MM-DD") from exc
    return parsed.isoformat()


@dataclass(frozen=True)
class RuleProfile:
    """Immutable, hashable inputs for one opportunity-rule profile.

    ``thresholds_pct`` contains positive drawdown percentages.  Deployment and
    allocation values are fractions, so ``Decimal("0.20")`` means 20%.
    Tuple storage keeps a profile deeply immutable and gives rule hashing a
    stable order across Python processes.
    """

    key: str
    label: str
    version: str
    thresholds_pct: tuple[tuple[str, tuple[Decimal, ...]], ...]
    deployment_fractions: tuple[Decimal, ...]
    allocation_fractions: tuple[tuple[str, Decimal], ...]

    def __post_init__(self) -> None:
        if not self.key or not self.version:
            raise ValueError("profile key and version are required")
        symbols = tuple(symbol for symbol, _ in self.thresholds_pct)
        if not symbols or len(set(symbols)) != len(symbols):
            raise ValueError("profile symbols must be unique")
        if len(self.deployment_fractions) == 0:
            raise ValueError("at least one deployment stage is required")
        stage_count = len(self.deployment_fractions)
        for symbol, thresholds in self.thresholds_pct:
            if len(thresholds) != stage_count:
                raise ValueError(f"{symbol} must define {stage_count} thresholds")
            if any(value <= 0 for value in thresholds):
                raise ValueError(f"{symbol} thresholds must be positive")
            if any(left >= right for left, right in zip(thresholds, thresholds[1:])):
                raise ValueError(f"{symbol} thresholds must be strictly increasing")
        if any(value <= 0 or value > 1 for value in self.deployment_fractions):
            raise ValueError("deployment fractions must be in (0, 1]")
        allocation_symbols = tuple(symbol for symbol, _ in self.allocation_fractions)
        if allocation_symbols != symbols:
            raise ValueError("allocation symbols and threshold symbols must match in order")
        if any(value < 0 for _, value in self.allocation_fractions):
            raise ValueError("allocation fractions cannot be negative")
        if sum((value for _, value in self.allocation_fractions), ZERO) != ONE:
            raise ValueError("allocation fractions must total 1")

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(symbol for symbol, _ in self.thresholds_pct)

    @property
    def stage_count(self) -> int:
        return len(self.deployment_fractions)

    def thresholds_for(self, symbol: str) -> tuple[Decimal, ...]:
        for configured_symbol, thresholds in self.thresholds_pct:
            if configured_symbol == symbol:
                return thresholds
        raise KeyError(f"symbol {symbol!r} is not configured")

    def deployment_fraction(self, stage: int) -> Decimal:
        if type(stage) is not int or not 1 <= stage <= self.stage_count:
            raise ValueError(f"stage must be between 1 and {self.stage_count}")
        return self.deployment_fractions[stage - 1]

    def as_payload(self) -> dict:
        """Return the complete JSON-friendly rule payload used for hashing."""
        return {
            "family": RULE_FAMILY,
            "version": self.version,
            "profile": self.key,
            "thresholds_pct": {
                symbol: [_decimal_text(value) for value in thresholds]
                for symbol, thresholds in self.thresholds_pct
            },
            "deployment_fractions": [
                _decimal_text(value) for value in self.deployment_fractions
            ],
            "deployment_base": "pre_execution_available_cash",
            "allocation_fractions": {
                symbol: _decimal_text(value)
                for symbol, value in self.allocation_fractions
            },
            "stage_scope": "combined",
            "stage_selection": "lowest_unexecuted_at_or_below_current",
            "reset": "all_symbols_strict_new_252_day_closing_high_after_execution",
        }


def _profile(
    key: str,
    label: str,
    voo: tuple[str, str, str, str],
    qqqm: tuple[str, str, str, str],
) -> RuleProfile:
    return RuleProfile(
        key=key,
        label=label,
        version=RULE_VERSION,
        thresholds_pct=(
            ("VOO", tuple(Decimal(value) for value in voo)),
            ("QQQM", tuple(Decimal(value) for value in qqqm)),
        ),
        deployment_fractions=tuple(
            Decimal(value) for value in ("0.20", "0.25", "0.25", "0.30")
        ),
        allocation_fractions=(("VOO", Decimal("0.80")), ("QQQM", Decimal("0.20"))),
    )


PROFILES = MappingProxyType({
    "sensitive": _profile(
        "sensitive", "较敏感", ("5", "10", "15", "20"), ("8", "12", "18", "25")
    ),
    "default": _profile(
        "default", "V1 默认", ("8", "12", "18", "25"), ("10", "15", "22", "30")
    ),
    "conservative": _profile(
        "conservative", "更保守", ("10", "15", "20", "30"), ("12", "18", "25", "35")
    ),
})


def get_profile(profile: str | RuleProfile = DEFAULT_PROFILE_KEY) -> RuleProfile:
    """Resolve a profile key while allowing callers to pass a custom profile."""
    if isinstance(profile, RuleProfile):
        return profile
    try:
        return PROFILES[str(profile)]
    except KeyError as exc:
        choices = ", ".join(PROFILES)
        raise ValueError(f"unknown opportunity profile {profile!r}; choose {choices}") from exc


def rule_hash(
    profile: str | RuleProfile = DEFAULT_PROFILE_KEY,
    extra_parameters: Mapping[str, object] | None = None,
) -> str:
    """Return a stable SHA-256 identifier for rules and material overrides.

    A caller should persist this value with a cycle and every execution.  A
    changed hash must never silently inherit old stage locks.
    """
    payload = get_profile(profile).as_payload()
    if extra_parameters:
        payload["extra_parameters"] = _canonical_value(extra_parameters)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_value(value: object) -> object:
    if isinstance(value, Decimal):
        return _decimal_text(value)
    if isinstance(value, Mapping):
        return {str(key): _canonical_value(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (tuple, list)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise TypeError(f"unsupported rule-hash value: {type(value).__name__}")


@dataclass(frozen=True)
class RollingCloseSignal:
    """One no-lookahead close/high/drawdown observation."""

    index: int
    close: Decimal
    rolling_high: Decimal
    drawdown_pct: Decimal
    prior_rolling_high: Decimal | None
    strict_new_high: bool
    window: int


def rolling_close_signal(
    closes: Sequence[object],
    index: int | None = None,
    *,
    window: int = DEFAULT_WINDOW,
) -> RollingCloseSignal:
    """Calculate an inclusive rolling drawdown and strict new-high flag.

    Only values at or before ``index`` are inspected.  Drawdown uses the last
    ``window`` closes including the current close.  A strict new high requires a
    separate full ``window`` of prior closes and therefore at least
    ``window + 1`` observations.  Equality with the prior high is not a reset.
    """
    if type(window) is not int or window < 2:
        raise ValueError("window must be an integer of at least 2")
    if len(closes) == 0:
        raise ValueError("closes cannot be empty")
    resolved_index = len(closes) - 1 if index is None else index
    if type(resolved_index) is not int or not 0 <= resolved_index < len(closes):
        raise IndexError("index is outside closes")
    if resolved_index + 1 < window:
        raise ValueError(f"at least {window} closes through index are required")

    inclusive_values = tuple(
        _decimal(value, "close")
        for value in closes[resolved_index - window + 1:resolved_index + 1]
    )
    if any(value <= 0 for value in inclusive_values):
        raise ValueError("closes must be positive")
    current = inclusive_values[-1]
    rolling_high = max(inclusive_values)
    drawdown_pct = (ONE - current / rolling_high) * Decimal("100")

    prior_high = None
    strict_new_high = False
    if resolved_index >= window:
        prior_values = tuple(
            _decimal(value, "close")
            for value in closes[resolved_index - window:resolved_index]
        )
        if any(value <= 0 for value in prior_values):
            raise ValueError("closes must be positive")
        prior_high = max(prior_values)
        strict_new_high = current > prior_high

    return RollingCloseSignal(
        index=resolved_index,
        close=current,
        rolling_high=rolling_high,
        drawdown_pct=drawdown_pct,
        prior_rolling_high=prior_high,
        strict_new_high=strict_new_high,
        window=window,
    )


def strict_rolling_new_high(
    closes: Sequence[object],
    index: int | None = None,
    *,
    window: int = DEFAULT_WINDOW,
) -> bool:
    """Convenience wrapper for strict, no-lookahead rolling-high detection."""
    return rolling_close_signal(closes, index, window=window).strict_new_high


def stage_for_drawdown(
    symbol: str,
    drawdown_pct: object,
    profile: str | RuleProfile = DEFAULT_PROFILE_KEY,
) -> int:
    """Map a nonnegative drawdown percentage to stage 0..N inclusively."""
    configured = get_profile(profile)
    drawdown = _decimal(drawdown_pct, "drawdown_pct")
    if drawdown < 0:
        raise ValueError("drawdown_pct cannot be negative")
    return sum(drawdown >= threshold for threshold in configured.thresholds_for(symbol))


def next_lowest_unexecuted_stage(
    current_stage: int,
    executed_stages: Sequence[int],
    *,
    stage_count: int = 4,
) -> int | None:
    """Return the next sequential stage without stacking gap moves.

    For example, a jump directly to stage 3 with no prior executions returns
    stage 1.  At most one returned stage should be opened per completed market
    date by the caller.
    """
    if type(stage_count) is not int or stage_count < 1:
        raise ValueError("stage_count must be a positive integer")
    if type(current_stage) is not int or not 0 <= current_stage <= stage_count:
        raise ValueError(f"current_stage must be between 0 and {stage_count}")
    executed = set(executed_stages)
    if any(type(stage) is not int or not 1 <= stage <= stage_count for stage in executed):
        raise ValueError(f"executed stages must be between 1 and {stage_count}")
    return next((stage for stage in range(1, current_stage + 1) if stage not in executed), None)


@dataclass(frozen=True)
class CombinedCycleState:
    """Serializable state for the conservative combined-stage cycle."""

    cycle_number: int = 1
    executed_stages: tuple[int, ...] = ()
    new_high_symbols: tuple[str, ...] = ()
    last_market_date: str | None = None

    def __post_init__(self) -> None:
        if type(self.cycle_number) is not int or self.cycle_number < 1:
            raise ValueError("cycle_number must be a positive integer")
        if self.executed_stages != tuple(sorted(set(self.executed_stages))):
            raise ValueError("executed_stages must be sorted and unique")
        if self.new_high_symbols != tuple(sorted(set(self.new_high_symbols))):
            raise ValueError("new_high_symbols must be sorted and unique")
        _market_day(self.last_market_date)

    def as_dict(self) -> dict:
        return {
            "cycle_number": self.cycle_number,
            "executed_stages": list(self.executed_stages),
            "new_high_symbols": list(self.new_high_symbols),
            "last_market_date": self.last_market_date,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object] | None) -> "CombinedCycleState":
        """Restore state from JSON-compatible storage; ``None`` starts cycle 1."""
        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise ValueError("combined cycle state must be a mapping")
        cycle_number = value.get("cycle_number", 1)
        if isinstance(cycle_number, bool):
            raise ValueError("cycle_number must be a positive integer")
        try:
            cycle_number = int(cycle_number)
            executed = tuple(sorted({int(item) for item in value.get("executed_stages", ())}))
            highs = tuple(sorted({str(item) for item in value.get("new_high_symbols", ())}))
        except (TypeError, ValueError) as exc:
            raise ValueError("combined cycle state contains invalid values") from exc
        return cls(
            cycle_number=cycle_number,
            executed_stages=executed,
            new_high_symbols=highs,
            last_market_date=_market_day(value.get("last_market_date") or None),
        )


@dataclass(frozen=True)
class StageEvaluation:
    """Current per-symbol stages and the single next combined candidate."""

    drawdowns_pct: tuple[tuple[str, Decimal], ...]
    symbol_stages: tuple[tuple[str, int], ...]
    combined_stage: int
    next_stage: int | None
    trigger_symbols: tuple[str, ...]
    deployment_fraction: Decimal

    def stage_for(self, symbol: str) -> int:
        for configured_symbol, stage in self.symbol_stages:
            if configured_symbol == symbol:
                return stage
        raise KeyError(symbol)


def evaluate_combined_stage(
    drawdowns_pct: Mapping[str, object],
    state: CombinedCycleState,
    profile: str | RuleProfile = DEFAULT_PROFILE_KEY,
) -> StageEvaluation:
    """Evaluate independent thresholds and select one combined next stage."""
    configured = get_profile(profile)
    missing = set(configured.symbols) - set(drawdowns_pct)
    extra = set(drawdowns_pct) - set(configured.symbols)
    if missing or extra:
        raise ValueError(
            f"drawdowns must contain exactly {', '.join(configured.symbols)}"
        )
    normalized = tuple(
        (symbol, _decimal(drawdowns_pct[symbol], f"{symbol} drawdown"))
        for symbol in configured.symbols
    )
    stages = tuple(
        (symbol, stage_for_drawdown(symbol, drawdown, configured))
        for symbol, drawdown in normalized
    )
    combined = max((stage for _, stage in stages), default=0)
    candidate = next_lowest_unexecuted_stage(
        combined, state.executed_stages, stage_count=configured.stage_count
    )
    triggers = tuple(
        symbol for symbol, stage in stages
        if candidate is not None and stage >= candidate
    )
    fraction = configured.deployment_fraction(candidate) if candidate else ZERO
    return StageEvaluation(
        drawdowns_pct=normalized,
        symbol_stages=stages,
        combined_stage=combined,
        next_stage=candidate,
        trigger_symbols=triggers,
        deployment_fraction=fraction,
    )


def record_stage_execution(
    state: CombinedCycleState,
    stage: int,
    *,
    current_stage: int,
    market_date: date | str | None = None,
    stage_count: int = 4,
) -> CombinedCycleState:
    """Lock exactly the currently eligible lowest stage after real execution."""
    expected = next_lowest_unexecuted_stage(
        current_stage, state.executed_stages, stage_count=stage_count
    )
    if expected is None:
        raise ValueError("no stage is currently eligible for execution")
    if stage != expected:
        raise ValueError(f"stage {stage} cannot execute now; the next stage is {expected}")
    resolved_date = _market_day(market_date) or state.last_market_date
    if state.last_market_date and resolved_date and resolved_date < state.last_market_date:
        raise ValueError("execution market_date cannot move backwards")
    return replace(
        state,
        executed_stages=tuple(sorted((*state.executed_stages, stage))),
        last_market_date=resolved_date,
    )


@dataclass(frozen=True)
class CycleUpdate:
    state: CombinedCycleState
    reset_happened: bool
    completed_cycle_number: int | None = None


def observe_cycle_day(
    state: CombinedCycleState,
    market_date: date | str,
    strict_new_high_symbols: Sequence[str],
    *,
    required_symbols: Sequence[str] = ("VOO", "QQQM"),
) -> CycleUpdate:
    """Apply one completed market day's reset observations idempotently.

    New highs before the first stage execution are deliberately ignored.  Once
    any stage is locked, all required symbols must independently make a strict
    rolling new high before the combined stage locks are cleared.
    """
    day = _market_day(market_date)
    assert day is not None
    required = tuple(required_symbols)
    if not required or len(set(required)) != len(required):
        raise ValueError("required_symbols must be nonempty and unique")
    observed = set(strict_new_high_symbols)
    unknown = observed - set(required)
    if unknown:
        raise ValueError(f"unknown new-high symbols: {', '.join(sorted(unknown))}")
    if state.last_market_date:
        if day < state.last_market_date:
            raise ValueError("market_date cannot move backwards")
        if day == state.last_market_date:
            return CycleUpdate(state=state, reset_happened=False)

    if not state.executed_stages:
        return CycleUpdate(
            state=replace(state, new_high_symbols=(), last_market_date=day),
            reset_happened=False,
        )

    accumulated = set(state.new_high_symbols) | observed
    if accumulated.issuperset(required):
        completed = state.cycle_number
        return CycleUpdate(
            state=CombinedCycleState(
                cycle_number=completed + 1,
                executed_stages=(),
                new_high_symbols=(),
                last_market_date=day,
            ),
            reset_happened=True,
            completed_cycle_number=completed,
        )
    return CycleUpdate(
        state=replace(
            state,
            new_high_symbols=tuple(sorted(accumulated)),
            last_market_date=day,
        ),
        reset_happened=False,
    )


def stage_cash_amount(
    available_cash: object,
    stage: int,
    profile: str | RuleProfile = DEFAULT_PROFILE_KEY,
    *,
    risk_multiplier: object = ONE,
    quantum: Decimal = CENT,
) -> Decimal:
    """Return a rounded stage cap from pre-execution available cash."""
    configured = get_profile(profile)
    cash = _decimal(available_cash, "available_cash")
    multiplier = _decimal(risk_multiplier, "risk_multiplier")
    if cash < 0:
        raise ValueError("available_cash cannot be negative")
    if multiplier < 0 or multiplier > 1:
        raise ValueError("risk_multiplier must be between 0 and 1")
    if quantum <= 0:
        raise ValueError("quantum must be positive")
    amount = cash * configured.deployment_fraction(stage) * multiplier
    return amount.quantize(quantum, rounding=ROUND_HALF_UP)


def allocate_amount(
    total: object,
    allocation_fractions: Sequence[tuple[str, Decimal]],
    *,
    quantum: Decimal = CENT,
) -> dict[str, Decimal]:
    """Allocate a rounded total exactly; the last symbol receives remainder."""
    amount = _decimal(total, "total")
    if amount < 0:
        raise ValueError("total cannot be negative")
    if quantum <= 0:
        raise ValueError("quantum must be positive")
    weights = tuple((symbol, _decimal(weight, f"{symbol} weight"))
                    for symbol, weight in allocation_fractions)
    if not weights or len({symbol for symbol, _ in weights}) != len(weights):
        raise ValueError("allocation symbols must be nonempty and unique")
    if any(weight < 0 for _, weight in weights) or sum(
        (weight for _, weight in weights), ZERO
    ) != ONE:
        raise ValueError("allocation weights must be nonnegative and total 1")
    rounded_total = amount.quantize(quantum, rounding=ROUND_HALF_UP)
    result: dict[str, Decimal] = {}
    assigned = ZERO
    for index, (symbol, weight) in enumerate(weights):
        part = (rounded_total - assigned if index == len(weights) - 1 else
                (rounded_total * weight).quantize(quantum, rounding=ROUND_HALF_UP))
        result[symbol] = part
        assigned += part
    return result


def allocate_80_20(total: object, *, quantum: Decimal = CENT) -> dict[str, Decimal]:
    """Allocate an amount exactly to VOO 80% and QQQM 20%."""
    return allocate_amount(
        total,
        (("VOO", Decimal("0.80")), ("QQQM", Decimal("0.20"))),
        quantum=quantum,
    )


RISK_ACTION_MULTIPLIERS = MappingProxyType({
    "full": Decimal("1"),
    "half": Decimal("0.5"),
    "pause": Decimal("0"),
})


def multiplier_for_risk_action(
    action: str,
    *,
    half_multiplier: object = Decimal("0.5"),
) -> Decimal:
    """Translate ``full``/``half``/``pause`` to a bounded Decimal multiplier."""
    if action not in RISK_ACTION_MULTIPLIERS:
        raise ValueError("risk action must be full, half, or pause")
    if action != "half":
        return RISK_ACTION_MULTIPLIERS[action]
    value = _decimal(half_multiplier, "half_multiplier")
    if value <= 0 or value >= 1:
        raise ValueError("half_multiplier must be strictly between 0 and 1")
    return value


@dataclass(frozen=True)
class RiskDecision:
    """Conservative structured review result used to gate an eligible stage."""

    action: str
    multiplier: Decimal
    complete: bool
    reasons: tuple[str, ...]


def conservative_risk_decision(
    review: Mapping[str, object] | None,
    *,
    half_multiplier: object = Decimal("0.5"),
    require_note: bool = True,
) -> RiskDecision:
    """Map a manual V1 risk review to full, half, or pause.

    Required status values are:

    * ``earnings_status``: ``stable``, ``uncertain``, ``deteriorating``
    * ``liquidity_status``: ``normal``, ``stressed``, ``crisis``
    * ``rate_status``: ``normal``, ``elevated``, ``rapid_rise``

    Missing fields, a missing required note, or absent explicit confirmation are
    incomplete and conservatively pause deployment.  Deteriorating earnings or
    a liquidity crisis also pause.  Any uncertain/stressed/elevated condition
    reduces the stage to the configurable half multiplier.  Otherwise it is
    full.  VIX is intentionally not an automatic price-stage input.
    """
    if review is None:
        return RiskDecision("pause", ZERO, False, ("risk review is missing",))
    allowed = {
        "earnings_status": {"stable", "uncertain", "deteriorating"},
        "liquidity_status": {"normal", "stressed", "crisis"},
        "rate_status": {"normal", "elevated", "rapid_rise"},
    }
    missing: list[str] = []
    values: dict[str, str] = {}
    for field, choices in allowed.items():
        raw = review.get(field)
        if raw in (None, ""):
            missing.append(field)
            continue
        value = str(raw)
        if value not in choices:
            raise ValueError(f"invalid {field}: {value!r}")
        values[field] = value
    if review.get("manual_confirmation") is not True:
        missing.append("manual_confirmation")
    if require_note and not str(review.get("note") or "").strip():
        missing.append("note")
    if missing:
        return RiskDecision(
            "pause", ZERO, False,
            tuple(f"missing {field}" for field in missing),
        )

    pause_reasons = []
    if values["earnings_status"] == "deteriorating":
        pause_reasons.append("earnings outlook is deteriorating")
    if values["liquidity_status"] == "crisis":
        pause_reasons.append("liquidity or financial system is in crisis")
    if pause_reasons:
        return RiskDecision("pause", ZERO, True, tuple(pause_reasons))

    half_reasons = []
    if values["earnings_status"] == "uncertain":
        half_reasons.append("earnings outlook is uncertain")
    if values["liquidity_status"] == "stressed":
        half_reasons.append("liquidity is stressed")
    if values["rate_status"] in {"elevated", "rapid_rise"}:
        half_reasons.append("rate pressure is elevated")
    if half_reasons:
        return RiskDecision(
            "half",
            multiplier_for_risk_action("half", half_multiplier=half_multiplier),
            True,
            tuple(half_reasons),
        )
    return RiskDecision("full", ONE, True, ("risk review passed",))


__all__ = [
    "CENT",
    "CombinedCycleState",
    "CycleUpdate",
    "DEFAULT_PROFILE_KEY",
    "DEFAULT_WINDOW",
    "PROFILES",
    "RULE_FAMILY",
    "RULE_VERSION",
    "RiskDecision",
    "RollingCloseSignal",
    "RuleProfile",
    "StageEvaluation",
    "allocate_80_20",
    "allocate_amount",
    "conservative_risk_decision",
    "evaluate_combined_stage",
    "get_profile",
    "multiplier_for_risk_action",
    "next_lowest_unexecuted_stage",
    "observe_cycle_day",
    "record_stage_execution",
    "rolling_close_signal",
    "rule_hash",
    "stage_cash_amount",
    "stage_for_drawdown",
    "strict_rolling_new_high",
]
