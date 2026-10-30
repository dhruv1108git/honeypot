"""Adversarial tests for the Codex round-2 review fixes (design/reviews/codex-on-runner-r2.md).

Covers: #6 untrusted ledger validation, #7 any bad 200 is retryable, #8 one redaction choke point for every
persisted payload, reserved sampling/provider fields, partial trailing ledger line, cancellation records carry
the request body, numeric control ranges. MockTransport only; no network, no GPU.

Run:  cd /scratch2/dhruv/ais/hp && /scratch2/dhruv/uv_envs/ais-hp/bin/python -m unittest tests.test_review_fixes_r2 -v
"""
import asyncio
import contextlib
import io
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx
from pydantic import ValidationError

from hp.backends import ModelSpec, build_body, build_request, failed_record, generate
from hp.records import SCHEMA_VERSION, Record
from hp.runner import (LOCK_FILE, RunConfig, RunnerError, Writer, build_items, load_config, main, messages_sha256,
                       read_records, repair_trailing_newline, validate_config, validate_record)
from tests.test_review_fixes import PACKET, REG, Harness, ok_completion, router

SECRET = "sk-or-ROUND2-SECRET-5a4b3c"
META = dict(run_id="t", item_key="e|none|m|0", hp_git_commit=None, hp_git_dirty=None, hp_code_hash="x",
            config_sha256="y", env_id="e", condition="none", sample_idx=0, attempt=1, prompt_ids={}, prompt_hashes={})


def setUpModule():
    logging.getLogger().addHandler(logging.NullHandler())


def _gen(body, spec=None, params=None):
    spec = spec or ModelSpec(id="q", backend="vllm", url="http://mock/v1", model="m", family="f")
    resp = body if isinstance(body, httpx.Response) else httpx.Response(200, json=body)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: resp)) as c:
            return await generate(spec, [{"role": "user", "content": "u"}], params or {}, meta=META, client=c)
    return asyncio.run(go())


# ---------------------------------------------------------------- #6 validation trusts nothing self-reported

class TestUntrustedLedger(unittest.TestCase):
    def test_codex_fabricated_record_rejected_even_without_manifest(self):
        """The round-2 repro: schema_version 1, attempt 0, empty messages/request, no content/finish_reason, ok=true."""
        fake = dict(META, schema_version=1, attempt=0, timestamp="t", latency_s=0.0, ok=True, params={}, messages=[],
                    request_body={}, model={"id": "zzz", "backend": "openrouter", "url": "http://wrong", "model": "w",
                                            "family": "w", "reasoning_visibility": "hidden"})
        self.assertIsNotNone(validate_record(fake, expected=None, run_id=None, config_sha256=None))
        # fix the fields one at a time: each remaining defect is still caught
        fake["attempt"] = 1
        self.assertIn("schema_version", validate_record(fake, expected=None, run_id=None, config_sha256=None))
        fake["schema_version"] = SCHEMA_VERSION
        self.assertIn("stored messages", validate_record(fake, expected=None, run_id=None, config_sha256=None))
        msgs = [{"role": "user", "content": "u"}]
        fake["messages"] = msgs; fake["prompt_hashes"] = {"messages": messages_sha256(msgs)}
        self.assertIn("empty request_body", validate_record(fake, expected=None, run_id=None, config_sha256=None))
        fake["request_body"] = {"model": "w", "messages": msgs}
        self.assertIn("empty content", validate_record(fake, expected=None, run_id=None, config_sha256=None))
        fake["content"] = "c"
        self.assertIn("finish_reason", validate_record(fake, expected=None, run_id=None, config_sha256=None))
        fake["finish_reason"] = "stop"
        self.assertIsNone(validate_record(fake, expected=None, run_id=None, config_sha256=None))
        # ... but against a manifest it is still rejected (wrong model block)
        with tempfile.TemporaryDirectory() as d:
            hz = Harness(d)
            hz.go(router())
            man = hz.manifest(); good = hz.records()[0]
            fake.update(item_key=good["item_key"], run_id="t", config_sha256=hz.sha, env_id="e1",
                        messages=good["messages"], prompt_hashes=good["prompt_hashes"], request_body=good["request_body"],
                        prompt_token_ids=good["prompt_token_ids"], completion_token_ids=good["completion_token_ids"])
            fake["model"]["id"] = "m1"
            self.assertIn("model block", validate_record(fake, expected=man["items"], run_id="t", config_sha256=hz.sha))

    def test_every_tampered_field_is_caught_and_item_is_redone(self):
        with tempfile.TemporaryDirectory() as d:
            hz = Harness(d)
            hz.go(router())
            good = hz.records()[0]
            man = hz.manifest()
            exp = man["items"][good["item_key"]]
            # the manifest now carries the full item spec
            self.assertEqual(exp["params"], {"temperature": 0.6, "max_tokens": 10, "seed": 0})
            self.assertEqual(exp["model"]["backend"], "vllm"); self.assertEqual(exp["model"]["family"], "Qwen3")
            self.assertEqual(len(exp["request_body_sha256"]), 64)
            self.assertIsNone(validate_record(good, expected=man["items"], run_id="t", config_sha256=hz.sha))

            def variant(**changes):
                r = json.loads(json.dumps(good))
                for path, val in changes.items():
                    cur = r; parts = path.split(".")
                    for p_ in parts[:-1]:
                        cur = cur[p_]
                    cur[parts[-1]] = val
                return r
            tampered_msgs = [{"role": "system", "content": "evil"}, {"role": "user", "content": "x"}]
            cases = {
                "schema_version 1": (variant(schema_version=1), "schema_version"),
                "attempt 0": (variant(attempt=0), "attempt"),                      # pydantic ge=1 -> schema error
                "messages changed, self-reported hash kept": (variant(messages=tampered_msgs), "stored messages"),
                "messages changed, hash recomputed to match": (
                    variant(messages=tampered_msgs, **{"prompt_hashes.messages": messages_sha256(tampered_msgs)}),
                    "messages"),                                                   # body/messages or manifest mismatch
                "messages+body+hash all changed consistently": (
                    variant(messages=tampered_msgs, **{"prompt_hashes.messages": messages_sha256(tampered_msgs),
                                                       "request_body.messages": tampered_msgs}), "manifest"),
                "wrong backend": (variant(**{"model.backend": "openrouter"}), "model block"),
                "wrong url": (variant(**{"model.url": "http://elsewhere/v1"}), "model block"),
                "wrong family": (variant(**{"model.family": "gpt-oss"}), "model block"),
                "wrong served model": (variant(**{"model.model": "other"}), "model block"),
                "wrong params": (variant(params={"temperature": 0.0, "max_tokens": 10, "seed": 0}), "params"),
                "extra param": (variant(params={"temperature": 0.6, "max_tokens": 10, "seed": 0, "top_p": 0.1}), "params"),
                "request body tampered": (variant(**{"request_body.temperature": 0.0}), "request_body"),
                "request body messages differ": (variant(**{"request_body.messages": tampered_msgs}), "request_body"),
                "ok but empty content and reasoning": (variant(content="", reasoning=None), "empty content"),
                "ok but no finish_reason": (variant(finish_reason=None), "finish_reason"),
                "ok with an error": (variant(error={"type": "x", "message": "m"}), "carries an error"),
                "failed without error": (variant(ok=False), "no error"),
                "ok with empty request body": (variant(request_body={}), "empty request_body"),
                "truncated disagrees with finish_reason": (variant(truncated=True), "truncated"),
                "token ids missing although requested": (variant(prompt_token_ids=None), "prompt_token_ids"),
                "ok is 1 not true (strict)": (variant(ok=1), "schema"),
                "schema_version is a string (strict)": (variant(schema_version="2"), "schema"),
                "attempt is a float (strict)": (variant(attempt=1.0), "schema"),
            }
            for name, (rec, needle) in cases.items():
                with self.subTest(name):
                    reason = validate_record(rec, expected=man["items"], run_id="t", config_sha256=hz.sha)
                    self.assertIsNotNone(reason, name)
                    self.assertIn(needle, reason, name)
            # a ledger made only of tampered "successes": nothing is done, every line is reported, item re-run
            p = hz.run_dir / "records.jsonl"
            p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r, _ in cases.values()) + "\n", encoding="utf-8")
            led = read_records(p, expected=man["items"], run_id="t", config_sha256=hz.sha)
            self.assertEqual(led.done, set()); self.assertEqual(len(led.bad), len(cases))
            self.assertEqual([b["line"] for b in led.bad], list(range(1, len(cases) + 1)))
            out = hz.go(router())
            self.assertEqual(out["summary"]["n_ok"], 1)
            self.assertEqual(out["invocation"]["ledger"]["n_bad"], len(cases))
            self.assertEqual(hz.records()[-1]["attempt"], 2)          # attempts continue past the tampered lines
            self.assertEqual(hz.go(router())["summary"]["n_runnable"], 0)

    def test_genuine_failed_record_is_valid_for_the_manifest(self):
        """A real failed attempt (cancelled, 5xx) must not be flagged: the body check applies to its stored body."""
        with tempfile.TemporaryDirectory() as d:
            hz = Harness(d, max_attempts=2)
            hz.go(router(completion=[httpx.Response(503, json={"e": 1}), ok_completion()]))
            man = hz.manifest()
            bad, good = hz.records()
            self.assertFalse(bad["ok"]); self.assertTrue(good["ok"])
            for r in (bad, good):
                self.assertIsNone(validate_record(r, expected=man["items"], run_id="t", config_sha256=hz.sha))

    def test_changed_item_spec_refuses_resume(self):
        """params / model block are part of the frozen manifest view now (not only the messages hash)."""
        with tempfile.TemporaryDirectory() as d:
            hz = Harness(d, n_samples=2)
            hz.go(router(), limit=1)
            hz.cfg.models[0].params["temperature"] = 0.0          # same config bytes, different item spec
            with self.assertRaises(RunnerError) as cm:
                hz.go(router())
            self.assertIn("items", str(cm.exception))


    def test_lax_ok_is_bad_not_limbo(self):
        """`ok: 1` used to pass validation (pydantic lax) but not `r.get("ok") is True`: neither done nor bad, so
        the item was silently re-run on every resume. Strict validation makes it a reported bad line."""
        with tempfile.TemporaryDirectory() as d:
            hz = Harness(d)
            hz.go(router())
            good = hz.records()[0]; man = hz.manifest()
            p = hz.run_dir / "records.jsonl"
            p.write_text(json.dumps(dict(good, ok=1)) + "\n", encoding="utf-8")
            led = read_records(p, expected=man["items"], run_id="t", config_sha256=hz.sha)
            self.assertEqual(led.done, set()); self.assertEqual(len(led.bad), 1)
            self.assertIn("schema", led.bad[0]["reason"])
            # and `attempt` comes from the typed model: "attempt": "2" is a schema error, not attempt 0
            p.write_text(json.dumps(dict(good, attempt="2")) + "\n", encoding="utf-8")
            led = read_records(p, expected=man["items"], run_id="t", config_sha256=hz.sha)
            self.assertEqual(len(led.bad), 1); self.assertEqual(led.attempts, {good["item_key"]: 2})


# ---------------------------------------------------------------- #7 any bad 200 is retryable

class TestBad200Retryable(unittest.TestCase):
    def test_schema_mismatch_and_parse_crash_are_retryable_and_runner_recovers(self):
        for name, body in {"usage not an object": ok_completion(usage="not-a-dict"),
                           "prompt_token_ids not ints": ok_completion(prompt_token_ids=["a", "b"]),
                           "finish_reason not a string": ok_completion(choices=[{"finish_reason": 7, "message": {
                               "content": "c"}, "token_ids": [5]}]),
                           "content is an object": ok_completion(choices=[{"finish_reason": "stop",
                               "message": {"content": {"text": "c"}}, "token_ids": [5]}]),
                           "content parts not objects": ok_completion(choices=[{"finish_reason": "stop",
                               "message": {"content": [1, 2]}, "token_ids": [5]}]),
                           }.items():
            with self.subTest(name):
                r = _gen(body)
                self.assertFalse(r.ok, name)
                self.assertTrue(r.error.retryable, name)
                self.assertEqual(r.error.type, "MalformedResponse", name)
                Record.model_validate_json(r.to_json())
        # config problems found before sending stay non-retryable
        r = _gen(ok_completion(), params={"model": "x"})
        self.assertFalse(r.ok); self.assertFalse(r.error.retryable)
        with tempfile.TemporaryDirectory() as d:
            hz = Harness(d, max_attempts=3)
            out = hz.go(router(completion=[ok_completion(usage="not-a-dict"), ok_completion(prompt_token_ids=["a"]),
                                            ok_completion()]))
            self.assertEqual(out["summary"]["n_ok"], 1)
            recs = hz.records()
            self.assertEqual([r["ok"] for r in recs], [False, False, True])
            self.assertTrue(all(r["error"]["retryable"] for r in recs[:2]))
            self.assertEqual(recs[0]["raw_response"]["usage"], "not-a-dict")   # the offending body is kept
            self.assertEqual(hz.go(router())["summary"]["n_runnable"], 0)


# ---------------------------------------------------------------- #8 one choke point, every payload

class TestRedactionChokePoint(unittest.TestCase):
    def _spec(self):
        return ModelSpec(id="o", backend="openrouter", model="m", family="f",
                         provider={"order": ["x"], "note": SECRET})

    def test_writer_redacts_every_record_field(self):
        """A Record holding the key in messages, params, effective_params, request_body, model.provider, error,
        raw_response and content never reaches the ledger unredacted."""
        msgs = [{"role": "system", "content": f"key {SECRET}"}, {"role": "user", "content": SECRET}]
        rec = Record(**META, timestamp="t", latency_s=0.0, ok=False, model=self._spec().info(),
                     params={"stop": [SECRET]}, effective_params={"stop": [SECRET], SECRET: 1},
                     messages=msgs, request_body={"model": "m", "messages": msgs, "stop": [SECRET]},
                     raw_response={"echo": SECRET}, content=SECRET, reasoning=SECRET,
                     error={"type": "x", "message": f"bad {SECRET}"})
        self.assertIn(SECRET, rec.to_json())                       # unredacted in memory ...
        self.assertNotIn(SECRET, rec.to_json([SECRET]))            # ... redacted when the writer passes secrets
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "records.jsonl"
            w = Writer(p, [SECRET]); self.assertTrue(w.write(rec)); w.close()
            text = p.read_text(encoding="utf-8")
            self.assertNotIn(SECRET, text)
            back = json.loads(text)
            self.assertEqual(back["messages"][1]["content"], "[REDACTED:api_key]")
            self.assertEqual(back["params"]["stop"], ["[REDACTED:api_key]"])
            self.assertEqual(back["model"]["provider"]["note"], "[REDACTED:api_key]")
            self.assertIn("[REDACTED:api_key]", json.dumps(back["effective_params"]))
            self.assertIn("[REDACTED:api_key]", back["request_body"]["messages"][0]["content"])
            # the mirror file goes through the same choke point
            w = Writer(p, [SECRET]); w.f.close()
            with self.assertLogs("hp.runner", level="ERROR"):
                self.assertFalse(w.write(rec))
            self.assertNotIn(SECRET, (Path(d) / "records.unwritten.jsonl").read_text(encoding="utf-8"))

    def test_manifest_invocations_and_log_are_redacted(self):
        """Config text (description) and argv carrying the key are redacted in manifest.json / invocations.jsonl."""
        os.environ["VLLM_API_KEY"] = SECRET
        try:
            with tempfile.TemporaryDirectory() as d:
                hz = Harness(d, extra_cfg={"description": f"run with {SECRET} in the description"})
                with mock.patch.object(__import__("sys"), "argv", ["hp", f"--token={SECRET}"]):
                    out = hz.go(router(completion=ok_completion(content=f"echo {SECRET}")))
                self.assertEqual(out["summary"]["n_ok"], 1)
                for name in ("manifest.json", "records.jsonl", "invocations.jsonl"):
                    self.assertNotIn(SECRET, (hz.run_dir / name).read_text(encoding="utf-8"), name)
                man = hz.manifest()
                self.assertIn("[REDACTED:api_key]", man["config"]["description"])
                self.assertIn("[REDACTED:api_key]", man["config_resolved"]["description"])
                self.assertIn("[REDACTED:api_key]", json.dumps(hz.invocations()[0]["argv"]))
                self.assertEqual(hz.records()[0]["content"], "echo [REDACTED:api_key]")
        finally:
            del os.environ["VLLM_API_KEY"]

    def test_key_in_prompt_or_request_body_is_refused_before_anything_is_written(self):
        """A rendered message / param / extra / provider containing the key could only be stored redacted, so its
        body hash would never validate on resume: refused up front (the Codex in-memory repro)."""
        os.environ["VLLM_API_KEY"] = SECRET
        try:
            with tempfile.TemporaryDirectory() as d:
                hz = Harness(d, packet=PACKET + f"\nauth: {SECRET}\n")
                with self.assertRaises(RunnerError) as cm:
                    hz.go(router())
                self.assertIn("API key", str(cm.exception)); self.assertIn(hz.items()[0][0].item_key, str(cm.exception))
                self.assertFalse(hz.run_dir.exists())
                hz = Harness(d, extra_cfg={"run_id": "t2"},
                             models=[{"id": "m1", "backend": "vllm", "url": "http://127.0.0.1:9/v1", "model": "served",
                                      "family": "Qwen3", "extra": {"user": SECRET}}])
                with self.assertRaises(RunnerError):
                    hz.go(router(), dry_run=True)
                self.assertFalse(hz.run_dir.exists())
        finally:
            del os.environ["VLLM_API_KEY"]

    def test_key_in_model_url_or_packet_provenance_is_refused_not_bricked(self):
        """A key in model.url (frozen item spec, not the body) would be written redacted and then refuse every
        resume ("items changed"). Refused up front instead; same for a packet's repo/branch."""
        os.environ["VLLM_API_KEY"] = SECRET
        try:
            with tempfile.TemporaryDirectory() as d:
                hz = Harness(d, models=[{"id": "m1", "backend": "vllm", "url": f"http://127.0.0.1:9/v1?k={SECRET}",
                                         "model": "served", "family": "Qwen3"}])
                with self.assertRaises(RunnerError) as cm:
                    hz.go(router())
                self.assertIn("model url", str(cm.exception)); self.assertFalse(hz.run_dir.exists())
                hz = Harness(d, extra_cfg={"run_id": "t2"})
                (hz.envs_dir / "e1" / "env.yaml").write_text(f"repo: cotmon\nbranch: {SECRET}\n", encoding="utf-8")
                with self.assertRaises(RunnerError) as cm:
                    hz.go(router())
                self.assertIn("API key", str(cm.exception)); self.assertFalse(hz.run_dir.exists())   # branch is in the task line too
                from hp.runner import check_secrets_in_requests
                with self.assertRaises(RunnerError) as cm:       # the packets block alone is checked as well
                    check_secrets_in_requests([], [SECRET], {"e1": {"repo": "x", "branch": SECRET}})
                self.assertIn("repo/branch", str(cm.exception))
        finally:
            del os.environ["VLLM_API_KEY"]

    def test_lock_file_is_redacted(self):
        os.environ["VLLM_API_KEY"] = SECRET
        try:
            with tempfile.TemporaryDirectory() as d:
                from hp.runner import acquire_lock, release_lock
                lock = acquire_lock(Path(d), argv=["hp", f"--token={SECRET}"], secrets=[SECRET])
                text = lock.read_text(encoding="utf-8")
                self.assertNotIn(SECRET, text); self.assertIn("[REDACTED:api_key]", text)
                release_lock(lock)
                # through run(): argv is taken from sys.argv and the configured key is scrubbed
                async def slow(req):
                    await asyncio.sleep(30)
                    return httpx.Response(200, json=ok_completion())
                h = router(completion=slow)
                hz = Harness(d)

                async def scenario():
                    with mock.patch.object(__import__("sys"), "argv", ["hp", f"--token={SECRET}"]):
                        t = asyncio.create_task(hz.go_async(h))
                        await asyncio.sleep(0.5)
                        lock_text = (hz.run_dir / LOCK_FILE).read_text(encoding="utf-8")
                        t.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await t
                    return lock_text
                lock_text = asyncio.run(scenario())
                self.assertNotIn(SECRET, lock_text); self.assertIn("[REDACTED:api_key]", lock_text)
        finally:
            del os.environ["VLLM_API_KEY"]

    def test_short_key_is_not_scrubbed(self):
        """A 1-char 'key' would corrupt every payload if scrubbed; it is ignored (not a secret)."""
        os.environ["VLLM_API_KEY"] = "a"
        try:
            spec = ModelSpec(id="q", backend="vllm", url="http://mock/v1", model="m", family="f")
            self.assertEqual(spec.secrets(), [])
            _, headers, _ = build_request(spec, [{"role": "user", "content": "u"}], {})
            self.assertEqual(headers["Authorization"], "Bearer a")   # still sent
        finally:
            del os.environ["VLLM_API_KEY"]


# ---------------------------------------------------------------- reserved sampling / provider fields

class TestReservedFields(unittest.TestCase):
    def test_sampling_fields_in_extra_and_provider_anywhere_are_refused(self):
        base = dict(id="q", backend="vllm", url="http://mock/v1", model="m", family="f")
        for extra in ({"temperature": 0.7}, {"top_p": 0.9}, {"top_k": 5}, {"min_p": 0.1}, {"max_tokens": 5},
                      {"seed": 1}, {"reasoning_effort": "low"}, {"reasoning": {"effort": "low"}}, {"stop": ["x"]},
                      {"provider": {"order": ["x"]}}, {"return_token_ids": True, "temperature": 0.7}):
            with self.subTest(extra=extra), self.assertRaises(ValueError) as cm:
                ModelSpec(**base, extra=extra)
            self.assertTrue("sampling" in str(cm.exception) or "provider" in str(cm.exception), str(cm.exception))
        with self.assertRaises(ValueError):
            ModelSpec(**base, params={"provider": {"order": ["x"]}})
        # the openrouter pin block goes through its own field and lands in the body exactly once
        os.environ["OPENROUTER_API_KEY"] = SECRET
        try:
            spec = ModelSpec(id="o", backend="openrouter", model="m", family="f", provider={"order": ["x"]},
                             extra={"return_token_ids": True})
            url, body = build_body(spec, [{"role": "user", "content": "u"}], {"temperature": 0.7, "seed": 3})
            self.assertEqual(body["provider"], {"order": ["x"]})
            # per call: params may not smuggle provider either
            r = asyncio.run(generate(spec, [{"role": "user", "content": "u"}], {"provider": {"order": ["y"]}}, meta=META))
            self.assertFalse(r.ok); self.assertFalse(r.error.retryable); self.assertIn("provider", r.error.message)
        finally:
            del os.environ["OPENROUTER_API_KEY"]

    def test_denylist_covers_routing_and_newer_sampling_names(self):
        base = dict(id="q", backend="vllm", url="http://mock/v1", model="m", family="f")
        for extra in ({"models": ["a", "b"]}, {"route": "fallback"}, {"top_a": 0.1}, {"structured_outputs": {"json": {}}},
                      {"stop_token_ids": [1]}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                ModelSpec(**base, extra=extra)
        for params in ({"models": ["a"]}, {"route": "x"}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                ModelSpec(**base, params=params)

    def test_backend_defaults_live_in_params_for_every_backend(self):
        """openrouter `reasoning: {enabled: true}` and anthropic `max_tokens: 32768` used to be injected into the
        body only; now build_items puts them in params, so params == sampling part of the body for all backends."""
        from hp.backends import default_params
        os.environ["OPENROUTER_API_KEY"] = SECRET; os.environ["ANTHROPIC_API_KEY"] = SECRET
        try:
            with tempfile.TemporaryDirectory() as d:
                models = [{"id": "o", "backend": "openrouter", "model": "x/y", "family": "f", "params": {"max_tokens": 5},
                           "provider": {"order": ["p"]}},
                          {"id": "a", "backend": "anthropic", "model": "claude", "family": "f", "params": {"temperature": 1.0}},
                          {"id": "v", "backend": "vllm", "url": "http://mock/v1", "model": "m", "family": "f"}]
                items, _ = Harness(d, models=models, extra_cfg={"seed_from_sample_idx": False}).items()
                by = {it.model_id: it for it in items}
                self.assertEqual(by["o"].params, {"reasoning": {"enabled": True}, "max_tokens": 5})
                self.assertEqual(by["a"].params, {"max_tokens": 32768, "temperature": 1.0})
                self.assertEqual(by["v"].params, {})
                for it in items:
                    body = it.request_body()
                    sampling = {k: v for k, v in body.items() if k not in ("model", "messages", "system", "provider")
                                and k not in it.spec.extra}
                    self.assertEqual(sampling, it.params, it.model_id)
                # a config value overrides the default (no double source of truth)
                models[0]["params"]["reasoning"] = {"effort": "low"}
                items, _ = Harness(d, models=models, extra_cfg={"seed_from_sample_idx": False, "run_id": "t2"}).items()
                self.assertEqual(next(i for i in items if i.model_id == "o").params["reasoning"], {"effort": "low"})
                self.assertEqual(default_params("openrouter"), {"reasoning": {"enabled": True}})
        finally:
            del os.environ["OPENROUTER_API_KEY"]; del os.environ["ANTHROPIC_API_KEY"]

    def test_record_params_equal_sampling_part_of_body(self):
        """With the reserved-field rules, body == {model, messages} + params + non-sampling extra, exactly."""
        with tempfile.TemporaryDirectory() as d:
            hz = Harness(d)
            hz.go(router())
            r = hz.records()[0]
            body = dict(r["request_body"]); body.pop("model"); body.pop("messages")
            self.assertEqual({k: v for k, v in body.items() if k != "return_token_ids"}, r["params"])
            # config with a sampling field in extra is refused at build time (RunnerError, exit 3 path)
            hz2 = Harness(d, extra_cfg={"run_id": "t2"},
                          models=[{"id": "m1", "backend": "vllm", "url": "http://127.0.0.1:9/v1", "model": "served",
                                   "family": "Qwen3", "params": {"max_tokens": 10}, "extra": {"temperature": 0.7}}])
            with self.assertRaises(RunnerError) as cm:
                hz2.items()
            self.assertIn("temperature", str(cm.exception))


# ---------------------------------------------------------------- partial trailing ledger line

class TestPartialTrailingLine(unittest.TestCase):
    def test_repair_trailing_newline(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "records.jsonl"
            self.assertIsNone(repair_trailing_newline(p))                      # absent
            p.write_text("", encoding="utf-8")
            self.assertIsNone(repair_trailing_newline(p))                      # empty
            p.write_text('{"a":1}\n{"b":2}\n', encoding="utf-8")
            self.assertIsNone(repair_trailing_newline(p)); self.assertEqual(p.read_text(), '{"a":1}\n{"b":2}\n')
            p.write_text('{"a":1}\n{"b":2}\n{"item_key":"c","ok":tr', encoding="utf-8")
            self.assertEqual(repair_trailing_newline(p), 3)
            self.assertTrue(p.read_bytes().endswith(b'"ok":tr\n'))
            self.assertIsNone(repair_trailing_newline(p))                      # idempotent

    def test_partial_line_does_not_swallow_the_next_record(self):
        """Crash mid-write -> fragment without newline. The next invocation's records must land on their own
        lines, be valid, and the item must not be re-issued again afterwards (Codex round 2, new bug 1)."""
        with tempfile.TemporaryDirectory() as d:
            hz = Harness(d, n_samples=2)
            hz.go(router(), limit=1)
            p = hz.run_dir / "records.jsonl"
            full = hz.records()[0]
            fragment = json.dumps(full)[:200]                 # a truncated copy of a real record, no newline
            with open(p, "a", encoding="utf-8") as f:
                f.write(fragment)
            self.assertFalse(p.read_bytes().endswith(b"\n"))
            out = hz.go(router())
            self.assertEqual(out["summary"]["n_ok"], 1)
            self.assertEqual(out["summary"]["ledger_repaired_line"], 2)
            self.assertEqual(out["invocation"]["ledger"]["bad"], [{"line": 2, "item_key": None,
                              "reason": mock.ANY}])
            self.assertIn("partial line", out["invocation"]["ledger"]["bad"][0]["reason"])
            lines = p.read_text(encoding="utf-8").split("\n")
            self.assertEqual(len(lines), 4); self.assertEqual(lines[1], fragment); self.assertEqual(lines[3], "")
            json.loads(lines[2])                                              # the new record is a clean line
            led = read_records(p, expected=hz.manifest()["items"], run_id="t", config_sha256=hz.sha)
            self.assertEqual(len(led.done), 2); self.assertEqual([b["line"] for b in led.bad], [2])
            self.assertEqual(hz.go(router())["summary"]["n_runnable"], 0)   # nothing re-issued


# ---------------------------------------------------------------- cancellation record carries the body

class TestCancellationBody(unittest.TestCase):
    def test_cancelled_record_has_the_posted_body_and_validates(self):
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
            hz = Harness(d)
            asyncio.run(scenario(hz))
            rec = hz.records()[0]
            self.assertEqual(rec["error"]["type"], "Cancelled")
            posted = h.state["calls"][0]
            self.assertEqual(rec["request_body"], posted)                 # exactly what went over the wire
            self.assertEqual(rec["request_body"]["messages"], rec["messages"])
            self.assertEqual(rec["request_body"]["temperature"], 0.6); self.assertTrue(rec["request_body"]["return_token_ids"])
            man = hz.manifest()
            self.assertIsNone(validate_record(rec, expected=man["items"], run_id="t", config_sha256=hz.sha))
            # failed_record without a body is still allowed (request never built), body {} is not "wrong"
            spec = hz.items()[0][0].spec
            r = failed_record(spec, [{"role": "user", "content": "u"}], {}, meta=META, exc_type="X", message="m",
                              retryable=True)
            self.assertEqual(r.request_body, {})


# ---------------------------------------------------------------- numeric controls

class TestNumericControls(unittest.TestCase):
    def test_config_ranges_refused_at_load_and_after_patching(self):
        with tempfile.TemporaryDirectory() as d:
            hz = Harness(d)
            base = hz.cfg_dict
            for change in ({"n_samples": 0}, {"max_attempts": 0}, {"backoff_s": -1}, {"n_samples": -3},
                           {"models": [dict(base["models"][0], concurrency=0)]},
                           {"models": [dict(base["models"][0], timeout_s=0)]}):
                with self.subTest(change=change), self.assertRaises(ValidationError):
                    RunConfig.model_validate({**base, **change})
            for attr, val in (("max_attempts", 0), ("n_samples", 0), ("backoff_s", -0.5)):
                cfg, _, _ = load_config(hz.cfg_path)
                setattr(cfg, attr, val)
                with self.subTest(attr=attr), self.assertRaises(RunnerError) as cm:
                    validate_config(cfg)
                self.assertIn(attr, str(cm.exception))
            cfg, _, _ = load_config(hz.cfg_path)
            cfg.models[0].concurrency = 0
            with self.assertRaises(RunnerError) as cm:
                build_items(cfg, REG)
            self.assertIn("concurrency", str(cm.exception))
            cfg, _, _ = load_config(hz.cfg_path)
            cfg.models[0].timeout_s = 0
            with self.assertRaises(RunnerError):
                validate_config(cfg)
            with self.assertRaises(ValueError):
                ModelSpec(id="q", backend="vllm", url="http://mock/v1", model="m", family="f", concurrency=0)

    def test_negative_limit_refused_in_run_and_cli(self):
        with tempfile.TemporaryDirectory() as d:
            hz = Harness(d)
            with self.assertRaises(RunnerError) as cm:
                hz.go(router(), limit=-1)
            self.assertIn("--limit", str(cm.exception))
            self.assertFalse(hz.run_dir.exists())
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = main([str(hz.cfg_path), "--limit", "-1", "--log-dir", str(Path(d) / "logs")])
            self.assertEqual(rc, 3); self.assertIn("--limit must be >= 0", err.getvalue())
            self.assertFalse((Path(d) / "logs").exists()); self.assertFalse(hz.run_dir.exists())
            self.assertEqual(hz.go(router(), limit=0)["summary"]["n_runnable"], 0)   # 0 is legal: run nothing
            logging.getLogger().handlers.clear(); logging.getLogger().addHandler(logging.NullHandler())


if __name__ == "__main__":
    unittest.main()
