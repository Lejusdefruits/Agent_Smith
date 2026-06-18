from typing import TypedDict, Annotated
import operator


class State(TypedDict):
    prompt: str
    plan: Annotated[list, operator.add]
    code: Annotated[list, operator.add]
    fs_snapshot: dict
    tests: Annotated[list, operator.add]
    test_output: str
    execution_output: str
    score: float
    logs: Annotated[list, operator.add]
    error: str
