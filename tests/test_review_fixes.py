"""Adversarial tests for the Codex-review fixes (design/reviews/codex-on-runner.md #3-#12 + probable bugs).

Everything runs against an httpx MockTransport; no network, no GPU. Each test builds a throwaway
envs_dir/runs_dir under a TemporaryDirectory.

Run:  cd /scratch2/dhruv/ais/hp && /scratch2/dhruv/uv_envs/ais-hp/bin/python -m unittest tests.test_review_fixes -v
"""
import asyncio
import contextlib
import signal
import io
import json
import logging
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx
import yaml

from hp import HP_ROOT
from hp.backends import ModelSpec, generate
from hp.records import Record
from hp.runner import (LOCK_FILE, RunnerError, Writer, acquire_lock, build_items, load_config, main, read_records,
                       run, validate_config)
from hp.prompts import Registry

REG = Registry()
SECRET = "sk-or-SUPERSECRET-9f8e7d"
PACKET = "commit abc\n\nconfig: deployments\n| research-agent | {target_model_family} |\n+++ b/x.py\n"


def setUpModule():
    logging.getLogger().addHandler(logging.NullHandler())   # silence lastResort; main() installs its own handlers


def ok_completion(content="c", reasoning="r", prompt_token_ids=(1, 2), **over):
    body = {"id": "x", "model": "served", "object": "chat.completion",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content, "reasoning": reasoning},
                         "token_ids": [5]}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1}}
    if prompt_token_ids is not None:
        body["prompt_token_ids"] = list(prompt_token_ids)
    body.update(over)
    return body


def router(completion=None, models=None, render=None):
    """Build a MockTransport handler: GET /models, POST /chat/completions/render, POST /chat/completions.
    `completion` may be a dict, an httpx.Response, a callable(request)->Response, or a list consumed in order."""
    state = {"calls": []}

    def resp_of(x, req):
        if callable(x):
            return x(req)
        if isinstance(x, httpx.Response):
            return x
        return httpx.Response(200, json=x)

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path.endswith("/models"):
            return resp_of(models if models is not None else {"object": "list", "data": [{"id": "served"}]}, req)
        if path.endswith("/chat/completions/render"):
            return resp_of(render if render is not None else
                           {"sampling_params": {"temperature": 0.6, "top_p": 1.0, "seed": 0, "max_tokens": 10,
                                                "output_kind": 2, "skip_clone": True}}, req)
        state["calls"].append(json.loads(req.content))
        c = completion
        if isinstance(c, list):
            c = c[min(len(state["calls"]) - 1, len(c) - 1)]
        return resp_of(c if c is not None else ok_completion(), req)
    handler.state = state
    return handler


class Harness:
    """Temp envs_dir + runs_dir + config file. `go()` runs one invocation through hp.runner.run()."""

    def __init__(self, tmp: str, *, models=None, envs=None, conditions=("none",), n_samples=1, max_attempts=2,
                 packet=PACKET, extra_cfg=None):
        self.tmp = Path(tmp)
        self.envs_dir = self.tmp / "envs"; self.runs_dir = self.tmp / "runs"
        for e in (envs or [{"id": "e1"}]):
            d = self.envs_dir / e["id"]; d.mkdir(parents=True, exist_ok=True)
            (d / "packet.md").write_text(packet, encoding="utf-8")
            (d / "env.yaml").write_text("repo: cotmon\nbranch: monitor-v4\n", encoding="utf-8")
        self.cfg_dict = {
            "run_id": "t", "runs_dir": str(self.runs_dir), "envs_dir": str(self.envs_dir),
            "n_samples": n_samples, "order_seed": 1, "max_attempts": max_attempts, "backoff_s": 0.0,
            "envs": envs or [{"id": "e1"}], "conditions": list(conditions),
            "models": models or [{"id": "m1", "backend": "vllm", "url": "http://127.0.0.1:9/v1", "model": "served",
                                  "family": "Qwen3", "concurrency": 2, "params": {"temperature": 0.6, "max_tokens": 10},
                                  "extra": {"return_token_ids": True}, "render_probe": True}],
        }
        self.cfg_dict.update(extra_cfg or {})
        self.cfg_path = self.tmp / "cfg.yaml"
        self.cfg_path.write_text(yaml.safe_dump(self.cfg_dict, sort_keys=False), encoding="utf-8")
        self.cfg, self.sha, self.raw = load_config(self.cfg_path)
        self.run_dir = self.runs_dir / self.cfg.run_id

    def items(self):
        return build_items(self.cfg, REG)

    async def go_async(self, handler, *, selected=None, filters=None, dry_run=False, limit=None):
        items, packets = self.items()
        sel = selected(items) if selected else items
        return await run(self.cfg, self.sha, self.raw, items, sel, packets, run_dir=self.run_dir, dry_run=dry_run,
                         limit=limit, filters=filters,
                         make_client=lambda spec: httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    def go(self, handler, **kw):
        return asyncio.run(self.go_async(handler, **kw))

    def records(self):
        p = self.run_dir / "records.jsonl"
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()] if p.exists() else []

    def manifest(self):
        return json.loads((self.run_dir / "manifest.json").read_text(encoding="utf-8"))

    def invocations(self):
        return [json.loads(l) for l in (self.run_dir / "invocations.jsonl").read_text(encoding="utf-8").splitlines()]


# ---------------------------------------------------------------- #5 run lock / concurrent invocation

class TestRunLock(unittest.TestCase):
    def test_second_process_refused_and_stale_lock_taken_over(self):
        with tempfile.TemporaryDirectory() as d:
            code = ("import sys, time; from pathlib import Path; from hp.runner import acquire_lock\n"
                    "acquire_lock(Path(sys.argv[1])); print('locked', flush=True); time.sleep(60)")
            p = subprocess.Popen([sys.executable, "-c", code, d], cwd=HP_ROOT, stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(p.stdout.readline().strip(), "locked")
                with self.assertRaises(RunnerError) as cm:
                    acquire_lock(Path(d))
                self.assertIn(f"pid {p.pid}", str(cm.exception)); self.assertIn("locked", str(cm.exception))
            finally:
                p.kill(); p.wait()
            # the holder died without cleaning up: stale lock (dead pid, same host) is taken over
            self.assertTrue((Path(d) / LOCK_FILE).exists())
            lock = acquire_lock(Path(d))
            self.assertEqual(json.loads(lock.read_text())["pid"], os.getpid())

    def test_foreign_host_lock_is_never_taken_over(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / LOCK_FILE).write_text(json.dumps({"pid": 1, "host": "some-other-node", "started": "x"}))
            with self.assertRaises(RunnerError):
                acquire_lock(Path(d))

    def test_concurrent_invocation_refused_and_cancel_writes_attempt_record(self):
        """Two run() calls on one run_id: the second is refused while the first holds the lock. Cancelling the
        first mid-request leaves a `Cancelled` attempt record and releases the lock."""
        async def slow(req):
            await asyncio.sleep(30)
            return httpx.Response(200, json=ok_completion())
        h = router(completion=slow)

        async def scenario(hz: Harness):
            first = asyncio.create_task(hz.go_async(h))
            await asyncio.sleep(0.5)                       # first invocation is in flight on its request
            self.assertTrue((hz.run_dir / LOCK_FILE).exists())
            with self.assertRaises(RunnerError) as cm:
                await hz.go_async(h)                       # same run_id, same process: lock holder is alive
            self.assertIn("locked by pid", str(cm.exception))
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
        with tempfile.TemporaryDirectory() as d:
            hz = Harness(d)
            asyncio.run(scenario(hz))
            recs = hz.records()
            self.assertEqual(len(recs), 1)
            self.assertFalse(recs[0]["ok"]); self.assertEqual(recs[0]["error"]["type"], "Cancelled")
            self.assertTrue(recs[0]["error"]["retryable"]); self.assertEqual(recs[0]["attempt"], 1)
            Record.model_validate(recs[0])
            self.assertFalse((hz.run_dir / LOCK_FILE).exists())           # released on the way out
            # the next invocation retries it with attempt 2
            out = hz.go(router())
            self.assertEqual(out["summary"]["n_ok"], 1)
            self.assertEqual([r["attempt"] for r in hz.records()], [1, 2])


    def test_cancel_while_queued_leaves_no_record(self):
        """Only the request actually in flight gets a Cancelled record; items still waiting on the semaphore
        never issued a request and must not consume an attempt (reviewer finding M1)."""
        async def slow(req):
            await asyncio.sleep(30)
            return httpx.Response(200, json=ok_completion())
        h = router(completion=slow)

        async def scenario(hz: Harness):
            t = asyncio.create_task(hz.go_async(h))
            await asyncio.sleep(0.5)
            t.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await t
        with tempfile.TemporaryDirectory() as d:
            hz = Harness(d, n_samples=6, models=[{"id": "m1", "backend": "vllm", "url": "http://127.0.0.1:9/v1",
                                                  "model": "served", "family": "Qwen3", "concurrency": 1}])
            asyncio.run(scenario(hz))
            self.assertEqual(len(h.state["calls"]), 1)
            recs = hz.records()
            self.assertEqual(len(recs), 1); self.assertEqual(recs[0]["error"]["type"], "Cancelled")
            self.assertEqual(hz.go(router())["summary"]["n_ok"], 6)
            self.assertEqual(sorted(r["attempt"] for r in hz.records()), [1, 1, 1, 1, 1, 1, 2])

    def test_sigterm_cancels_like_ctrl_c(self):
        """SIGTERM (Slurm timeout/scancel) -> in-flight request gets a Cancelled record, lock released."""
        async def slow(req):
            await asyncio.sleep(30)
            return httpx.Response(200, json=ok_completion())
        h = router(completion=slow)

        async def scenario(hz: Harness):
            t = asyncio.create_task(hz.go_async(h))
            await asyncio.sleep(0.5)
            signal.raise_signal(signal.SIGTERM)
            with self.assertRaises(asyncio.CancelledError):
                await t
        with tempfile.TemporaryDirectory() as d:
            hz = Harness(d)
            asyncio.run(scenario(hz))
            recs = hz.records()
            self.assertEqual([r["error"]["type"] for r in recs], ["Cancelled"])
            self.assertFalse((hz.run_dir / LOCK_FILE).exists())
            self.assertEqual(signal.getsignal(signal.SIGTERM), signal.SIG_DFL)   # handler removed on exit


# ---------------------------------------------------------------- #6 ledger validation

class TestLedgerValidation(unittest.TestCase):
    def test_invalid_unrelated_and_duplicate_records_are_reported(self):
        with tempfile.TemporaryDirectory() as d:
            hz = Harness(d)
            hz.go(router())
            good = hz.records()[0]
            man = hz.manifest()
            wrong_cfg = dict(good, config_sha256="0" * 64)
            wrong_hash = json.loads(json.dumps(good)); wrong_hash["prompt_hashes"]["messages"] = "deadbeef"
            wrong_model = json.loads(json.dumps(good)); wrong_model["model"]["id"] = "other"
            unknown = dict(good, item_key="zzz|none|m1|0")
            lines = [json.dumps(x, ensure_ascii=False) for x in (
                {"item_key": good["item_key"], "ok": True},     # the Codex example: minimal fake success
                wrong_cfg, wrong_hash, wrong_model, unknown, good, good)]  # last two: duplicate success
            p = hz.run_dir / "records.jsonl"
            p.write_text("\n".join(lines) + "\n", encoding="utf-8")
            led = read_records(p, expected=man["items"], run_id="t", config_sha256=hz.sha)
            self.assertEqual(led.n_lines, 7)
            reasons = {b["line"]: b["reason"] for b in led.bad}
            self.assertTrue(reasons[1].startswith("schema:"))
            self.assertIn("config_sha256", reasons[2])
            self.assertIn("prompt_hashes.messages", reasons[3])
            self.assertIn("model_id", reasons[4])
            self.assertIn("not in manifest", reasons[5])
            self.assertEqual(set(reasons), {1, 2, 3, 4, 5})
            self.assertTrue(all(b["item_key"] for b in led.bad))        # every report names the item
            self.assertEqual(led.done, {good["item_key"]})
            self.assertEqual(led.duplicates, {good["item_key"]: 2})
            self.assertEqual(led.duplicate_attempts, [{"line": 7, "item_key": good["item_key"], "attempt": 1}])
            # a ledger holding ONLY the fake success -> the item is not done and gets re-run
            p.write_text(lines[0] + "\n", encoding="utf-8")
            out = hz.go(router())
            self.assertEqual(out["summary"]["n_ok"], 1)
            self.assertEqual(out["invocation"]["ledger"]["n_bad"], 1)
            self.assertEqual(out["invocation"]["ledger"]["bad"][0]["line"], 1)


# ---------------------------------------------------------------- #3 / #4 provenance

class TestProvenance(unittest.TestCase):
    def test_record_carries_calling_process_code_hash_manifest_keeps_original(self):
        with tempfile.TemporaryDirectory() as d:
            hz = Harness(d, n_samples=2)
            hz.go(router(), limit=1)
            man0 = hz.manifest()
            with mock.patch("hp.runner.code_hash", return_value="f" * 64), \
                 mock.patch("hp.runner.git_state", return_value=("abc123", False)):
                hz.go(router())
            r1, r2 = hz.records()
            self.assertEqual(r1["hp_code_hash"], man0["hp_code_hash"])
            self.assertEqual(r2["hp_code_hash"], "f" * 64)
            self.assertEqual((r2["hp_git_commit"], r2["hp_git_dirty"]), ("abc123", False))
            man1 = hz.manifest()
            self.assertEqual(man1["hp_code_hash"], man0["hp_code_hash"])       # manifest keeps the original
            self.assertEqual(man1["invocations"][1]["hp_code_hash"], "f" * 64)  # the invocation says what ran

    def test_changed_branch_family_or_packet_refuses_resume(self):
        with tempfile.TemporaryDirectory() as d:
            hz = Harness(d, n_samples=2)
            hz.go(router(), limit=1)
            # branch changes in env.yaml -> rendered user message changes -> refused
            (hz.envs_dir / "e1" / "env.yaml").write_text("repo: cotmon\nbranch: monitor-v5\n", encoding="utf-8")
            with self.assertRaises(RunnerError) as cm:
                hz.go(router())
            self.assertIn("packets", str(cm.exception)); self.assertIn("items", str(cm.exception))
            (hz.envs_dir / "e1" / "env.yaml").write_text("repo: cotmon\nbranch: monitor-v4\n", encoding="utf-8")
            # target_model_family changes (same config file bytes are NOT required: patch the loaded config)
            hz.cfg.models[0].family = "gpt-oss"
            with self.assertRaises(RunnerError) as cm:
                hz.go(router())
            self.assertIn("model_families", str(cm.exception))
            hz.cfg.models[0].family = "Qwen3"
            # packet edit -> refused
            (hz.envs_dir / "e1" / "packet.md").write_text(PACKET + "extra line\n", encoding="utf-8")
            with self.assertRaises(RunnerError):
                hz.go(router())
            (hz.envs_dir / "e1" / "packet.md").write_text(PACKET, encoding="utf-8")
            self.assertEqual(hz.go(router())["summary"]["n_ok"], 1)       # restored inputs resume fine
            self.assertEqual(len(hz.records()), 2)


# ---------------------------------------------------------------- #7 malformed 200

class TestMalformed200(unittest.TestCase):
    def _gen(self, body, spec=None, status=200):
        spec = spec or ModelSpec(id="q", backend="vllm", url="http://mock/v1", model="m", family="f",
                                 extra={"return_token_ids": True})
        resp = body if isinstance(body, httpx.Response) else httpx.Response(status, json=body)
        meta = dict(run_id="t", item_key="e|none|m|0", hp_git_commit=None, hp_git_dirty=None, hp_code_hash="x",
                    config_sha256="y", env_id="e", condition="none", sample_idx=0, attempt=1, prompt_ids={}, prompt_hashes={})

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: resp)) as c:
                return await generate(spec, [{"role": "user", "content": "u"}], {}, meta=meta, client=c)
        return asyncio.run(go())

    def test_shapes_are_failed_retryable_attempts(self):
        cases = {
            "no choices": {"id": "x"},
            "empty choices": {"choices": []},
            "choice without message": {"choices": [{}]},                       # the Codex repro
            "empty content and no reasoning": ok_completion(content="", reasoning=None),
            "whitespace content, empty reasoning": ok_completion(content=" \n", reasoning=""),
            "null content": ok_completion(content=None, reasoning=None),
            "prompt_token_ids missing when requested": ok_completion(prompt_token_ids=None),
            "prompt_token_ids empty when requested": ok_completion(prompt_token_ids=()),
            "completion token_ids missing when requested": ok_completion(
                choices=[{"index": 0, "finish_reason": "stop", "message": {"content": "c", "reasoning": "r"}}]),
            "finish_reason missing": ok_completion(
                choices=[{"index": 0, "message": {"content": "c", "reasoning": "r"}, "token_ids": [5]}]),
            "json list body": httpx.Response(200, json=[1, 2]),
            "non-json 200": httpx.Response(200, text="<html>gateway</html>"),
        }
        for name, body in cases.items():
            with self.subTest(name):
                r = self._gen(body)
                self.assertFalse(r.ok, name)
                self.assertEqual(r.error.type, "MalformedResponse", name)
                self.assertTrue(r.error.retryable, name)
                Record.model_validate_json(r.to_json())
        # content empty but reasoning present (truncated mid-thought) is a sample, not malformed
        r = self._gen(ok_completion(content="", reasoning="thinking"))
        self.assertTrue(r.ok)
        # token ids not requested -> their absence is fine
        r = self._gen(ok_completion(prompt_token_ids=None),
                      spec=ModelSpec(id="q", backend="vllm", url="http://mock/v1", model="m", family="f"))
        self.assertTrue(r.ok)

    def test_runner_retries_malformed_then_records_success(self):
        seq = [{"choices": [{}]}, ok_completion(content="", reasoning=None), ok_completion(prompt_token_ids=None),
               ok_completion()]
        with tempfile.TemporaryDirectory() as d:
            hz = Harness(d, max_attempts=4)
            out = hz.go(router(completion=seq))
            self.assertEqual(out["summary"]["n_ok"], 1)
            recs = hz.records()
            self.assertEqual([r["ok"] for r in recs], [False, False, False, True])
            self.assertEqual({r["error"]["type"] for r in recs[:3]}, {"MalformedResponse"})
            self.assertEqual(recs[3]["attempt"], 4)
            self.assertEqual(recs[0]["raw_response"], {"choices": [{}]})     # malformed body is kept
            self.assertEqual(hz.go(router())["summary"]["n_runnable"], 0)    # resume: done, not re-run

    def test_malformed_is_not_permanently_skipped(self):
        """A malformed 200 that exhausts max_attempts leaves the item pending; the next invocation retries it."""
        with tempfile.TemporaryDirectory() as d:
            hz = Harness(d, max_attempts=1)
            self.assertEqual(hz.go(router(completion={"choices": [{}]}))["summary"]["n_failed"], 1)
            self.assertEqual(hz.go(router())["summary"]["n_ok"], 1)
            self.assertEqual([r["attempt"] for r in hz.records()], [1, 2])


# ---------------------------------------------------------------- #8 redaction everywhere

class TestRedaction(unittest.TestCase):
    def test_secret_scrubbed_from_success_models_and_render_bodies(self):
        os.environ["VLLM_API_KEY"] = SECRET
        try:
            h = router(completion=ok_completion(content=f"my key is {SECRET}", reasoning=f"r {SECRET}",
                                                echo={"auth": f"Bearer {SECRET}", SECRET: 1}),
                       models={"data": [{"id": "served", "owner": SECRET}]},
                       render={"sampling_params": {"temperature": 0.6}, "debug": [SECRET]})
            with tempfile.TemporaryDirectory() as d:
                hz = Harness(d)
                out = hz.go(h)
                self.assertEqual(out["summary"]["n_ok"], 1)
                for name in ("records.jsonl", "invocations.jsonl", "manifest.json"):
                    text = (hz.run_dir / name).read_text(encoding="utf-8")
                    self.assertNotIn(SECRET, text, name)
                rec = hz.records()[0]
                self.assertTrue(rec["ok"])
                self.assertIn("[REDACTED:api_key]", rec["content"]); self.assertIn("[REDACTED:api_key]", rec["reasoning"])
                self.assertIn("[REDACTED:api_key]", json.dumps(rec["raw_response"]))
                inv = hz.invocations()[0]["servers"]["m1"]
                self.assertIn("[REDACTED:api_key]", json.dumps(inv["models"]["response"]))
                self.assertIn("[REDACTED:api_key]", json.dumps(inv["render_probe"]["response"]))
                # the key was actually sent (in the header), not just absent
                self.assertNotIn(SECRET, json.dumps(h.state["calls"]))
        finally:
            del os.environ["VLLM_API_KEY"]

    def test_secret_in_non_json_error_body(self):
        os.environ["VLLM_API_KEY"] = SECRET
        try:
            spec = ModelSpec(id="q", backend="vllm", url="http://mock/v1", model="m", family="f")
            meta = dict(run_id="t", item_key="k", hp_git_commit=None, hp_git_dirty=None, hp_code_hash="x",
                        config_sha256="y", env_id="e", condition="none", sample_idx=0, attempt=1, prompt_ids={}, prompt_hashes={})

            async def go():
                async with httpx.AsyncClient(transport=httpx.MockTransport(
                        lambda r: httpx.Response(502, text=f"bad gateway for {SECRET}"))) as c:
                    return await generate(spec, [{"role": "user", "content": "u"}], {}, meta=meta, client=c)
            r = asyncio.run(go())
            self.assertFalse(r.ok); self.assertNotIn(SECRET, r.to_json())
            self.assertEqual(r.raw_response, "bad gateway for [REDACTED:api_key]")   # text body kept (#9)
        finally:
            del os.environ["VLLM_API_KEY"]


# ---------------------------------------------------------------- #9 raw_response any JSON + effective params

class TestRawAndEffectiveParams(unittest.TestCase):
    def test_list_error_body_kept_and_effective_params_recorded(self):
        with tempfile.TemporaryDirectory() as d:
            hz = Harness(d, max_attempts=2)
            out = hz.go(router(completion=[httpx.Response(500, json=["boom", {"code": 1}]), ok_completion()]))
            self.assertEqual(out["summary"]["n_ok"], 1)
            bad, good = hz.records()
            self.assertEqual(bad["raw_response"], ["boom", {"code": 1}])
            self.assertEqual(good["params"], {"temperature": 0.6, "max_tokens": 10, "seed": 0})
            # requested params over the server-resolved defaults from the render probe (top_p from the server;
            # engine-internal fields dropped; the probe item's own requested values are not "defaults")
            self.assertEqual(good["effective_params"], {"temperature": 0.6, "max_tokens": 10, "seed": 0, "top_p": 1.0})
            self.assertEqual(good["effective_params_source"], "requested+vllm_render_probe")
            self.assertEqual(bad["effective_params_source"], "requested+vllm_render_probe")

    def test_no_render_probe_means_requested_only(self):
        with tempfile.TemporaryDirectory() as d:
            hz = Harness(d)
            hz.cfg.models[0].render_probe = False
            hz.go(router())
            r = hz.records()[0]
            self.assertEqual(r["effective_params_source"], "requested_only")
            self.assertEqual(r["effective_params"], r["params"])


# ---------------------------------------------------------------- #10 filtered invocations

class TestFilteredInvocations(unittest.TestCase):
    def test_filter_stored_and_totals_reconciled(self):
        models = [{"id": "m1", "backend": "vllm", "url": "http://mock/v1", "model": "a", "family": "Qwen3", "concurrency": 2},
                  {"id": "m2", "backend": "vllm", "url": "http://mock2/v1", "model": "b", "family": "gpt-oss", "concurrency": 2}]
        with tempfile.TemporaryDirectory() as d:
            hz = Harness(d, models=models, n_samples=2)         # grid: 1 env x 1 cond x 2 models x 2 = 4
            out = hz.go(router(), selected=lambda items: [i for i in items if i.model_id == "m1"], filters={"models": ["m1"]})
            man = hz.manifest()
            self.assertEqual(man["n_items"], 4); self.assertEqual(len(man["items"]), 4)   # full grid, not 2
            self.assertEqual(man["invocations"][0]["filters"], {"models": ["m1"], "limit": None})
            self.assertEqual(man["invocations"][0]["n_selected"], 2)
            self.assertEqual(man["totals"], {"n_items": 4, "n_done": 2, "n_pending": 2, "updated": out["summary"]["finished"]})
            out = hz.go(router())                                   # unfiltered: reconciles to the whole grid
            self.assertEqual(out["summary"]["n_runnable"], 2)
            man = hz.manifest()
            self.assertEqual(man["totals"]["n_done"], 4); self.assertEqual(man["totals"]["n_pending"], 0)
            self.assertEqual(len(man["invocations"]), 2); self.assertEqual(man["invocations"][1]["filters"], {"limit": None})
            self.assertEqual(sorted(r["model"]["id"] for r in hz.records()), ["m1", "m1", "m2", "m2"])


# ---------------------------------------------------------------- #11 missing placeholder, #12 ids, probable bugs

class TestConfigValidation(unittest.TestCase):
    def test_missing_placeholder_is_hard_error_unless_overridden(self):
        with tempfile.TemporaryDirectory() as d:
            hz = Harness(d, packet="commit abc\nno slot here\n")
            with self.assertRaises(RunnerError) as cm:
                hz.items()
            self.assertIn("{target_model_family}", str(cm.exception)); self.assertIn("allow_missing_family_placeholder", str(cm.exception))
            hz.cfg.envs[0].allow_missing_family_placeholder = True
            items, packets = hz.items()
            self.assertEqual(len(items), 1); self.assertFalse(packets["e1"]["has_family_placeholder"])

    def test_duplicate_ids_and_separator_rejected(self):
        base = {"id": "m1", "backend": "vllm", "url": "http://mock/v1", "model": "a", "family": "Q"}
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(RunnerError) as cm:
                Harness(d, models=[base, dict(base, family="other")]).items()
            self.assertIn("duplicate model id 'm1'", str(cm.exception))
            with self.assertRaises(RunnerError) as cm:
                Harness(d, conditions=["none", "none"]).items()
            self.assertIn("duplicate condition", str(cm.exception))
            with self.assertRaises(RunnerError) as cm:
                Harness(d, models=[dict(base, id="a|b")]).items()
            self.assertIn("contains '|'", str(cm.exception))
            with self.assertRaises(RunnerError) as cm:
                Harness(d, envs=[{"id": "e1"}, {"id": "e1"}]).items()
            self.assertIn("duplicate env id", str(cm.exception))

    def test_filter_typo_is_refused_not_exit_0(self):
        from hp.runner import _filter
        with tempfile.TemporaryDirectory() as d:
            items, _ = Harness(d).items()
            with self.assertRaises(RunnerError) as cm:
                _filter(items, "m1,gpt-oss-120b", None, None)
            self.assertIn("['gpt-oss-120b']", str(cm.exception))
            with self.assertRaises(RunnerError):
                _filter(items, None, None, "nonе")        # cyrillic е: looks right, matches nothing
            sel, f = _filter(items, "m1", "e1", "none")
            self.assertEqual(len(sel), 1); self.assertEqual(f, {"models": ["m1"], "envs": ["e1"], "conditions": ["none"]})

    def test_seed_in_params_with_seed_from_sample_idx_refused(self):
        base = {"id": "m1", "backend": "vllm", "url": "http://mock/v1", "model": "a", "family": "Q", "params": {"seed": 7}}
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(RunnerError) as cm:
                Harness(d, models=[base]).items()
            self.assertIn("params.seed", str(cm.exception))
            items, _ = Harness(d, models=[base], extra_cfg={"seed_from_sample_idx": False}).items()
            self.assertEqual(items[0].params["seed"], 7)

    def test_shared_url_concurrency_must_agree(self):
        base = {"backend": "vllm", "url": "http://mock/v1", "model": "a", "family": "Q"}
        with tempfile.TemporaryDirectory() as d:
            hz = Harness(d, models=[dict(base, id="m1", concurrency=4), dict(base, id="m2", concurrency=8)])
            with self.assertRaises(RunnerError) as cm:
                validate_config(hz.cfg)
            self.assertIn("disagree on concurrency", str(cm.exception))
            hz = Harness(d, models=[dict(base, id="m1", concurrency=4), dict(base, id="m2", concurrency=4)])
            self.assertEqual(validate_config(hz.cfg), {"http://mock/v1": 4})

    def test_params_and_extra_cannot_override_model_or_messages(self):
        for params, extra in (({"model": "x"}, {}), ({}, {"messages": []}), ({"stream": True}, {}),
                              ({"temperature": 1}, {"temperature": 0})):
            with self.subTest(params=params, extra=extra), self.assertRaises(ValueError):
                ModelSpec(id="q", backend="vllm", url="http://mock/v1", model="m", family="f", params=params, extra=extra)
        spec = ModelSpec(id="q", backend="vllm", url="http://mock/v1", model="m", family="f", extra={"return_token_ids": True})
        meta = dict(run_id="t", item_key="k", hp_git_commit=None, hp_git_dirty=None, hp_code_hash="x", config_sha256="y",
                    env_id="e", condition="none", sample_idx=0, attempt=1, prompt_ids={}, prompt_hashes={})
        r = asyncio.run(generate(spec, [{"role": "user", "content": "u"}], {"messages": [], "return_token_ids": False}, meta=meta))
        self.assertFalse(r.ok); self.assertFalse(r.error.retryable); self.assertIn("runner-owned", r.error.message)


# ---------------------------------------------------------------- writer failure, dry-run

class TestWriterAndDryRun(unittest.TestCase):
    def test_writer_failure_mirrors_record(self):
        with tempfile.TemporaryDirectory() as d:
            w = Writer(Path(d) / "records.jsonl")
            w.f.close()                                        # simulate a dead ledger handle
            rec = Record(run_id="t", item_key="k", timestamp="t", hp_git_commit=None, hp_git_dirty=None, hp_code_hash="x",
                         config_sha256="y", env_id="e", condition="none", sample_idx=0, attempt=1,
                         model={"id": "m", "backend": "vllm", "url": "u", "model": "s", "family": "f", "reasoning_visibility": "raw"},
                         params={}, messages=[], request_body={}, prompt_ids={}, prompt_hashes={}, ok=True, latency_s=0.0)
            with self.assertLogs("hp.runner", level="ERROR"):
                self.assertFalse(w.write(rec))
            self.assertEqual(w.n_failed, 1)
            self.assertEqual(json.loads((Path(d) / "records.unwritten.jsonl").read_text())["item_key"], "k")

    def test_dry_run_writes_nothing_and_lists_missing_packets(self):
        with tempfile.TemporaryDirectory() as d:
            hz = Harness(d, envs=[{"id": "e1"}, {"id": "e_missing"}, {"id": "e_missing2"}])
            for e in ("e_missing", "e_missing2"):
                (hz.envs_dir / e / "packet.md").unlink()
            log_dir = Path(d) / "logs"
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = main([str(hz.cfg_path), "--dry-run", "--log-dir", str(log_dir)])
            self.assertEqual(rc, 3)
            self.assertIn("missing packet.md for 2 env(s)", err.getvalue())
            self.assertIn(str(hz.envs_dir / "e_missing" / "packet.md"), err.getvalue())
            self.assertIn(str(hz.envs_dir / "e_missing2" / "packet.md"), err.getvalue())
            self.assertFalse(log_dir.exists()); self.assertFalse(hz.runs_dir.exists())
            # a well-formed dry run: exit 0, still nothing written (server probe fails: nothing listens on mock host)
            hz2 = Harness(d, extra_cfg={"run_id": "t2"})
            out = io.StringIO()
            with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(out):
                rc = main([str(hz2.cfg_path), "--dry-run", "--log-dir", str(log_dir)])
            self.assertEqual(rc, 0)
            self.assertIn("[dry-run] run_id=t2 grid=1 selected=1 done=0 todo=1", out.getvalue())
            self.assertFalse(log_dir.exists()); self.assertFalse(hz2.run_dir.exists())
            logging.getLogger().handlers.clear(); logging.getLogger().addHandler(logging.NullHandler())


if __name__ == "__main__":
    unittest.main()
