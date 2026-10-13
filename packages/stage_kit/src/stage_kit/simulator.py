import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from contracts import Command, ScriptToken

TIMEOUT_MARGIN_SECONDS = 0.01


@dataclass(frozen=True, slots=True)
class Profile:
    latency_min_s: float
    latency_max_s: float
    transient_probability: float
    reject_probability: float


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Simulator:
    def __init__(
        self,
        profile: Profile,
        rng: random.Random,
        *,
        clock: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._profile = profile
        self._rng = rng
        self._clock = clock
        self._sleep = sleep

    async def decide(self, command: Command) -> ScriptToken:
        scripted = command.script.root.get(command.stage)
        if scripted is None:
            return await self._by_profile()
        tokens = scripted.get(command.kind, [])
        token = tokens[command.attempt - 1] if command.attempt <= len(tokens) else None
        if token is ScriptToken.TIMEOUT:
            remaining = (command.deadline_at - self._clock()).total_seconds()
            await self._sleep(max(0.0, remaining) + TIMEOUT_MARGIN_SECONDS)
            return ScriptToken.SUCCEED
        return token or ScriptToken.SUCCEED

    async def _by_profile(self) -> ScriptToken:
        profile = self._profile
        await self._sleep(self._rng.uniform(profile.latency_min_s, profile.latency_max_s))
        roll = self._rng.random()
        if roll < profile.reject_probability:
            return ScriptToken.REJECT
        if roll < profile.reject_probability + profile.transient_probability:
            return ScriptToken.RETRY
        return ScriptToken.SUCCEED
