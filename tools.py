"""
tools.py
A collection of utility tools that the AI or application can leverage locally.
"""
import os

def calculator(expression: str) -> str:
    """Evaluates a basic mathematical expression safely."""
    try:
        # Whitelist characters for basic safe evaluation
        allowed_chars = set("0123456789+-*/(). ")
        if not all(c in allowed_chars for c in expression):
            return "Error: Invalid characters in expression."
        return str(eval(expression))
    except Exception as e:
        return f"Error computing expression: {e}"

def file_reader(filepath: str) -> str:
    """Reads the contents of a local text file."""
    if not os.path.exists(filepath):
        return f"Error: File '{filepath}' not found."
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        return f"Error reading file: {e}"

def web_search(query: str) -> str:
    """Stub for a web search tool."""
    return f"[Web Search Stub] Local index search required for: '{query}'."