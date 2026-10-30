"""hp: honeypot replication runner (paper 07, code-review setting).

Modules:
  prompts   - prompt registry loader with sha256 verification
  render    - builds the system and user messages from registered prompts + env packet
  backends  - one generate() over vllm / openrouter / anthropic backends
  records   - pydantic schema of the per-call record (BUILD_SPEC "Records")
  runner    - YAML config -> manifest -> bounded-concurrency execution -> records.jsonl
"""

HP_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
