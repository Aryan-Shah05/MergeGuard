import os
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from langchain_groq import ChatGroq
from langgraph.graph import StateGraph, START, END
import logging
# Import the actual agent runners
from agents.security_agent import run_security_agent
from agents.quality_agent import run_quality_agent
from agents.test_gap_agent import run_test_gap_agent
from agents.doc_agent import run_doc_agent
from agents.supervisor_agent import run_supervisor_agent

# Load environment variables (.env)
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)

# 1. Initialize the Groq LLM (Using Llama 3.1 70B for high-quality reasoning)
llm = ChatGroq(
    temperature=0.1,  # Low temperature keeps the code review deterministic and factual
    model_name="llama-3.1-70b-versatile",
    groq_api_key=os.getenv("GROQ_API_KEY")
)

# 2. Define the Shared State
# This object flows through every agent. Each agent reads from it and adds its findings.
class PRReviewState(BaseModel):
    repo_name: str = Field(default="", description="The full name of the GitHub repository")
    pr_number: int = Field(default=0, description="The PR number being reviewed")
    pr_diff: str = Field(default="", description="The raw code changes to analyze")
    coverage_json: str = Field(default="The raw JSON coverage report string from the Pytest CI pipeline")
    
    # Agent Feedbacks
    security_feedback: str = Field(default="", description="Findings from the Security Agent")
    quality_feedback: str = Field(default="", description="Findings from the Quality Agent")
    test_feedback: str = Field(default="", description="Findings from the Test-Gap Agent")
    docs_feedback: str = Field(default="", description="Findings from the Documentation Agent")
    
    human_feedback: str = Field(default="", description="Feedback from human reviewer")
    # Final Output
    final_report: str = Field(default="", description="The aggregated markdown report")

# 3. Define Agent Nodes (Async)
async def security_agent(state: PRReviewState):
    #print("--- Security Agent Analyzing Code ---")
    logger.info("Security Agent started.")
    pr_diff = state.pr_diff if hasattr(state, "pr_diff") else state.get("pr_diff", "")
    report = await run_security_agent(pr_diff)
    #print("Security Agent Report:", report, end="\n\n")
    #logger.info("Security Agent Report: %s", report)
    return {"security_feedback": report}

async def quality_agent(state: PRReviewState):
    #print("--- Quality Agent Analyzing Code ---")
    logger.info("Quality Agent started.")
    repo_name = state.repo_name if hasattr(state, "repo_name") else state.get("repo_name", "")
    pr_number = state.pr_number if hasattr(state, "pr_number") else state.get("pr_number", 0)
    pr_diff = state.pr_diff if hasattr(state, "pr_diff") else state.get("pr_diff", "")
    
    # Convert github repository slash notation into SonarQube's project key format
    sonar_key = os.getenv("SONAR_PROJECT_KEY") or repo_name.replace("/", "_")
    report = await run_quality_agent(
        sonar_project_key=sonar_key,
        pr_number=pr_number,
        diff_text=pr_diff
    )
    #print("Quality Agent Report:", report, end="\n\n")
    #logger.info("Quality Agent Report: %s", report)
    return {"quality_feedback": report}

async def test_gap_agent(state: PRReviewState):
    #print("--- Test-Gap Agent Analyzing Code ---")
    logger.info("Test-Gap Agent started.")
    # Convert state to dictionary to match run_test_gap_agent expectations
    state_dict = state.model_dump() if hasattr(state, "model_dump") else dict(state)
    result = await run_test_gap_agent(state_dict)
    #print("Test-Gap Agent Report:", result, end="\n\n")
    #logger.info("Test-Gap Agent Report: %s", result)
    return result

async def doc_agent(state: PRReviewState):
    #print("--- Documentation Agent Analyzing Code ---")
    logger.info("Documentation Agent started.")
    # Convert state to dictionary to match run_doc_agent expectations
    state_dict = state.model_dump() if hasattr(state, "model_dump") else dict(state)
    result = await run_doc_agent(state_dict)
    #print("Documentation Agent Report:", result, end="\n\n")
    #logger.info("Documentation Agent Report: %s", result)
    return result

async def supervisor_agent(state: PRReviewState):
    #print("--- Supervisor Agent Synthesizing Final Report ---")
    logger.info("Supervisor Agent started.")
    # Convert state to dictionary to match run_supervisor_agent expectations
    state_dict = state.model_dump() if hasattr(state, "model_dump") else dict(state)
    result = await run_supervisor_agent(state_dict)
    return result

# 1. Initialize the Graph with our strict Pydantic State
workflow = StateGraph(PRReviewState)

# 2. Add the Nodes (The Agents)
workflow.add_node("security_agent", security_agent)
workflow.add_node("quality_agent", quality_agent)
workflow.add_node("test_gap_agent", test_gap_agent)
workflow.add_node("doc_agent", doc_agent)
workflow.add_node("supervisor_agent", supervisor_agent)

# 3. Connect the Edges (The Workflow Logic)
# FAN-OUT: Trigger all 4 specialists simultaneously
workflow.add_edge(START, "security_agent")
workflow.add_edge(START, "quality_agent")
workflow.add_edge(START, "test_gap_agent")
workflow.add_edge(START, "doc_agent")

# FAN-IN: All specialists send their output to the Supervisor
workflow.add_edge("security_agent", "supervisor_agent")
workflow.add_edge("quality_agent", "supervisor_agent")
workflow.add_edge("test_gap_agent", "supervisor_agent")
workflow.add_edge("doc_agent", "supervisor_agent")

# End the process after the supervisor finishes its markdown report
workflow.add_edge("supervisor_agent", END)

# 4. Compile the Graph into an executable application
mergeguard_app = workflow.compile()

