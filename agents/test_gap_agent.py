import os
import json
from dotenv import load_dotenv
from langchain_groq import ChatGroq
from typing import Dict, Any
from unidiff import PatchSet  # <-- Added unidiff import

load_dotenv()

# ==========================================
# 1. PARSE THE CLOUD DATA (Deterministic Python)
# ==========================================
def extract_missing_coverage(coverage_json_str: str) -> str:
    """Parses the Pytest JSON report to extract only files with missing test coverage."""
    if not coverage_json_str or coverage_json_str == "{}" or coverage_json_str == '""':
        return "No coverage data available from the CI pipeline."

    try:
        data = json.loads(coverage_json_str)
        files: Dict[str, Any] = data.get("files", {})
        
        summary = []
        overall = data.get("totals", {}).get("percent_covered", 0)
        summary.append(f"**Overall Project Coverage:** {round(overall, 2)}%\n")
        
        for filename, details in files.items():
            missing = details.get("missing_lines", [])
            if missing:
                percent = details.get("summary", {}).get("percent_covered", 0)
                summary.append(f"- **{filename}** ({round(percent, 2)}% covered): Missing tests for lines {missing}")
        
        if len(summary) == 1:
            return summary[0] + "\nAll executed files have 100% coverage! Excellent work."
            
        return "\n".join(summary)
    
    except Exception as e:
        return f"Error parsing coverage JSON: {str(e)}"

# ==========================================
# NEW HELPER: PARSE & CROSS-REFERENCE DIFF LINES
# ==========================================
def identify_uncovered_additions(diff_text: str, coverage_json_str: str) -> str:
    """
    Parses the unified diff to find all newly added lines and cross-references
    them programmatically against the missing coverage lines.
    """
    # 1. Extract added/modified line numbers from the diff
    added_lines_by_file = {}
    try:
        patch = PatchSet(diff_text)
        for patched_file in patch:
            if patched_file.is_binary_file or patched_file.is_removed_file:
                continue
            
            filename = patched_file.path
            lines = []
            for hunk in patched_file:
                for line in hunk:
                    if line.is_added:
                        # line.target_line_no is computed by unidiff for the new file version
                        lines.append({
                            "line_no": line.target_line_no,
                            "content": line.value.rstrip('\n')
                        })
            if lines:
                added_lines_by_file[filename] = lines
    except Exception as e:
        return f"Error parsing code diff: {str(e)}"

    # 2. Extract missing coverage line numbers
    coverage_data = {}
    if coverage_json_str and coverage_json_str != "{}" and coverage_json_str != '""':
        try:
            coverage_data = json.loads(coverage_json_str)
        except Exception:
            pass
            
    files_cov = coverage_data.get("files", {})
    
    # 3. Cross-reference
    gaps = []
    for filename, added in added_lines_by_file.items():
        file_cov = files_cov.get(filename, {})
        missing_lines = file_cov.get("missing_lines", [])
        
        # Check which added line numbers intersect with missing_lines
        uncovered = [line for line in added if line["line_no"] in missing_lines]
        
        gaps.append(f"File: **{filename}**")
        if uncovered:
            gaps.append("⚠️ The following newly added/modified lines have NO test coverage:")
            for line in uncovered:
                gaps.append(f"  - Line {line['line_no']}: `{line['content']}`")
        else:
            gaps.append("✅ All newly added/modified lines in this file are covered by tests.")
        gaps.append("")

    return "\n".join(gaps)

# ==========================================
# 2. CONFIGURE THE AGENT (The Brain)
# ==========================================
llm = ChatGroq(
    temperature=0.1, 
    model_name=os.getenv("TEST_GAP_MODEL"),
    groq_api_key=os.getenv("GROQ_API_KEY")
)

TEST_GAP_PROMPT = """You are a strict QA Engineer specialized in Test-Gap Analysis.
Your sole responsibility is to identify and report newly added or modified lines of code in a Pull Request that lack test coverage, using the provided coverage report.

You will be given:
1. The raw pull request diff.
2. The general project coverage summary.
3. A pre-calculated analysis matching your code changes to missing coverage line numbers.

### OUTPUT FORMAT
You must format your response strictly using the markdown structure below. Do not add intro/outro text, greetings, tables, or Python code blocks.

#### 🧪 Uncovered Test Gaps
* **[File Path]** ([File Coverage %] covered):
  * **Untested Lines**: [List exact line numbers/ranges that are uncovered]
  * **Untested Logic**: [1-sentence description of the logic on those lines]
  * **Test Scenario**: [1-sentence description of what to assert in a new unit test]

*(Repeat the bulleted structure above for each file that contains uncovered changes. Group all findings by file.)*

### EDGE CASES:
- **All changes are covered**: If all new/modified lines are covered, output exactly: "All newly added or modified lines are fully covered by tests."
- **No coverage data**: If the coverage data is missing or failed to parse, output exactly: "Coverage data is unavailable for this run."
- **No functional changes**: If the PR only modifies comments, documentation, workflows, or tests, output: "This Pull Request does not contain functional code additions requiring test coverage."

### CRITICAL CONSTRAINTS:
- Do NOT output any code blocks (e.g., no ```python blocks).
- Do NOT use markdown tables.
- Limit each file's summary to at most 3 bullet points as structured above."""
# ==========================================
# 3. ASYNC NODE EXECUTOR
# ==========================================
async def run_test_gap_agent(state: dict) -> dict:

    diff = state.get("pr_diff", "")
    cov_json = state.get("coverage_json", "{}")
    #print("Code Diff for Test-Gap Agent:\n", diff, end="\n\n")
    #print("Coverage JSON for Test-Gap Agent:\n", cov_json, end="\n\n")
    
    if not cov_json or cov_json == "{}" or cov_json == '""':
        return {
            "test_feedback": "⚠️ The `coverage-data` artifact was not found or the CI test suite failed. Test-gap analysis could not be performed for this run."
        }
    # 1. Extract the hard data using our custom parser
    coverage_summary = extract_missing_coverage(cov_json)
    #print("Coverage Summary for Test-Gap Agent:\n", coverage_summary, end="\n\n")
    
    # 2. Programmatically compute the exact test gaps
    precise_gaps = identify_uncovered_additions(diff, cov_json)
    #print("Precise Gaps for Test-Gap Agent:\n", precise_gaps, end="\n\n")

    # 3. Build the specific prompt for this PR
    user_message = f"""
    ### 1. Pull Request Code Diff:
    {diff}
    
    ### 2. General Missing Coverage Data (from GitHub Actions):
    {coverage_summary}
    
    ### 3. Programmatically Cross-Referenced Gaps (Exact added lines without tests):
    {precise_gaps}
    """
    
    # 4. Invoke the LLM directly
    response = await llm.ainvoke([
        ("system", TEST_GAP_PROMPT),
        ("user", user_message)
    ])
    
    return {"test_feedback": response.content}