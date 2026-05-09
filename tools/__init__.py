"""Tool definitions for CLAWCLI — Ollama function-calling schema."""

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the local filesystem. Returns file contents with line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Absolute or relative path to the file"},
                    "offset": {"type": "integer", "description": "Line number to start reading from (1-indexed)"},
                    "limit": {"type": "integer", "description": "Maximum number of lines to read"}
                },
                "required": ["file_path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file, creating it if it doesn't exist or overwriting it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Absolute or relative path to the file"},
                    "content": {"type": "string", "description": "The full content to write to the file"}
                },
                "required": ["file_path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace an exact string in a file with new content. The old_string must match character-for-character including indentation. Prefer replace_lines when you have line numbers from a recent read_file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Path to the file to edit"},
                    "old_string": {"type": "string", "description": "The exact text to replace (must be unique in file, whitespace included)"},
                    "new_string": {"type": "string", "description": "The replacement text"},
                    "replace_all": {"type": "boolean", "description": "Replace all occurrences instead of requiring uniqueness"}
                },
                "required": ["file_path", "old_string", "new_string"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "replace_lines",
            "description": "Replace a range of lines in a file by line number. More reliable than edit_file because it requires no string matching — use this whenever you have line numbers from a recent read_file call.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Path to the file to edit"},
                    "start_line": {"type": "integer", "description": "First line to replace (1-indexed, inclusive)"},
                    "end_line": {"type": "integer", "description": "Last line to replace (1-indexed, inclusive)"},
                    "new_content": {"type": "string", "description": "Replacement text. May span multiple lines. Include correct indentation. Use empty string to delete the lines."}
                },
                "required": ["file_path", "start_line", "end_line", "new_content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "glob_files",
            "description": "Find files matching a glob pattern. Returns matching file paths sorted by modification time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern (e.g. '**/*.py', 'src/**/*.ts')"},
                    "directory": {"type": "string", "description": "Base directory to search in (defaults to cwd)"}
                },
                "required": ["pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "grep_files",
            "description": "Search file contents using regex. Returns matching lines or file paths.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern to search for"},
                    "path": {"type": "string", "description": "File or directory to search in"},
                    "glob": {"type": "string", "description": "Glob filter for file types (e.g. '*.py')"},
                    "output_mode": {
                        "type": "string",
                        "enum": ["content", "files_with_matches", "count"],
                        "description": "content=matching lines, files_with_matches=file paths only, count=match counts"
                    },
                    "case_insensitive": {"type": "boolean", "description": "Case insensitive search"},
                    "context_lines": {"type": "integer", "description": "Lines of context around each match"}
                },
                "required": ["pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Execute a bash command. Returns stdout and stderr. Ask for confirmation before destructive operations.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The bash command to execute"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (default 300, max 600). Increase for very long-running commands."},
                    "description": {"type": "string", "description": "Brief description of what this command does"}
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web using SearXNG. Use for research, documentation lookup, current events.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query. Never use pronouns (he/she/him/her/they/it/this/that). Always use the specific name or term from conversation context. Example: if the user says 'research him' after discussing Anthony Scarola, the query must be 'Anthony Scarola', not 'him'."},
                    "num_results": {"type": "integer", "description": "Number of results to return (default 10)"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch the content of a URL. Returns the page text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to fetch"},
                    "max_chars": {"type": "integer", "description": "Maximum characters to return (default 8000)"}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": "Save an important fact, preference, or context to persistent memory for future sessions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "section": {"type": "string", "description": "Memory section: 'User Preferences', 'Project Context', or 'Important Facts'"},
                    "content": {"type": "string", "description": "The memory content to save (one concise bullet point)"}
                },
                "required": ["section", "content"]
            }
        }
    }
]
