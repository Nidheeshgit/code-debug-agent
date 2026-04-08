# -*- coding: utf-8 -*-
"""
Code Debugging Agent - Full Version (FIXED)
Fixes:
  1. History items are now clickable — clicking reloads the full code into the editor
  2. Quick Example buttons now correctly inject code into the text area on the same render cycle
Supports  : Python, C, C++, Java
Detects   : Syntax + ALL Runtime errors (loop) + Logic (LLM review)
UI        : Streamlit
"""

import os
import ast
import subprocess
import tempfile
import re
import sys
from dotenv import load_dotenv

load_dotenv()

from langchain_groq import ChatGroq
from langchain.memory import ConversationBufferMemory
from langchain_core.messages import HumanMessage, SystemMessage

# ─────────────────────────────────────────────────────────────────────────────
# LANGUAGE DETECTOR
# ─────────────────────────────────────────────────────────────────────────────

def detect_language(src: str) -> str:
    if re.search(r"public\s+class\s+\w+|System\.out\.print|import\s+java\.", src):
        return "java"
    if re.search(r"#include\s*<(stdio|stdlib|string|math|time)\.h>", src):
        return "c"
    if re.search(r"#include\s*<(iostream|vector|string|map|algorithm|bits/stdc\+\+)>|cout|cin|\bstd::", src):
        return "cpp"
    return "python"


# ─────────────────────────────────────────────────────────────────────────────
# PYTHON: COLLECT ALL SYNTAX ERRORS
# ─────────────────────────────────────────────────────────────────────────────

def collect_python_syntax_errors(code: str) -> list:
    errors = []
    working_code = code.splitlines()
    seen_lines = set()
    for _ in range(50):
        src = "\n".join(working_code)
        try:
            ast.parse(src)
            break
        except SyntaxError as e:
            lineno = e.lineno or 1
            if lineno in seen_lines:
                break
            seen_lines.add(lineno)
            errors.append({
                "type":    "SyntaxError",
                "message": e.msg,
                "line":    lineno,
                "offset":  e.offset,
                "text":    (e.text or "").rstrip(),
            })
            if 0 < lineno <= len(working_code):
                working_code[lineno - 1] = "pass  # patched"
    return errors


# ─────────────────────────────────────────────────────────────────────────────
# PYTHON: COLLECT ALL RUNTIME ERRORS
# ─────────────────────────────────────────────────────────────────────────────

def collect_python_runtime_errors(code: str) -> list:
    errors = []
    working_lines = code.splitlines()
    seen_lines = set()

    for _ in range(20):
        src = "\n".join(working_lines)
        with tempfile.TemporaryDirectory() as tmpdir:
            src_path = os.path.join(tmpdir, "code.py")
            with open(src_path, "w", encoding="utf-8") as f:
                f.write(src)
            try:
                result = subprocess.run(
                    [sys.executable, src_path],
                    capture_output=True, text=True, timeout=5,
                    encoding="utf-8", errors="replace",
                )
            except subprocess.TimeoutExpired:
                errors.append({
                    "type": "TimeoutError",
                    "message": "Execution timed out after 5 seconds.",
                    "line": None,
                    "source_line": "",
                })
                break

            if result.returncode == 0:
                break

            stderr = result.stderr.strip()
            lines_out = stderr.splitlines()
            error_type = None
            error_msg  = None
            lineno     = None

            for i, ln in enumerate(lines_out):
                m = re.search(r'File ".*?", line (\d+)', ln)
                if m:
                    lineno = int(m.group(1))
                if i == len(lines_out) - 1 and ":" in ln:
                    parts      = ln.split(":", 1)
                    error_type = parts[0].strip()
                    error_msg  = parts[1].strip() if len(parts) > 1 else ln

            if lineno in seen_lines:
                break
            seen_lines.add(lineno)

            source_line = ""
            if lineno and 0 < lineno <= len(working_lines):
                source_line = working_lines[lineno - 1].strip()
                working_lines[lineno - 1] = "pass  # patched"

            errors.append({
                "type":        error_type or "RuntimeError",
                "message":     error_msg  or stderr,
                "line":        lineno,
                "source_line": source_line,
            })

    return errors


# ─────────────────────────────────────────────────────────────────────────────
# C / C++ COMPILER
# ─────────────────────────────────────────────────────────────────────────────

def compile_c_cpp(code: str, language: str) -> list:
    errors = []
    ext      = ".c" if language == "c" else ".cpp"
    compiler = "gcc" if language == "c" else "g++"
    std_flag = "-std=c11" if language == "c" else "-std=c++17"

    try:
        subprocess.run([compiler, "--version"], capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        return [{"type": "ToolError", "line": None,
                 "message": f"{compiler} not found. Please install it."}]

    with tempfile.TemporaryDirectory() as tmpdir:
        fname    = "code" + ext
        src_path = os.path.join(tmpdir, fname)
        out_path = os.path.join(tmpdir, "code.out")
        with open(src_path, "w", encoding="utf-8") as f:
            f.write(code)

        res = subprocess.run(
            [compiler, src_path, "-o", out_path, std_flag],
            capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace",
        )

        if res.returncode != 0:
            raw = res.stderr.strip().replace(src_path, fname)
            for line in raw.splitlines():
                m = re.match(
                    rf"{re.escape(fname)}:(\d+):\d+:\s+(error|warning|note):\s+(.*)",
                    line
                )
                if m:
                    errors.append({
                        "type":    m.group(2).capitalize(),
                        "line":    int(m.group(1)),
                        "message": m.group(3),
                    })
            if not errors:
                errors.append({"type": "CompilerError", "line": None, "message": raw})

    return errors


# ─────────────────────────────────────────────────────────────────────────────
# JAVA COMPILER
# ─────────────────────────────────────────────────────────────────────────────

def compile_java(code: str) -> list:
    errors = []

    try:
        subprocess.run(["javac", "-version"], capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        return [{"type": "ToolError", "line": None,
                 "message": "javac not found. Please install JDK."}]

    class_match = re.search(r"public\s+class\s+(\w+)", code)
    class_name  = class_match.group(1) if class_match else "Main"

    with tempfile.TemporaryDirectory() as tmpdir:
        src_path = os.path.join(tmpdir, f"{class_name}.java")
        with open(src_path, "w", encoding="utf-8") as f:
            f.write(code)

        res = subprocess.run(
            ["javac", src_path],
            capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace",
        )

        if res.returncode != 0:
            raw = res.stderr.strip().replace(src_path, f"{class_name}.java")
            for line in raw.splitlines():
                m = re.match(
                    rf"{re.escape(class_name)}\.java:(\d+):\s*error:\s*(.*)", line
                )
                if m:
                    errors.append({
                        "type":    "CompilerError",
                        "line":    int(m.group(1)),
                        "message": m.group(2),
                    })
            if not errors:
                errors.append({"type": "CompilerError", "line": None, "message": raw})

    return errors


# ─────────────────────────────────────────────────────────────────────────────
# MAIN DEBUG TOOL
# ─────────────────────────────────────────────────────────────────────────────

def run_debug_tool(code: str) -> dict:
    code     = code.strip()
    language = detect_language(code)
    result   = {
        "language":        language.upper(),
        "syntax_errors":   [],
        "runtime_errors":  [],
        "compiler_errors": [],
        "logic_hints":     [],
        "clean":           False,
    }

    if language == "python":
        syntax_errors = collect_python_syntax_errors(code)
        result["syntax_errors"] = syntax_errors
        if not syntax_errors:
            runtime_errors = collect_python_runtime_errors(code)
            result["runtime_errors"] = runtime_errors
        if not syntax_errors and not result["runtime_errors"]:
            result["clean"] = True

    elif language in ("c", "cpp"):
        compiler_errors = compile_c_cpp(code, language)
        result["compiler_errors"] = compiler_errors
        if not compiler_errors:
            result["clean"] = True

    elif language == "java":
        compiler_errors = compile_java(code)
        result["compiler_errors"] = compiler_errors
        if not compiler_errors:
            result["clean"] = True

    return result


def count_errors(result: dict) -> int:
    return (
        len(result["syntax_errors"])
        + len(result["runtime_errors"])
        + len(result["compiler_errors"])
    )


# ─────────────────────────────────────────────────────────────────────────────
# LLM SETUP
# ─────────────────────────────────────────────────────────────────────────────

def build_llm(api_key: str = "") -> ChatGroq:
    key = api_key.strip() or os.environ.get("GROQ_API_KEY", "").strip()
    if not key:
        raise EnvironmentError("No GROQ_API_KEY found.")
    os.environ["GROQ_API_KEY"] = key
    return ChatGroq(model="llama-3.3-70b-versatile", temperature=0, groq_api_key=key)


# ─────────────────────────────────────────────────────────────────────────────
# LLM: EXPLAIN ALL ERRORS
# ─────────────────────────────────────────────────────────────────────────────

EXPLAIN_PROMPT = """You are a Code Debugging Assistant.
You receive structured error output from a real compiler / AST / runtime checker,
along with the original source code.

Rules:
- Explain EVERY error. Do not skip any.
- Base explanations ONLY on the provided data. No assumptions.
- For each error: say what it means, where it is, and give the exact fix.
- Be concise: 2-3 sentences max per error.
- Always include the corrected line of code in FIX.
- At the end, output the COMPLETE corrected version of the entire original code
  with ALL errors fixed. Do not omit any lines. Use the same language.

Format:

ERROR SUMMARY: <N> error(s) found.

[Error #N - <ErrorType>]
WHAT  : <what the error means>
WHERE : Line <X> [Column <Y> if available]
CAUSE : <root cause in one sentence>
FIX   : <corrected line of code>

OVERALL SUGGESTION:
<one sentence on what to fix first>

CORRECTED CODE:
```
<complete fixed source code here — every line, nothing omitted>
```
"""

def explain_errors(llm: ChatGroq, result: dict, original_code: str) -> str:
    lang     = result["language"]
    sections = [f"Language: {lang}", f"Original Code:\n{original_code}\n"]

    for i, e in enumerate(result["syntax_errors"], 1):
        sections.append(
            f"Syntax Error #{i}:\n"
            f"  Message: {e['message']}\n"
            f"  Line: {e['line']}, Column: {e['offset']}\n"
            f"  Code: {e['text']}"
        )

    for i, e in enumerate(result["runtime_errors"], 1):
        sections.append(
            f"Runtime Error #{i}:\n"
            f"  Type: {e['type']}\n"
            f"  Message: {e['message']}\n"
            f"  Line: {e.get('line', 'N/A')}\n"
            f"  Source Line: {e.get('source_line', '')}"
        )

    for i, e in enumerate(result["compiler_errors"], 1):
        sections.append(
            f"Compiler Error #{i}:\n"
            f"  Type: {e['type']}\n"
            f"  Message: {e['message']}\n"
            f"  Line: {e.get('line', 'N/A')}"
        )

    messages = [
        SystemMessage(content=EXPLAIN_PROMPT),
        HumanMessage(content="\n\n".join(sections) + "\n\nExplain all errors."),
    ]
    return llm.invoke(messages).content.strip()


# ─────────────────────────────────────────────────────────────────────────────
# LLM: LOGIC REVIEW
# ─────────────────────────────────────────────────────────────────────────────

LOGIC_PROMPT = """You are a senior code reviewer.
The code below has NO syntax or runtime errors — it runs without crashing.
Your job is to find LOGIC errors, bad practices, or semantic bugs only.

Rules:
- Only flag real logic problems (wrong algorithm, wrong variable, off-by-one, etc.)
- Do NOT flag style issues or missing optimizations unless they cause wrong output.
- If the code is genuinely correct, say: "No logic errors found." and still output
  the original code under CORRECTED CODE unchanged.
- Give the corrected code snippet for each issue.
- At the end, always output the COMPLETE corrected version of the entire code
  with ALL logic fixes applied.

Format:

LOGIC REVIEW:

[Issue #N]
WHAT  : <what is logically wrong>
WHERE : Line <X>
FIX   : <corrected code snippet>

VERDICT: <one sentence overall>

CORRECTED CODE:
```
<complete fixed source code — every line, nothing omitted>
```
"""

def review_logic(llm: ChatGroq, code: str, language: str) -> str:
    messages = [
        SystemMessage(content=LOGIC_PROMPT),
        HumanMessage(content=f"Language: {language}\n\nCode:\n{code}\n\nReview for logic errors."),
    ]
    return llm.invoke(messages).content.strip()


# ─────────────────────────────────────────────────────────────────────────────
# QUICK EXAMPLE SNIPPETS
# ─────────────────────────────────────────────────────────────────────────────

EXAMPLES = {
    "python": (
        'numbers = [10, 20, 30]\ntotal = 0\nfor i in range(len(numbers)):\n'
        '    total = total + numbers[i]\naverage = total / len(number)\n'
        'print("Avg: " + average)'
    ),
    "java": (
        "public class Main {\n    public static void main(String[] args) {\n"
        "        int x = 10\n        System.out.println(x);\n    }\n}"
    ),
    "c": (
        '#include <stdio.h>\nint main() {\n'
        '    int arr[3] = {1, 2, 3};\n    printf("%d\\n", arr[5]);\n'
        '    return 0\n}'
    ),
    "cpp": (
        '#include <iostream>\nusing namespace std;\n'
        'int main() {\n    int x = 5\n    cout << x << endl;\n    return 0;\n}'
    ),
}


# ─────────────────────────────────────────────────────────────────────────────
# STREAMLIT UI
# ─────────────────────────────────────────────────────────────────────────────

def run_streamlit():
    import streamlit as st

    st.set_page_config(
        page_title="Code Debugging Agent",
        page_icon="🐛",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    # ── CSS ───────────────────────────────────────────────────────────────────
    st.markdown("""
    <style>
    html { font-size: 18px !important; }
    body, .stApp, [class*="css"], .stMarkdown, .stMarkdown p, .stMarkdown li,
    p, li, span, div { font-size: 18px !important; line-height: 1.75 !important; }
    .block-container {
        padding-top: 2rem !important; padding-left: 3rem !important;
        padding-right: 3rem !important; max-width: 100% !important;
    }
    [data-testid="stSidebar"] { min-width: 290px !important; max-width: 290px !important; }
    [data-testid="stSidebar"] * { font-size: 16px !important; line-height: 1.7 !important; }
    [data-testid="stSidebar"] h3 {
        font-size: 18px !important; font-weight: 600 !important;
        margin-top: 1.2rem !important; margin-bottom: 0.4rem !important;
    }
    .stTextArea label { font-size: 19px !important; font-weight: 600 !important; margin-bottom: 10px !important; }
    .stTextArea textarea {
        background-color: #0d1117 !important; color: #e6edf3 !important;
        font-family: 'Courier New', 'Consolas', monospace !important;
        font-size: 17px !important; line-height: 1.85 !important;
        border: 2px solid #30363d !important; border-radius: 10px !important;
        padding: 18px 20px !important; min-height: 460px !important;
    }
    .stTextArea textarea:focus {
        border-color: #58a6ff !important;
        box-shadow: 0 0 0 3px rgba(88,166,255,0.15) !important; outline: none !important;
    }
    h1, .stMarkdown h1 { font-size: 36px !important; font-weight: 700 !important; }
    h2, .stMarkdown h2 { font-size: 28px !important; font-weight: 600 !important; margin-top: 1.5rem !important; }
    h3, .stMarkdown h3 { font-size: 22px !important; font-weight: 600 !important; margin-top: 1.4rem !important; margin-bottom: 0.5rem !important; }
    .stButton > button {
        font-size: 17px !important; font-weight: 500 !important;
        padding: 14px 20px !important; border-radius: 10px !important;
        height: auto !important; width: 100% !important; margin-bottom: 8px !important;
        border: 1.5px solid #30363d !important;
    }
    .stButton > button:hover { border-color: #58a6ff !important; background: rgba(88,166,255,0.08) !important; }
    .stButton > button[kind="primary"] {
        background: linear-gradient(90deg, #e74c3c, #c0392b) !important;
        color: white !important; font-size: 21px !important; font-weight: 700 !important;
        padding: 20px 0 !important; border-radius: 12px !important; border: none !important;
        letter-spacing: 0.03em !important; margin-top: 16px !important;
    }
    .stButton > button[kind="primary"]:hover { background: linear-gradient(90deg, #c0392b, #a93226) !important; }
    .error-box {
        background: #1c1a24; border-left: 7px solid #f85149;
        border-radius: 0 10px 10px 0; padding: 24px 30px; margin: 16px 0;
        font-family: 'Courier New', monospace; font-size: 17px !important; line-height: 2.1 !important;
    }
    .ok-box {
        background: #0d2016; border-left: 7px solid #3fb950;
        border-radius: 0 10px 10px 0; padding: 24px 30px; margin: 16px 0;
        font-size: 17px !important; line-height: 2.1 !important;
    }
    .logic-box {
        background: #1a1a0d; border-left: 7px solid #d29922;
        border-radius: 0 10px 10px 0; padding: 24px 30px; margin: 16px 0;
        font-size: 17px !important; line-height: 2.1 !important;
    }
    .lang-badge {
        display: inline-block; padding: 7px 22px; border-radius: 24px;
        font-size: 19px !important; font-weight: 700; margin-bottom: 14px; letter-spacing: 0.05em;
    }
    .line-count { font-size: 17px !important; color: #8b949e; }
    .stCode, .stCode code, .stCode pre, pre, code { font-size: 16px !important; line-height: 1.85 !important; }
    .stDownloadButton > button { font-size: 16px !important; padding: 12px 22px !important; border-radius: 8px !important; margin-top: 10px !important; }
    .stToggle label, [data-testid="stToggle"] * { font-size: 17px !important; }
    .stAlert, [data-testid="stNotification"] { font-size: 17px !important; padding: 18px 22px !important; }
    .stSpinner > div { font-size: 17px !important; }
    .stCaption { font-size: 15px !important; }
    hr { margin: 2rem 0 !important; border-color: #30363d !important; }

    /* ── History item buttons — distinct styling ── */
    .history-btn > button {
        text-align: left !important;
        font-family: 'Courier New', monospace !important;
        font-size: 14px !important;
        background: #161b22 !important;
        color: #c9d1d9 !important;
        border: 1px solid #30363d !important;
        border-radius: 8px !important;
        padding: 10px 14px !important;
        margin-bottom: 6px !important;
    }
    .history-btn > button:hover {
        border-color: #58a6ff !important;
        background: #1f2937 !important;
        color: #58a6ff !important;
    }
    </style>
    """, unsafe_allow_html=True)

    # ── Header ────────────────────────────────────────────────────────────────
    st.markdown("## 🐛 Code Debugging Agent")
    st.markdown("Supports **Python · C · C++ · Java** — detects Syntax, Runtime, Compiler & Logic errors")
    st.divider()

    # ── Session state init ────────────────────────────────────────────────────
    if "history" not in st.session_state:
        st.session_state.history = []          # list of dicts with full code
    # "main_editor" IS the text_area's key — writing to it directly updates the widget
    if "main_editor" not in st.session_state:
        st.session_state["main_editor"] = ""

    # ── Sidebar ───────────────────────────────────────────────────────────────
    with st.sidebar:
        st.markdown("### ⚙️ Configuration")
        api_key_input = os.environ.get("GROQ_API_KEY", "").strip()
        if api_key_input:
            st.success("✅ API key loaded from environment")
        else:
            st.error("❌ GROQ_API_KEY not found. Add it to your .env file.")

        st.markdown("---")
        st.markdown("### 🔍 Detection Modes")
        do_logic = st.toggle("Logic Error Review (LLM)", value=True)

        st.markdown("---")
        st.markdown("### 📋 History")
        st.caption("Click any session to reload its code into the editor.")

        if st.session_state.history:
            # Show most-recent first (up to 10)
            for idx, h in enumerate(reversed(st.session_state.history[-10:])):
                real_idx = len(st.session_state.history) - idx
                label    = f"#{real_idx} {h['lang']} — {h['errors']} error(s)\n{h['code'][:40]}…"
                # Use a unique key per entry
                btn_key = f"hist_{real_idx}_{idx}"
                st.markdown('<div class="history-btn">', unsafe_allow_html=True)
                if st.button(label, key=btn_key):
                    # Write directly to the widget's own key — this is what Streamlit actually reads
                    st.session_state["main_editor"] = h["full_code"]
                    st.rerun()
                st.markdown('</div>', unsafe_allow_html=True)
        else:
            st.caption("No sessions yet.")

        st.markdown("---")
        st.markdown("### 👨‍💻 Developed By")
        st.markdown("""
        <div style="
            background: linear-gradient(135deg, #1e1e2e, #2a2a3e);
            border: 1px solid #30363d; border-radius: 12px; padding: 16px 20px; margin-top: 6px;
        ">
            <div style="display:flex; align-items:center; margin-bottom: 12px;">
                <span style="background:#3776ab;color:white;border-radius:50%;width:40px;height:40px;
                    display:inline-flex;align-items:center;justify-content:center;
                    font-weight:bold;font-size:16px;margin-right:14px;flex-shrink:0;">N</span>
                <span style="color:#e6edf3;font-size:17px;font-weight:500;">C.Nidheesh Reddy-(3665)</span>
            </div>
            <div style="display:flex; align-items:center;">
                <span style="background:#d29922;color:white;border-radius:50%;width:40px;height:40px;
                    display:inline-flex;align-items:center;justify-content:center;
                    font-weight:bold;font-size:16px;margin-right:14px;flex-shrink:0;">H</span>
                <span style="color:#e6edf3;font-size:17px;font-weight:500;">PM Ashrith Ram-(5618)</span>
            </div>
        </div>
        """, unsafe_allow_html=True)

        st.markdown("---")
        st.markdown("### 🛠 Requirements")
        st.code("pip install streamlit langchain\n    langchain-groq python-dotenv", language="bash")

    # ── Memory ────────────────────────────────────────────────────────────────
    if "memory" not in st.session_state:
        st.session_state.memory = ConversationBufferMemory(
            memory_key="chat_history", return_messages=True
        )

    # ── Code Input + Quick Examples ───────────────────────────────────────────
    col1, col2 = st.columns([4, 1], gap="large")

    with col2:
        st.markdown("#### Quick Examples")
        # ── FIX 2: buttons set editor_code directly; st.rerun() forces re-render
        # so the text_area picks up the new value immediately.
        if st.button("🐍 Python Bug", use_container_width=True):
            st.session_state["main_editor"] = EXAMPLES["python"]
            st.rerun()
        if st.button("☕ Java Bug", use_container_width=True):
            st.session_state["main_editor"] = EXAMPLES["java"]
            st.rerun()
        if st.button("⚙️ C Bug", use_container_width=True):
            st.session_state["main_editor"] = EXAMPLES["c"]
            st.rerun()
        if st.button("🔧 C++ Bug", use_container_width=True):
            st.session_state["main_editor"] = EXAMPLES["cpp"]
            st.rerun()

    with col1:
        # NO value= here — Streamlit owns this widget via key="main_editor"
        # Writing to st.session_state["main_editor"] before rerun() is what updates it
        code_input = st.text_area(
            "📝 Paste your code here",
            height=460,
            placeholder="# Paste Python, C, C++, or Java code here...\n# You can paste multiple lines with multiple bugs.",
            key="main_editor",
        )

    analyze_btn = st.button("🔍 Analyze Code", type="primary", use_container_width=True)

    # ── Analysis ──────────────────────────────────────────────────────────────
    if analyze_btn:
        if not code_input.strip():
            st.warning("⚠️ Please paste some code first.")
            st.stop()

        if not api_key_input:
            st.error("❌ GROQ_API_KEY not found. Please add it to your .env file and restart the app.")
            st.stop()

        try:
            llm = build_llm(api_key_input)
        except Exception as e:
            st.error(f"LLM Error: {e}")
            st.stop()

        lang       = detect_language(code_input)
        line_count = len(code_input.splitlines())

        badge_colors = {
            "python": "#3776ab", "java": "#f89820",
            "c": "#555555",      "cpp": "#00599c",
        }
        color = badge_colors.get(lang, "#555")
        st.markdown(
            f'<span class="lang-badge" style="background:{color};color:white;">'
            f'{lang.upper()}</span>'
            f'&nbsp;&nbsp;<span class="line-count">{line_count} lines of code</span>',
            unsafe_allow_html=True,
        )

        with st.spinner("⏳ Running compiler / AST / runtime analysis..."):
            result = run_debug_tool(code_input)

        total = count_errors(result)

        # ── Error Report ──────────────────────────────────────────────────────
        st.markdown("### 📋 Error Report")

        if result["clean"] or total == 0:
            st.markdown(
                '<div class="ok-box">✅ &nbsp;<b>No syntax or runtime errors detected.</b></div>',
                unsafe_allow_html=True,
            )
        else:
            st.markdown(f"**{total} error(s) found:**")

            for i, e in enumerate(result["syntax_errors"], 1):
                st.markdown(
                    f'<div class="error-box">'
                    f'<b>🔴 Syntax Error #{i}</b><br><br>'
                    f'<b>Message:</b> &nbsp;{e["message"]}<br>'
                    f'<b>Line:</b> &nbsp;{e["line"]} &nbsp;&nbsp; <b>Column:</b> &nbsp;{e["offset"]}<br>'
                    f'<b>Code:</b> &nbsp;<code style="font-size:16px">{e["text"]}</code>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

            for i, e in enumerate(result["runtime_errors"], 1):
                src = e.get("source_line", "")
                st.markdown(
                    f'<div class="error-box">'
                    f'<b>🟠 Runtime Error #{i} — {e["type"]}</b><br><br>'
                    f'<b>Message:</b> &nbsp;{e["message"]}<br>'
                    f'<b>Line:</b> &nbsp;{e.get("line", "N/A")}'
                    + (f'<br><b>Code:</b> &nbsp;<code style="font-size:16px">{src}</code>' if src else "")
                    + f'</div>',
                    unsafe_allow_html=True,
                )

            for i, e in enumerate(result["compiler_errors"], 1):
                st.markdown(
                    f'<div class="error-box">'
                    f'<b>🔴 Compiler Error #{i} — {e["type"]}</b><br><br>'
                    f'<b>Message:</b> &nbsp;{e["message"]}<br>'
                    f'<b>Line:</b> &nbsp;{e.get("line", "N/A")}'
                    f'</div>',
                    unsafe_allow_html=True,
                )

        # ── LLM Explanation ───────────────────────────────────────────────────
        if total > 0:
            st.markdown("### 💡 LLM Explanation")
            with st.spinner(f"🤖 Explaining {total} error(s) with AI..."):
                try:
                    explanation = explain_errors(llm, result, code_input)
                    if "CORRECTED CODE:" in explanation:
                        parts       = explanation.split("CORRECTED CODE:", 1)
                        explain_txt = parts[0].strip()
                        fixed_block = parts[1].strip().strip("`").strip()
                        fixed_lines = fixed_block.splitlines()
                        if fixed_lines and re.match(r"^[a-zA-Z+]+$", fixed_lines[0].strip()):
                            fixed_block = "\n".join(fixed_lines[1:])
                    else:
                        explain_txt = explanation
                        fixed_block = None

                    st.markdown(
                        f'<div class="error-box">{explain_txt.replace(chr(10), "<br>")}</div>',
                        unsafe_allow_html=True,
                    )

                    if fixed_block:
                        st.markdown("### ✅ Corrected Code")
                        st.code(fixed_block, language=lang if lang != "cpp" else "cpp")
                        st.download_button(
                            label="⬇️ Download Fixed Code",
                            data=fixed_block,
                            file_name=f"fixed.{'java' if lang=='java' else 'c' if lang=='c' else 'cpp' if lang=='cpp' else 'py'}",
                            mime="text/plain",
                        )
                except Exception as e:
                    st.error(f"LLM error: {e}")

        # ── Logic Review ──────────────────────────────────────────────────────
        if do_logic:
            st.markdown("### 🧠 Logic Error Review")
            with st.spinner("🔬 Reviewing code logic..."):
                try:
                    logic_review = review_logic(llm, code_input, lang.upper())
                    if "CORRECTED CODE:" in logic_review:
                        parts       = logic_review.split("CORRECTED CODE:", 1)
                        logic_txt   = parts[0].strip()
                        logic_fixed = parts[1].strip().strip("`").strip()
                        logic_lines = logic_fixed.splitlines()
                        if logic_lines and re.match(r"^[a-zA-Z+]+$", logic_lines[0].strip()):
                            logic_fixed = "\n".join(logic_lines[1:])
                    else:
                        logic_txt   = logic_review
                        logic_fixed = None

                    box_class = "ok-box" if "No logic errors found" in logic_txt else "logic-box"
                    st.markdown(
                        f'<div class="{box_class}">{logic_txt.replace(chr(10), "<br>")}</div>',
                        unsafe_allow_html=True,
                    )

                    if logic_fixed and "No logic errors found" not in logic_txt:
                        st.markdown("### ✅ Logic-Fixed Code")
                        st.code(logic_fixed, language=lang if lang != "cpp" else "cpp")
                        st.download_button(
                            label="⬇️ Download Logic-Fixed Code",
                            data=logic_fixed,
                            file_name=f"logic_fixed.{'java' if lang=='java' else 'c' if lang=='c' else 'cpp' if lang=='cpp' else 'py'}",
                            mime="text/plain",
                            key="logic_dl",
                        )
                except Exception as e:
                    st.error(f"Logic review error: {e}")

        # ── Original Code ─────────────────────────────────────────────────────
        st.markdown("### 📄 Your Original Code")
        st.code(code_input, language=lang if lang != "cpp" else "cpp")

        # ── FIX 1 (cont): save FULL code into history so it can be reloaded ──
        st.session_state.history.append({
            "lang":      lang.upper(),
            "errors":    total,
            "code":      code_input[:80],   # preview snippet
            "full_code": code_input,        # ← full code stored here
        })
        st.session_state.memory.chat_memory.add_user_message(
            f"{lang.upper()} code, {line_count} lines, {total} error(s)"
        )

        st.divider()
        st.caption(f"✅ Session #{len(st.session_state.history)} complete.")


# ─────────────────────────────────────────────────────────────────────────────
# CLI FALLBACK
# ─────────────────────────────────────────────────────────────────────────────

def run_cli():
    if sys.platform == "win32":
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

    print("+----------------------------------------------------------+")
    print("|      Code Debugging Agent  (Python / C / C++ / Java)    |")
    print("+----------------------------------------------------------+")

    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    if not api_key:
        api_key = input("Enter Groq API Key: ").strip()
    try:
        llm = build_llm(api_key)
    except Exception as e:
        print(f"[ERROR] {e}")
        return

    memory  = ConversationBufferMemory(memory_key="chat_history", return_messages=True)
    session = 0

    while True:
        print("\n" + "=" * 60)
        print("Paste code (blank line x2 to submit, 'quit' to exit):")
        lines, blank = [], 0
        while True:
            try:
                line = input()
            except EOFError:
                break
            if line == "":
                blank += 1
                if blank >= 2:
                    break
                lines.append(line)
            else:
                blank = 0
                lines.append(line)

        code = "\n".join(lines).strip()
        if not code or code.lower() == "quit":
            print("Goodbye!")
            break

        session += 1
        lang       = detect_language(code)
        line_count = len(code.splitlines())

        print(f"\n[Session #{session}] Language: {lang.upper()} | Lines: {line_count}")
        print("Analyzing...", end="", flush=True)

        result = run_debug_tool(code)
        total  = count_errors(result)
        print(f" done. -> {total} error(s) found.")

        print("\n" + "-" * 60)
        print("  FULL ERROR REPORT")
        print("-" * 60)

        if result["clean"] or total == 0:
            print("[OK] No errors detected.")
        else:
            for i, e in enumerate(result["syntax_errors"], 1):
                print(f"\n  +--[ SYNTAX ERROR #{i} ]")
                print(f"  |  Message : {e['message']}")
                print(f"  |  Line    : {e['line']}  Column: {e['offset']}")
                print(f"  |  Code    : {e['text']}")

            for i, e in enumerate(result["runtime_errors"], 1):
                print(f"\n  +--[ RUNTIME ERROR #{i} - {e['type']} ]")
                print(f"  |  Message : {e['message']}")
                print(f"  |  Line    : {e.get('line', 'N/A')}")
                if e.get("source_line"):
                    print(f"  |  Code    : {e['source_line']}")

            for i, e in enumerate(result["compiler_errors"], 1):
                print(f"\n  +--[ COMPILER ERROR #{i} - {e['type']} ]")
                print(f"  |  Message : {e['message']}")
                print(f"  |  Line    : {e.get('line', 'N/A')}")

        if total > 0:
            print("\n" + "-" * 60)
            print("  LLM EXPLANATION")
            print("-" * 60)
            try:
                explanation = explain_errors(llm, result, code)
                if "CORRECTED CODE:" in explanation:
                    parts       = explanation.split("CORRECTED CODE:", 1)
                    explain_txt = parts[0].strip()
                    fixed_block = parts[1].strip().strip("`").strip()
                    fixed_lines = fixed_block.splitlines()
                    if fixed_lines and re.match(r"^[a-zA-Z+]+$", fixed_lines[0].strip()):
                        fixed_block = "\n".join(fixed_lines[1:])
                    print(explain_txt)
                    print("\n" + "-" * 60)
                    print("  CORRECTED CODE")
                    print("-" * 60)
                    print(fixed_block)
                else:
                    print(explanation)
            except Exception as e:
                print(f"[LLM ERROR] {e}")

        print("\n" + "-" * 60)
        print("  LOGIC REVIEW")
        print("-" * 60)
        try:
            logic_review = review_logic(llm, code, lang.upper())
            if "CORRECTED CODE:" in logic_review:
                parts       = logic_review.split("CORRECTED CODE:", 1)
                logic_txt   = parts[0].strip()
                logic_fixed = parts[1].strip().strip("`").strip()
                logic_lines = logic_fixed.splitlines()
                if logic_lines and re.match(r"^[a-zA-Z+]+$", logic_lines[0].strip()):
                    logic_fixed = "\n".join(logic_lines[1:])
                print(logic_txt)
                if "No logic errors found" not in logic_txt:
                    print("\n" + "-" * 60)
                    print("  LOGIC-FIXED CODE")
                    print("-" * 60)
                    print(logic_fixed)
            else:
                print(logic_review)
        except Exception as e:
            print(f"[LLM ERROR] {e}")

        memory.chat_memory.add_user_message(f"Session #{session}: {lang.upper()}, {total} error(s)")


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    try:
        import streamlit
        run_streamlit()
    except ImportError:
        run_cli()