"""hp: shared primitives for the honeypot eval (paper 07, code-review setting).

Modules:
  backends  - one generate() over openrouter / anthropic / vllm backends (used by agent/)
  records   - pydantic schema of the per-call record (BUILD_SPEC "Records")

The single-turn generation pipeline (prompts/render/conditions/runner) was retired to the
out-of-tree archive; the agentic harness in agent/ is the current path and imports only
hp.backends (+ hp.records).
"""

HP_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
