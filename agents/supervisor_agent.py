import os
from dotenv import load_dotenv
#from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_huggingface import ChatHuggingFace, HuggingFaceEndpoint
from langchain_groq import ChatGroq

load_dotenv()

# ==========================================
# 1. CONFIGURE THE SUPERVISOR LLM
# ==========================================

hf_endpoint = HuggingFaceEndpoint(
    repo_id=os.getenv("SUPERVISOR_MODEL"),
    task="text-generation",
    max_new_tokens=3000, # Length of the generated summary
    temperature=0.1,
    huggingfacehub_api_token=os.getenv("HF_TOKEN"),
)
llm = ChatHuggingFace(llm=hf_endpoint)
"""
llm = ChatGroq(
    temperature=0.1,
    model_name="openai/gpt-oss-20b",
    groq_api_key=os.getenv("GROQ_API_KEY")
)
"""


SUPERVISOR_PROMPT = """You are the Lead Software Architect.
Your team of AI agents has just finished reviewing a Pull Request. You will receive their individual reports on Security, Code Quality, Test Coverage, and Documentation.

Your objective is to synthesize these into a single, highly readable, and professionally formatted GitHub PR comment.

**Instructions:**
1. **The Executive Summary:** Provide a concise, bulleted overview synthesizing the overall health of the Pull Request. Do not just blindly list what each agent did; connect the dots across domains. Highlight the most important takeaways, including critical blockers (e.g., security flaws, severe code complexity, or missing tests) as well as major wins (e.g., pristine documentation or clean architectural additions). Use exactly as many bullet points as necessary to capture the crucial insights.
2. **Feedback Standardization:** Below the summary, present the specific findings from each agent under the provided headers. The raw reports will likely have inconsistent Markdown styles. You must standardize them into clean, scannable formatting (using consistent sub-bullets, bold text for emphasis, and proper markdown code blocks). 
3. **Data Preservation (CRITICAL):** While you must clean up the formatting and readability, you MUST NOT summarize away the technical details. You must strictly preserve all specific file paths, line numbers, error codes, and code snippets provided by the worker agents.

**Structure the output exactly like this:**

### 🛡️ MergeGuard Review Summary
[Your bulleted Executive Summary here]

---

#### 🔒 Security Analysis
[Standardized Security Feedback]

---

#### 🧹 Code Quality & Style
[Standardized Quality Feedback]

---

#### 🧪 Test Coverage
[Standardized Test-Gap Feedback]

---

#### 📚 Documentation
*(Standardize the documentation feedback. CRITICAL FORMATTING RULE: To prevent broken markdown formatting on GitHub, DO NOT nest code blocks inside bullet lists or list items. Present each missing docstring under its own flat header (e.g., `##### function_name`) with the copy-pasteable python code block directly below it, completely unindented.)*
[Standardized Docs Feedback]
"""

# ==========================================
# 2. ASYNC NODE EXECUTOR
# ==========================================


async def run_supervisor_agent(state: dict) -> dict:
    
    # Gather all the reports from the shared state
    sec_feedback = state.get("security_feedback", "No security issues detected.")
    qual_feedback = state.get("quality_feedback", "Code quality looks good.")
    test_feedback = state.get("test_feedback", "Test coverage is adequate.")
    docs_feedback = state.get("docs_feedback", "Documentation is up to standard.")
    human_feedback = state.get("human_feedback", "")
    previous_report = state.get("final_report", "")
    
    # 1. Refinement Loop: If human feedback is provided, rewrite the previous report
    if human_feedback and previous_report:
        print("🔄 Refining report using human feedback...")
        refinement_prompt = f"""You are the Lead Software Architect.
A human reviewer has analyzed your generated Pull Request review report and provided feedback for modifications.

Your task is to rewrite the previous report, incorporating the human's feedback precisely while keeping the overall structure and format identical to the original.

**CRITICAL DIRECTIVE:**
1)**Absolute Priority:**The human feedback is the highest priority instruction. You must strictly prioritize and implement all changes requested by the human, overriding any previous agent findings or styling choices if they conflict with the human's directives. Do not ignore any part of the human's feedback.
2)**No Meta-Commentary:** Do not include any conversational text, apologies, or explanations of what you changed. The output must seamlessly look like the first draft was never written. It should be silent editing.

**Original Report:**
{previous_report}

**Human Feedback / Requested Changes:**
{human_feedback}

Output the fully updated, finalized report. Maintain all formatting standards (no nested code blocks, consistent headers, data preservation). Do not add any conversational text or explanation outside the markdown report."""
        response = await llm.ainvoke([
            ("system", refinement_prompt),
            ("user", "Please update the report according to the feedback above.")
        ])
    
    # 2. Standard flow: If no human feedback is provided, do the initial synthesis
    else:
        user_message = f"""
        Here are the reports from your agents:
        
        [SECURITY REPORT]:
        {sec_feedback}
        
        [QUALITY REPORT]:
        {qual_feedback}
        
        [TEST COVERAGE REPORT]:
        {test_feedback}
        
        [DOCUMENTATION REPORT]:
        {docs_feedback}
        """
        
        response = await llm.ainvoke([
            ("system", SUPERVISOR_PROMPT),
            ("user", user_message)
        ])
        
    return {"final_report": response.content}