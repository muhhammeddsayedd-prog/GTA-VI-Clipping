"""Title writer v2 (docs/30-titles-v2.md): facts in, five candidate titles from one model call, checked by
code, the best one applied to the clip after the gate approves it.

    facts      the facts JSON the model gets (pure builders) and the name book
    checks     the code checks, selection, features and the fallback title (pure)
    store      database reads and writes
    service    one clip end to end; `after_approval` is the gate's hook
    commands   the `sixstars titles ...` CLI
"""
