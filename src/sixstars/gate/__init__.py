"""Automatic quality gate: every rendered clip is approved or rejected here before it can be posted. There is
no human in the loop.

- `decision.decide(clip, creator, recent, settings, visual, precheck, now)`: the pure verdict.
- `gate.run_gate(clip_id)`: load the inputs, decide, store, hand over; `gate.install()` wires it up.
- `models`: the input and decision dataclasses and the reason codes.
"""
