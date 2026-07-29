import os
import asyncio
import shutil
import tempfile
from dotenv import load_dotenv
from langchain_groq import ChatGroq
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain.agents import create_agent
from unidiff import PatchSet
from langchain_cerebras import ChatCerebras

load_dotenv()

# ==========================================
# 1. DIFF CLEANING
# ==========================================
def clean_diff_to_files(diff: str) -> list[dict]:
    """
    Parses a unified git diff into a list of {filename, content} dicts,
    one per modified file. Skips binary files and pure deletions.
    """
    patch = PatchSet(diff)
    files = []

    for patched_file in patch:
        if patched_file.is_binary_file or patched_file.is_removed_file:
            continue

        filename = patched_file.path
        lines = []
        for hunk in patched_file:
            for line in hunk:
                if line.is_added or line.is_context:
                    lines.append(line.value.rstrip('\n'))

        if lines:
            files.append({
                "filename": filename,
                "content": "\n".join(lines)
            })

    return files

def write_files_to_temp(files: list[dict]) -> tuple[str, list[str]]:
    """
    Writes cleaned diff files to a temp directory, preserving relative
    paths/filenames so Semgrep can properly detect language + structure.
    Returns (temp_dir_path, list_of_absolute_file_paths).
    """
    temp_dir = tempfile.mkdtemp(prefix="security_agent_scan_")
    abs_paths = []

    for f in files:
        # preserve subdirectories if the diff has them (e.g. app/routes/auth.py)
        dest_path = os.path.join(temp_dir, f["filename"])
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        with open(dest_path, "w") as out:
            out.write(f["content"])
        abs_paths.append(dest_path)

    return temp_dir, abs_paths

# ==========================================
# 2. MCP CLIENT SETUP
# ==========================================
semgrep_path = shutil.which("semgrep")
if not semgrep_path:
    raise RuntimeError("Semgrep not found on PATH. Install with: brew install semgrep")

mcp_client = MultiServerMCPClient({
    "semgrep": {
        "command": semgrep_path,
        "args": ["mcp"],
        "transport": "stdio",
    }
})

# ==========================================
# 3. AGENT CONFIG
# ==========================================
llm = ChatCerebras(
    temperature=0.1,
    model=os.getenv("SECURITY_MODEL"),  # bare ID, no "openai/" prefix — matches what your /v1/models call confirmed
    api_key=os.getenv("CEREBRAS_API_KEY"),
)
"""
    llm = ChatGroq(
    temperature=0.1,
    model_name="openai/gpt-oss-20b",
    groq_api_key=os.getenv("GROQ_API_KEY")
)
"""


SECURITY_PROMPT = """You are a Senior Application Security Engineer conducting a two-phase code review.

## PHASE 1: AUTOMATED SCAN
Always call semgrep_scan first, using config='p/security-audit', on the provided
absolute file paths. Record its findings exactly as returned — do not paraphrase
severity or omit any result.

## PHASE 2: MANUAL EXPERT REVIEW (MANDATORY — DO NOT SKIP)
Semgrep uses taint tracking: it can only flag a vulnerability if it can trace data
from a known "source" (user input) to a "sink" (a dangerous operation). When
reviewing an isolated diff or file, Semgrep often CANNOT see the caller, so it
stays silent even when a real vulnerability exists. An empty Semgrep result means
"no pattern matched with visible taint," NOT "this code is safe."

You must independently review the code against this checklist, treating every
function parameter, external input, or user-controllable value as if it COULD
originate from an untrusted source (HTTP request, file upload, CLI arg, API
payload) — even if the caller isn't visible in this diff. Silently check each
category below against the code; only surface it in your output if you find an
actual issue.

1. **Path Traversal (CWE-22) — Is any file path built via string concatenation or formatting instead of validated/normalized joins? Was a prior sanitization step (e.g. os.path.basename, os.path.normpath, allowlisting) removed or weakened in this diff?
2a. **SQL/Query Injection — String-built SQL queries, LDAP injection, NoSQL query injection.
2b. **XXE / XML Injection — XML parsers with external entity resolution enabled, DTD processing not disabled, user-controlled XML fed to a parser without hardening.
2c. **Command Injection (CWE-78) — os.system, subprocess with shell=True, exec/eval on external data, backtick/shell execution in other languages.
2d. **Template Injection — Jinja2 render_template_string, server-side template injection with unsanitized input.
3a. **Broken Authentication — Missing auth checks, weak session handling, hardcoded credentials, privilege checks removed or weakened in this diff.
3b. **Broken Access Control (IDOR / Mass Assignment) — Object references not scoped to the requesting user; ORM/model binding directly from request body (e.g. User(**request.json)) allowing clients to set fields like is_admin they shouldn't control.
4. **Insecure Deserialization — pickle.loads, yaml.load (without SafeLoader), eval/exec on external data, marshal.
5. **Cryptographic Failures — Weak/broken algorithms (MD5/SHA1 for passwords), hardcoded secrets/keys, insecure random (random instead of secrets module) for security-sensitive values, missing TLS verification.
6. **SSRF — Outbound requests (requests.get, urllib) built from user-controllable URLs/hosts without allowlisting.
7. **CSRF — State-changing endpoints (POST/PUT/DELETE) without CSRF token validation, or a diff that weakens cookie SameSite/Secure settings.
8. **Input Validation Gaps — Missing length/type/format checks on data that flows into sensitive operations, especially where this diff REMOVES an existing check.
9. **Race Conditions / TOCTOU — Check-then-use patterns on files or shared state without atomic operations or locks.
10. **Information Disclosure — Verbose error messages/stack traces exposed to users, sensitive data in logs.
11. **Vulnerable/Malicious Dependencies — New or bumped packages introduced without version pinning, known-CVE versions, or lockfile diffs (package-lock.json, requirements.txt, go.sum) that don't match the stated change.
12. **Regression Risk — For any diff (not just new code): did this change REMOVE, WEAKEN, or BYPASS an existing safeguard that was present in the old version? This is the single highest-value check — diffs that strip out sanitization are a common real-world vulnerability injection pattern.

## OUTPUT FORMAT
Output a single combined, flat list of every vulnerability found — whether from
Semgrep (Phase 1) or your own reasoning (Phase 2). Do not separate them into
different sections, and do not mention or justify categories where nothing was
found. Only list actual findings.

For each finding, report exactly these fields, nothing else:
- **Vulnerability**: short name + CWE reference if applicable
- **Source**: Semgrep or Manual Review
- **Location**: file, line/function
- **Severity**: Critical / High / Medium / Low
- **Reasoning**: the data flow or pattern that makes this risky (1-2 sentences;
  for Manual Review findings, state what you're assuming about the untrusted
  source, e.g. "assuming `filename` is attacker-controlled via an API endpoint")
- **Fix**: one-line concrete fix suggestion

If zero vulnerabilities are found across both phases, output exactly:
"No security vulnerabilities identified."

Do not add headers, phase labels, summaries, verdicts, or any text outside the
finding list."""

# Cached agent instance + lock so graph.py doesn't rebuild the MCP tool
# connection on every call (tool loading is an async network/subprocess
# round-trip to the Semgrep MCP server).
_agent = None
_agent_lock = asyncio.Lock()

async def build_security_agent():
    """
    Loads Semgrep MCP tools and constructs the security review agent.
    Safe to call multiple times; use get_security_agent() for a cached
    singleton in production call paths.
    """
    tools = await mcp_client.get_tools()
    return create_agent(llm, tools=tools, system_prompt=SECURITY_PROMPT)

async def get_security_agent():
    """
    Returns a cached agent instance, building it on first use.
    """
    global _agent
    if _agent is None:
        async with _agent_lock:
            if _agent is None:
                _agent = await build_security_agent()
    return _agent

# ==========================================
# 4. PUBLIC ENTRYPOINT
# ==========================================
async def run_security_agent(diff: str) -> str:
    files = clean_diff_to_files(diff)
    if not files:
        return "No security vulnerabilities identified."

    temp_dir, abs_paths = write_files_to_temp(files)
    file_contents = "\n\n".join(
        f"File: {f['filename']}\n```\n{f['content']}\n```" for f in files
    )

    try:
        agent = await get_security_agent()
        response = await agent.ainvoke({
            "messages": [(
                "user",
                f"Audit these changed files for security vulnerabilities.\n\n"
                f"Absolute file paths to scan with semgrep_scan: {abs_paths}\n\n"
                f"Full content of each changed file, for your manual review "
                f"(Phase 2):\n\n{file_contents}"
            )]
        })
        final_message = response["messages"][-1]
        return final_message.content
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)