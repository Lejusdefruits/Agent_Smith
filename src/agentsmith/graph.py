import sys
sys.path.insert(0, "vendor")

from notlonggraph.graph import StateGraph
from notlonggraph.constants import START, END
from agentsmith.state import State
from agentsmith.nodes import planner, generator, synthesizer, executor, tester, verifier


def build_graph():
    g = StateGraph(State)
    
    g.add_node("planner", planner.plan)
    g.add_node("generator", generator.generate)
    g.add_node("synthesizer", synthesizer.synthesize)
    g.add_node("executor", executor.execute)
    g.add_node("tester", tester.test)
    g.add_node("verifier", verifier.verify)
    
    g.add_edge(START, "planner")
    g.add_edge("planner", "generator")
    g.add_edge("generator", "synthesizer")
    g.add_edge("synthesizer", "executor")
    g.add_edge("executor", "tester")
    g.add_edge("tester", "verifier")
    g.add_edge("verifier", END)
    
    g.add_conditional_edge("verifier", verifier.should_retry, {
        True: "generator",
        False: END,
    })
    
    return g.compile()
