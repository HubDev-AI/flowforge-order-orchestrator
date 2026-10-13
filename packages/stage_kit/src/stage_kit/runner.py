from bus import MessageBus, consume_forever
from contracts import Command, CommandKind, Outcome, Result, ScriptToken

from stage_kit.handler import StageHandler
from stage_kit.simulator import Simulator

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
        await self._bus.publish_result(result)
        return result

    async def run(self) -> None:
        await consume_forever(lambda: self._bus.consume_commands(self._stage), self._handle)

    async def _handle(self, command: Command) -> None:
        await self.process(command)

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
    return Result(
        order_id=command.order_id,
        stage=command.stage,
        attempt=command.attempt,
        kind=command.kind,
        outcome=outcome,
        **fields,
    )
