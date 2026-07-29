import os
from dotenv import load_dotenv
from langchain_groq import ChatGroq
from openai import AsyncOpenAI

load_dotenv()

cerebras_client = AsyncOpenAI(
    api_key=os.getenv("CEREBRAS_API_KEY"),
    base_url="https://api.cerebras.ai/v1",
)
# ==========================================
# 1. CONFIGURE THE LLM & SYSTEM PROMPT
# ==========================================
# We use a low temperature (0.1) to keep the documentation factual and consistent
llm = ChatGroq(
    temperature=0.1,
    model_name="qwen/qwen3.6-27b",
    groq_api_key=os.getenv("GROQ_API_KEY"),
    reasoning_effort=None, # keeps the think-trace short, avoids TPM overflow
    reasoning_format="hidden"
)


DOCS_SYSTEM_PROMPT = """You are an expert Technical Writer and Senior Software Quality Assurance Engineer. 
Your role is to audit code diffs to ensure they follow strict documentation best practices.

CRITICAL RULE: You MUST ONLY evaluate newly added lines of code (lines strictly starting with a '+'). You must completely ignore all context lines (lines starting with a '-' or a space).

Analyze the provided code diff and look for:
1. Newly added public functions, classes, or API routes (marked with '+') that lack descriptive docstrings.
2. Complex logical blocks (marked with '+') that lack inline comments explaining the "why".
3. Discrepancies where an updated function signature no longer matches its existing docstring.

Provide your feedback as a clear, markdown-formatted report with the following structural layout:
- **Missing/Incomplete Docstrings**: Specify the function/class name, and provide a ready-to-use PEP 257 (for Python) or language-appropriate docstring that the developer can copy-paste.
- **Inline Comment Suggestions**: Point out complex lines and provide the exact comment line to add.

If the documentation is pristine and meets all standards, strictly output: 'Documentation and comments are thorough and up to standards.'"""

# ==========================================
# 3. ASYNC NODE EXECUTOR
# ==========================================
"""
async def run_doc_agent(state: dict) -> dict:
    print("🤖 Documentation Agent waking up...")
    diff = state.get("pr_diff", "")
    

    # We format the prompt by combining the system behavior with the user payload
    full_prompt = f"{DOCS_SYSTEM_PROMPT}\n\nAnalyze this code diff:\n\n{diff}"
    
    # Trigger the model asynchronously
    response = await llm.ainvoke(full_prompt)
    
    return {"docs_feedback": response.content}

"""

async def run_doc_agent(state: dict) -> dict:
    
    diff = state.get("pr_diff", "")
    #print("Code Diff for Documentation Agent:\n", diff, end="\n\n")
    
    user_message = f"### Pull Request Code Diff:\n{diff}"
    
    # 3. Call the Cerebras API asynchronously
    response = await cerebras_client.chat.completions.create(
        model=os.getenv("DOCS_MODEL"),  # <--- Verify your exact model string here!
        temperature=0.1,
        messages=[
            {"role": "system", "content": DOCS_SYSTEM_PROMPT},
            {"role": "user", "content": user_message}
        ]
    )
    
    # Extract the text from the OpenAI-compatible response object
    final_text = response.choices[0].message.content
    
    return {"docs_feedback": final_text}