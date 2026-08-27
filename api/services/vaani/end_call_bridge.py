"""Make Vaani's decision to end a call actually hang up the phone.

The bug
-------
Server runs 1, 3 and 4 all recorded `user_hangup`. Run 3 turn 6 the agent
delivered a proper goodbye -- "ధన్యవాదాలు, మంచి రోజు!" -- and then held the line
open until Vishnu hung up himself. Not one function-call event appears in any
call log.

The agent had TWO ways to end a call and neither one finished the job:

1. **Dograh's `end_call` tool.** Created, attached to the agent, active, and
   registered with the LLM (`compose_functions_for_node` adds it whenever the
   node has `tool_uuids` and the CustomToolManager exists, which it always
   does). The Layer 3 prompt even says "When the conversation is finished, use
   the end call tool."

   But Vaani's compiled prompt appends a MODE protocol that instructs the model
   to declare the end as TEXT:

       OUTPUT FORMAT — the first line of your reply is always exactly one of:
       MODE: ASK / MODE: CLOSE / MODE: END

   The model obeys the format it was given and never reaches for the tool.

2. **Vaani's `MODE: END`.** `ReplyFilter` parses the line and sets
   `state.must_end = True`. Grepping every reader of that flag:

       guardrails.py:178   must_close()   -> only changes guardrail checking
       state.py:94         render()       -> only changes the next prompt

   **Nothing called `end_call_with_reason`. Nothing hung up.**

Why bridge rather than delete the MODE protocol
-----------------------------------------------
MODE is the cheaper mechanism and it already works. It costs about three tokens
and arrives inline with the reply, whereas a tool call is a second LLM
round-trip on the critical path -- the opposite of what this agent needs. The
protocol is also load-bearing for the "never END on an engaged caller" rules the
model is held to. So the signal stays; this supplies the missing actuator.

Timing
------
Hanging up on `must_end` alone would cut the goodbye off mid-word. The trigger
is `BotStoppedSpeakingFrame`, which the transport emits once the audio has
actually finished playing out, so the caller hears the whole line first.
"""

from __future__ import annotations

from loguru import logger

from pipecat.frames.frames import BotStoppedSpeakingFrame, Frame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

# Matches EndTaskReason.END_CALL, so the run is dispositioned the same way a
# tool-driven hangup would be rather than inventing a new code.
END_REASON = "end_call"


class EndCallBridge(FrameProcessor):
    """Ends the call once Vaani has said goodbye."""

    def __init__(self, *, state, engine) -> None:
        super().__init__()
        self._state = state
        self._engine = engine
        self._fired = False

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        # Push first: the frame must keep flowing even if disposal is slow, and
        # nothing below this processor should be starved by the hangup path.
        await self.push_frame(frame, direction)

        if self._fired or not isinstance(frame, BotStoppedSpeakingFrame):
            return
        if not getattr(self._state, "must_end", False):
            return

        # The transport emits BotStoppedSpeaking both upstream and downstream,
        # so without this latch disposal would be entered twice.
        self._fired = True
        try:
            logger.info("[end-call] MODE: END and the goodbye has played -- ending the call")
            await self._engine.end_call_with_reason(END_REASON)
        except Exception as e:
            # A failure here must never take the call down uncleanly; the caller
            # can still hang up, and the run is recorded either way.
            logger.error(f"[end-call] hangup failed: {e!r}")
