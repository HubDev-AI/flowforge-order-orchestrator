import logging

from bus import MessageBus, consume_forever
from contracts import AttemptKey, Command, CommandKind, Outcome, Result, ScriptToken

from stage_kit.handler import StageHandler
from stage_kit.simulator import Simulator

logger = logging.getLogger(__name__)

SIMULATED_TRANSIENT = "simulated transient failure"
SIMULATED_REJECTION = "simulated rejection"


class StageRunner:
    def __init__(
        self, stage: str, handler: StageHandler, simulator: Simulator, bus: MessageBus
    ) -> None:
        self._stage = stage
        self._handler = handler
        self._simulator = simulator
        self._bus = bus

    async def process(self, command: Command) -> Result:
        result = await self._outcome_of(command)
        logger.info(
            "order %s %s %s attempt %s: %s",
            command.order_id,
            command.stage,
            command.kind,
            command.attempt,
            result.outcome,
        )
        await self._bus.publish_result(result)
        return result

    async def run(self) -> None:
        await consume_forever(lambda: self._bus.consume_commands(self._stage), self.process)

    async def _outcome_of(self, command: Command) -> Result:
        match await self._simulator.decide(command):
            case ScriptToken.RETRY:
                return _result(command, Outcome.RETRY, reason=SIMULATED_TRANSIENT)
            case ScriptToken.REJECT:
                return _result(command, Outcome.REJECTED, reason=SIMULATED_REJECTION)
        work = self._handler.undo if command.kind is CommandKind.UNDO else self._handler.execute
        try:
            output = await work(command)
        except Exception as error:
            return _result(command, Outcome.RETRY, reason=type(error).__name__)
        return _result(command, Outcome.SUCCEEDED, stage_output=output)


def _result(command: Command, outcome: Outcome, **fields) -> Result:
    identity = command.model_dump(include=set(AttemptKey.model_fields))
    return Result(**identity, outcome=outcome, **fields)
