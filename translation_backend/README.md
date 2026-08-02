# Translation backend adapter

This directory contains the backend-only integration of
[GMYXDS/AI-Markdown-Translator](https://github.com/GMYXDS/AI-Markdown-Translator).
Its line-based splitter, concurrent dispatcher, SQLite chunk state, OpenAI-compatible
client, and ordered merge behavior are used for whole-document translation.

The Research Toolbox web panel remains the only UI and the only owner of LLM
credentials. A selected preset is sent to this local process over stdin for the
duration of one invocation; API keys are never written to the command line or the
translation database.

The backend accepts Markdown (`.md`), Mathpix Markdown (`.mmd`), and raw HTML
(`.html` / `.htm`). Every format uses the same line-oriented chunk pipeline. HTML
is not parsed, protected, repaired, or translated node-by-node; the selected LLM is
instructed to preserve markup while translating visible prose.

The panel deliberately requests conservative chunks (about 7,200 characters after
the splitter safety factor). Provider responses that report `finish_reason=length`
or consume the configured output-token limit are rejected instead of being merged
as completed translations, preventing one truncated `$$` block from corrupting all
subsequent MathJax rendering.
