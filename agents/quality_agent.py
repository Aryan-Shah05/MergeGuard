import os
import json
import asyncio
from dotenv import load_dotenv
from langchain_groq import ChatGroq
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain.agents import create_agent

load_dotenv()

# ==========================================
# 1. MCP CLIENT SETUP
# ==========================================
mcp_client = MultiServerMCPClient({
    "sonarqube": {
        "transport": "streamable_http",
        "url": "https://api.sonarcloud.io/mcp",
        "headers": {
            "Authorization": f"Bearer {os.getenv('SONARQUBE_TOKEN')}",
            "SONARQUBE_ORG": os.getenv("SONARQUBE_ORG"),
        },
    }
})

# ==========================================
# 2. AGENTS CONFIG
# ==========================================
# Enforces the 120b model name to match your working agents
llm = ChatGroq(
    temperature=0.1,
    model_name=os.getenv("QUALITY_MODEL"),
    groq_api_key=os.getenv("GROQ_API_KEY")
)

# 2.1. Phase 1 Prompt (SonarQube Data Formatting)
SONARQUBE_INTERPRET_PROMPT = """You are a Senior Code Quality Engineer.
Your task is to interpret the provided SonarQube analysis JSON data (issues list) for a pull request.
List every issue SonarQube found, exactly as given — do not omit any issue present in the raw data. 

For each issue, format your output strictly as bullet points in this structure (do not use code blocks or tables):
- **Issue**: [State the exact rule name or message retrieved from the SonarQube JSON data]
- **Source**: SonarQube
- **Location**: [File Path], Line [Line Number]
- **Severity**: [SonarQube Severity (Critical / High / Medium / Low)]
- **Reasoning**: [1-2 sentences explaining why this matters for maintainability/quality]
- **Fix**: [One-line concrete suggestion to fix the issue]

If no issues are found, strictly output: "No issues identified by SonarQube."
Do not add any introduction, headers, or closing comments."""

# 2.2. Phase 2 Prompt (Manual Review)
MANUAL_REVIEW_PROMPT = """You are a Senior Code Quality Engineer conducting a manual review of code changes.
Review the actual code changes (PR diff) for clean code violations:
- Naming clarity — variable/function names that don't describe their purpose
- Magic numbers — hardcoded numeric literals with no named constant or comment
  explaining what they represent
- Low cohesion — a function doing several unrelated things (e.g. validation +
  I/O + formatting + logging all in one function) even if it isn't flagged as
  "complex" by a metric
- Overly broad exception handling (e.g. bare `except Exception: pass`) that
  SonarQube may not have flagged
- Whether this diff REMOVES or WEAKENS an existing quality safeguard compared
  to the prior version

For each issue found, format your output strictly as bullet points in this structure (do not use code blocks or tables):
- **Issue**: [State the type of clean-code issue identified]
- **Source**: Manual Review
- **Location**: [File Path], Line [Line Number]/[Function Name]
- **Severity**: [Severity (Critical / High / Medium / Low)]
- **Reasoning**: [1-2 sentences explaining why this matters for quality]
- **Fix**: [One-line concrete suggestion to fix the issue]

If no issues are found, output exactly: "No manual review issues identified."
Do not add any introduction, headers, or closing comments."""

# ==========================================
# 3. DETERMINISTIC SONAR DATA FETCH
# ==========================================
async def fetch_with_retry(tools_by_name, tool_name, params, max_retries, retry_delay_seconds):
    for attempt in range(1, max_retries + 1):
        try:
            return await tools_by_name[tool_name].ainvoke(params)
        except Exception as e:
            print(f"  -> {tool_name} attempt {attempt} failed: {e}")
            await asyncio.sleep(retry_delay_seconds)
    return f"Failed to retrieve {tool_name} after {max_retries} attempts."

# ==========================================
# 4. PUBLIC ENTRYPOINT
# ==========================================
async def run_quality_agent(
    sonar_project_key: str,
    pr_number: int,
    diff_text: str,
    max_retries: int = 6,
    retry_delay_seconds: int = 3
) -> str:
    """
    End-to-end pipeline:
    1. Deterministically fetches SonarQube analysis data for the given PR (Phase 1).
    2. Runs two parallel LLM calls:
       - Phase 1: Summarizes SonarQube issues.
       - Phase 2: Performs manual code diff review.
    3. Combines findings and outputs the final report with deterministic metrics.
    """
    tools = await mcp_client.get_tools()
    tools_by_name = {t.name: t for t in tools}

    # Step 1: Discover real SonarQube PR key
    pr_list = await fetch_with_retry(
        tools_by_name, "list_pull_requests",
        {"projectKey": sonar_project_key},
        max_retries, retry_delay_seconds
    )

    sonar_pr_key = str(pr_number)

    # Step 2: Fetch SonarQube data
    quality_gate_resp = await fetch_with_retry(
        tools_by_name, "get_project_quality_gate_status",
        {"projectKey": sonar_project_key, "pullRequest": sonar_pr_key},
        max_retries, retry_delay_seconds
    )

    issues_resp = await fetch_with_retry(
        tools_by_name, "search_sonar_issues_in_projects",
        {"projects": [sonar_project_key], "pullRequest": sonar_pr_key},
        max_retries, retry_delay_seconds
    )

    measures_resp = await fetch_with_retry(
        tools_by_name, "get_component_measures",
        {
            "projectKey": sonar_project_key,
            "pullRequest": sonar_pr_key,
            "metricKeys": ["ncloc", "complexity", "violations", "coverage", "duplicated_lines_density"],
        },
        max_retries, retry_delay_seconds
    )

    sonar_fetch_failed = any(
        isinstance(x, str) and x.startswith("Failed to retrieve")
        for x in (quality_gate_resp, issues_resp, measures_resp)
    )
    if sonar_fetch_failed:
        return (
            f"⚠️ SonarQube data could not be retrieved for PR #{pr_number} after "
            f"{max_retries} attempts per call. Quality Gate: PENDING (unverifiable). "
            f"Manual review was not performed since Phase 1 data was unavailable."
        )

    # 🔍 Diagnostic Logs
    #print("\n" + "=" * 80)
    #print("pr_no:", pr_number)
    #print("🔎 DIAGNOSTIC LOGS: QUALITY AGENT DATA EXTRACTION")
    #print("=" * 80)

    #print(f"\n--- [1] CODE DIFF ---\n{diff_text if diff_text else 'No diff.'}\n---------------------\n")
    #print(f"\n--- [2] SONARQUBE QUALITY GATE ---\n{quality_gate_resp}\n----------------------------------\n")
    #print(f"\n--- [3] SONARQUBE ISSUES ---\n{issues_resp}\n----------------------------\n")
    #print(f"\n--- [4] SONARQUBE MEASURES ---\n{measures_resp}\n------------------------------\n")
    #print("=" * 80 + "\n")

    # Step 3: Run Phase 1 & Phase 2 in parallel
    # We call the model directly for each phase task to keep it fast and separate
    async def run_phase1():
        user_message = f"Please interpret these SonarQube issues:\n\n{issues_resp}"
        response = await llm.ainvoke([
            ("system", SONARQUBE_INTERPRET_PROMPT),
            ("user", user_message)
        ])
        return response.content.strip()

    async def run_phase2():
        user_message = f"Please manually review this PR diff for clean code issues:\n\n{diff_text}"
        response = await llm.ainvoke([
            ("system", MANUAL_REVIEW_PROMPT),
            ("user", user_message)
        ])
        return response.content.strip()

    phase1_report, phase2_report = await asyncio.gather(run_phase1(), run_phase2())

    # Step 4: Extract Quality Gate Status and Metrics deterministically in Python
    gate_status = "PENDING"
    try:
        gate_json_str = quality_gate_resp[0]['text'] if isinstance(quality_gate_resp, list) else str(quality_gate_resp)
        gate_data = json.loads(gate_json_str)
        status_raw = gate_data.get("status", "PENDING")
        gate_status = "FAIL" if status_raw == "ERROR" else ("PASS" if status_raw == "OK" else "PENDING")
    except Exception:
        pass

    complexity, duplication, ncloc, violations, coverage = "0", "0.0", "0", "0", "0.0"
    try:
        measures_json_str = measures_resp[0]['text'] if isinstance(measures_resp, list) else str(measures_resp)
        measures_data = json.loads(measures_json_str)
        metrics_list = measures_data.get("measures", [])
        metrics_dict = {m.get("metric"): m.get("value") for m in metrics_list}
        
        complexity = metrics_dict.get("complexity", "0")
        duplication = metrics_dict.get("duplicated_lines_density", "0.0")
        ncloc = metrics_dict.get("ncloc", "0")
        violations = metrics_dict.get("violations", "0")
        coverage = metrics_dict.get("coverage", "0.0")
    except Exception:
        pass

    # Step 5: Format and Combine the outputs
    combined_findings = []
    
    if phase1_report and "No issues identified" not in phase1_report:
        combined_findings.append(phase1_report)
        
    if phase2_report and "No manual review issues" not in phase2_report:
        combined_findings.append(phase2_report)

    # If both are clean, output a clean message
    if not combined_findings:
        findings_section = "No quality issues identified."
    else:
        findings_section = "\n\n".join(combined_findings)

    # Compile the final report matching your strict format requirements
    final_report = f"""{findings_section}

- **Quality Gate**: {gate_status}
- **Key Metrics**: complexity={complexity}, duplication={duplication}%, lines of code={ncloc}, violations={violations}, coverage={coverage}%"""

    return final_report.strip()