async def verify(state):
    pass


def should_retry(state):
    return state.get("score", 0) < 1.0
