commit a41c7e9
Author: Ines Valdez <i.valdez@internal>
Date:   2026-08-25 11:02:44 +0000

    backends: add client for the jobs v2 training API

    The training service is retiring POST /v1/sft/runs on 2026-10-31.
    v2 describes a job as a spec with separate optimizer, schedule and
    data sections.

    Adds sfttrain/backends/jobs_v2.py: spec dataclasses, local validation
    (so a bad config fails before we upload anything) and JobsClient.
    Nothing calls it yet; the launch path moves over in the next commit.

    SFT-412

diff --git a/sfttrain/backends/__init__.py b/sfttrain/backends/__init__.py
new file mode 100644
index 0000000..a192a87
--- /dev/null
+++ b/sfttrain/backends/__init__.py
@@ -0,0 +1 @@
+"""Clients for the training service APIs."""
diff --git a/sfttrain/backends/jobs_v2.py b/sfttrain/backends/jobs_v2.py
new file mode 100644
index 0000000..877a130
--- /dev/null
+++ b/sfttrain/backends/jobs_v2.py
@@ -0,0 +1,104 @@
+"""Client for the jobs v2 training API.
+
+v2 replaces POST /v1/sft/runs, which the training service will stop
+accepting on 2026-10-31. A job is described by a JobSpec with separate
+optimizer, schedule and data sections. The service validates the spec again
+on submit, but we validate locally first so a bad config fails before the
+dataset upload.
+
+Route: POST /v2/jobs, which returns {"job_id", "status", "submitted_at"}.
+"""
+from __future__ import annotations
+
+from dataclasses import asdict, dataclass, field
+
+LR_SCHEDULES = ("cosine", "linear", "constant")
+PRIORITIES = ("low", "normal", "high")
+
+
+class JobSpecError(ValueError):
+    pass
+
+
+@dataclass
+class OptimizerSpec:
+    lr: float
+    weight_decay: float = 0.0
+    name: str = "adamw"
+    betas: tuple[float, float] = (0.9, 0.95)
+    grad_clip: float = 1.0
+
+
+@dataclass
+class ScheduleSpec:
+    epochs: int
+    warmup_steps: int = 0
+    lr_schedule: str = "cosine"
+    min_lr_ratio: float = 0.1
+
+
+@dataclass
+class DataSpec:
+    train_uri: str
+    heldout_uri: str
+    seq_len: int
+    global_batch_size: int
+    packing: bool = True
+
+
+@dataclass
+class JobSpec:
+    name: str
+    checkpoint: str
+    optimizer: OptimizerSpec
+    schedule: ScheduleSpec
+    data: DataSpec
+    labels: dict[str, str] = field(default_factory=dict)
+    priority: str = "normal"
+
+    def validate(self) -> list[str]:
+        errs = []
+        if not self.name:
+            errs.append("name is empty")
+        if not self.checkpoint.startswith("ckpt://"):
+            errs.append(f"checkpoint must be a ckpt:// URI, got {self.checkpoint!r}")
+        if not 0 < self.optimizer.lr < 1:
+            errs.append(f"optimizer.lr out of range: {self.optimizer.lr}")
+        if self.optimizer.weight_decay < 0:
+            errs.append("optimizer.weight_decay must be >= 0")
+        if self.schedule.epochs < 1:
+            errs.append("schedule.epochs must be >= 1")
+        if self.schedule.lr_schedule not in LR_SCHEDULES:
+            errs.append(f"schedule.lr_schedule must be one of {LR_SCHEDULES}")
+        if self.data.global_batch_size < 1 or self.data.seq_len < 1:
+            errs.append("data.global_batch_size and data.seq_len must be positive")
+        if self.priority not in PRIORITIES:
+            errs.append(f"priority must be one of {PRIORITIES}")
+        return errs
+
+    def to_body(self) -> dict:
+        body = asdict(self)
+        body["optimizer"]["betas"] = list(self.optimizer.betas)
+        return body
+
+
+@dataclass
+class JobHandle:
+    job_id: str
+    status: str
+    submitted_at: str | None = None
+
+
+class JobsClient:
+    def __init__(self, transport, project: str = "align-sft"):
+        self.transport = transport
+        self.project = project
+
+    def submit(self, spec: JobSpec) -> JobHandle:
+        errs = spec.validate()
+        if errs:
+            raise JobSpecError(f"invalid job spec {spec.name!r}: " + "; ".join(errs))
+        body = {"project": self.project, "kind": "sft", "spec": spec.to_body()}
+        resp = self.transport.post("/v2/jobs", body)
+        return JobHandle(job_id=resp["job_id"], status=resp.get("status", "queued"),
+                         submitted_at=resp.get("submitted_at"))

commit 0d93b52
Author: Ines Valdez <i.valdez@internal>
Date:   2026-08-27 16:20:09 +0000

    launch: submit through jobs v2, deprecate the v1 trainer

    `sfttrain launch` now builds a JobSpec from the model config and the
    dataset manifest and submits it with JobsClient. Datasets are passed
    as file:// URIs, and the spec carries the data config name, template
    and example count as labels so runs can be traced back to a build.

    launch refuses a dataset that was built for a different model config
    (we had one run in July train kestrel-3-70b on the 8b build).

    Removes LegacyTrainer: nothing else used it, and every config we still
    run has been submitted through v2 in the shadow project. `train` is
    kept as an alias of `launch`.

    SFT-413

diff --git a/README.md b/README.md
index 1824df5..22ecd58 100644
--- a/README.md
+++ b/README.md
@@ -15,7 +15,8 @@ service.
 | `sfttrain/filters.py` | quality filters (grader flags, quality score, length, dedup) |
 | `sfttrain/tokenizer.py` | offline tokenizer used for length accounting |
 | `sfttrain/build.py` | builds `train.jsonl`, `heldout.jsonl` and `manifest.json` |
-| `sfttrain/trainer.py` | submits a run to the training service |
+| `sfttrain/trainer.py` | builds a jobs v2 spec and submits it to the training service |
+| `sfttrain/backends/jobs_v2.py` | client for the jobs v2 training API |
 | `configs/models/` | one TOML file per model |
 | `configs/data/` | data mixtures |
 
@@ -24,13 +25,16 @@ service.
 ```bash
 python -m sfttrain build --model configs/models/kestrel-3-8b.toml \
     --data configs/data/assistant_mix.toml --out out/kestrel-3-8b
-python -m sfttrain train --model configs/models/kestrel-3-8b.toml \
+python -m sfttrain launch --model configs/models/kestrel-3-8b.toml \
     --dataset out/kestrel-3-8b --dry-run
 ```
 
 `--dry-run` writes the request body to `<dataset>/requests/` instead of
 sending it. Real runs need `TRAIN_API_TOKEN` in the environment.
 
+`launch` submits through the jobs v2 API (the v1 route, `/v1/sft/runs`, is
+switched off on 2026-10-31). `train` is kept as an alias of `launch`.
+
 ## Data
 
 Input records come from the conversation export job (see `sfttrain/records.py`
diff --git a/sfttrain/cli.py b/sfttrain/cli.py
index 128ff68..2b504c0 100644
--- a/sfttrain/cli.py
+++ b/sfttrain/cli.py
@@ -1,18 +1,27 @@
-"""Command line entry point: python -m sfttrain {build,train} ..."""
+"""Command line entry point: python -m sfttrain {build,launch} ..."""
 from __future__ import annotations
 
 import argparse
 import json
 import sys
+from dataclasses import asdict
 
+from .backends.jobs_v2 import JobsClient
 from .build import build_dataset
 from .config import load_data_config, load_model_config
-from .trainer import LegacyTrainer
+from .trainer import launch
 from .transport import DryRunTransport, HttpTransport
 
 
+def _transport(args, dataset_dir: str):
+    if args.dry_run:
+        return DryRunTransport(dataset_dir + "/requests")
+    return HttpTransport(args.endpoint)
+
+
 def main(argv: list[str] | None = None) -> int:
     ap = argparse.ArgumentParser(prog="sfttrain")
+    ap.add_argument("--endpoint", default="https://train.internal/api")
     sub = ap.add_subparsers(dest="cmd", required=True)
 
     b = sub.add_parser("build", help="build the SFT dataset")
@@ -20,9 +29,10 @@ def main(argv: list[str] | None = None) -> int:
     b.add_argument("--data", required=True, help="configs/data/<name>.toml")
     b.add_argument("--out", required=True)
 
-    t = sub.add_parser("train", help="submit a training run for a built dataset")
+    t = sub.add_parser("launch", aliases=["train"], help="submit a training job for a built dataset")
     t.add_argument("--model", required=True)
     t.add_argument("--dataset", required=True, help="output dir of `build`")
+    t.add_argument("--project", default="align-sft")
     t.add_argument("--dry-run", action="store_true")
 
     args = ap.parse_args(argv)
@@ -31,8 +41,9 @@ def main(argv: list[str] | None = None) -> int:
         manifest = build_dataset(model_cfg, load_data_config(args.data), args.out)
         print(json.dumps(manifest, indent=2))
         return 0
-    transport = DryRunTransport(args.dataset + "/requests") if args.dry_run else HttpTransport()
-    print(json.dumps(LegacyTrainer(model_cfg, transport).train(args.dataset), indent=2))
+    transport = _transport(args, args.dataset)
+    handle = launch(model_cfg, args.dataset, JobsClient(transport, project=args.project))
+    print(json.dumps(asdict(handle), indent=2))
     return 0
 
 
diff --git a/sfttrain/trainer.py b/sfttrain/trainer.py
index a7cba29..9d852b3 100644
--- a/sfttrain/trainer.py
+++ b/sfttrain/trainer.py
@@ -1,32 +1,43 @@
-"""Submit an SFT run to the training service (v1 API)."""
+"""Submit an SFT run to the training service through the jobs v2 API."""
 from __future__ import annotations
 
+import json
 from pathlib import Path
 
+from .backends.jobs_v2 import DataSpec, JobHandle, JobsClient, JobSpec, OptimizerSpec, ScheduleSpec
 from .config import ModelConfig
 
 
-class LegacyTrainer:
-    """Thin wrapper over POST /v1/sft/runs."""
-
-    def __init__(self, cfg: ModelConfig, transport):
-        self.cfg = cfg
-        self.transport = transport
-
-    def request_body(self, dataset_dir: str | Path) -> dict:
-        d = Path(dataset_dir)
-        return {
-            "run_name": self.cfg.name,
-            "checkpoint": self.cfg.base_checkpoint,
-            "train_file": str(d / "train.jsonl"),
-            "valid_file": str(d / "heldout.jsonl"),
-            "lr": self.cfg.learning_rate,
-            "epochs": self.cfg.num_epochs,
-            "batch_size": self.cfg.batch_size,
-            "seq_len": self.cfg.max_seq_len,
-            "weight_decay": self.cfg.weight_decay,
-            "warmup_steps": self.cfg.warmup_steps,
-        }
-
-    def train(self, dataset_dir: str | Path) -> dict:
-        return self.transport.post("/v1/sft/runs", self.request_body(dataset_dir))
+def dataset_uri(dataset_dir: str | Path, name: str) -> str:
+    return (Path(dataset_dir).resolve() / name).as_uri()
+
+
+def read_manifest(dataset_dir: str | Path) -> dict:
+    p = Path(dataset_dir) / "manifest.json"
+    if not p.exists():
+        raise FileNotFoundError(f"{p} not found; run `sfttrain build` first")
+    return json.loads(p.read_text(encoding="utf-8"))
+
+
+def spec_from_config(cfg: ModelConfig, dataset_dir: str | Path, manifest: dict | None = None) -> JobSpec:
+    manifest = manifest if manifest is not None else read_manifest(dataset_dir)
+    if manifest.get("model") != cfg.name:
+        raise ValueError(f"dataset was built for {manifest.get('model')!r}, not {cfg.name!r}")
+    return JobSpec(
+        name=cfg.name,
+        checkpoint=cfg.base_checkpoint,
+        optimizer=OptimizerSpec(lr=cfg.learning_rate, weight_decay=cfg.weight_decay),
+        schedule=ScheduleSpec(epochs=cfg.num_epochs, warmup_steps=cfg.warmup_steps),
+        data=DataSpec(
+            train_uri=dataset_uri(dataset_dir, "train.jsonl"),
+            heldout_uri=dataset_uri(dataset_dir, "heldout.jsonl"),
+            seq_len=cfg.max_seq_len,
+            global_batch_size=cfg.batch_size,
+        ),
+        labels={"data": str(manifest.get("data", "")), "template": cfg.template,
+                "n_train": str(manifest.get("n_train", ""))},
+    )
+
+
+def launch(cfg: ModelConfig, dataset_dir: str | Path, client: JobsClient) -> JobHandle:
+    return client.submit(spec_from_config(cfg, dataset_dir))
diff --git a/tests/test_trainer.py b/tests/test_trainer.py
new file mode 100644
index 0000000..ae9bfc4
--- /dev/null
+++ b/tests/test_trainer.py
@@ -0,0 +1,46 @@
+import json
+import tempfile
+import unittest
+from pathlib import Path
+
+from sfttrain.backends.jobs_v2 import JobsClient
+from sfttrain.config import CONFIG_ROOT, load_model_config
+from sfttrain.trainer import launch, spec_from_config
+from sfttrain.transport import DryRunTransport
+
+
+def write_manifest(d, model):
+    Path(d, "manifest.json").write_text(json.dumps({"model": model, "data": "unit", "n_train": 3}))
+
+
+class TrainerTests(unittest.TestCase):
+    def setUp(self):
+        self.cfg = load_model_config(CONFIG_ROOT / "models" / "kestrel-3-8b.toml")
+
+    def test_spec_maps_config_fields(self):
+        with tempfile.TemporaryDirectory() as d:
+            write_manifest(d, self.cfg.name)
+            spec = spec_from_config(self.cfg, d)
+            self.assertEqual(spec.optimizer.lr, self.cfg.learning_rate)
+            self.assertEqual(spec.schedule.epochs, self.cfg.num_epochs)
+            self.assertEqual(spec.data.global_batch_size, self.cfg.batch_size)
+            self.assertTrue(spec.data.train_uri.startswith("file://"))
+            self.assertEqual(spec.labels["data"], "unit")
+
+    def test_dataset_for_other_model_is_rejected(self):
+        with tempfile.TemporaryDirectory() as d:
+            write_manifest(d, "some-other-model")
+            with self.assertRaises(ValueError):
+                spec_from_config(self.cfg, d)
+
+    def test_launch_dry_run(self):
+        with tempfile.TemporaryDirectory() as d:
+            write_manifest(d, self.cfg.name)
+            t = DryRunTransport(Path(d) / "requests")
+            handle = launch(self.cfg, d, JobsClient(t))
+            self.assertEqual(handle.status, "dry_run")
+            self.assertTrue((Path(d) / "requests" / "v2_jobs.json").exists())
+
+
+if __name__ == "__main__":
+    unittest.main()

commit 7be2f10
Author: Rahul Iyer <r.iyer@internal>
Date:   2026-08-29 10:47:31 +0000

    config: nested optimizer/schedule tables to match jobs v2

    Model configs now mirror the v2 spec: [model] keeps name, checkpoint,
    batch size, sequence length and template, and the optimizer and
    schedule settings move to [model.optimizer] and [model.schedule]. This
    also exposes betas, grad_clip, lr_schedule and min_lr_ratio, which v2
    accepts and which were hard-coded on the service side before.

    kestrel-3-8b and kestrel-3-70b are converted. kestrel-2-8b stays in
    the flat layout because the 2025 baselines are reproduced from that
    exact file; flat configs are converted on load by migrate_legacy().
    Unknown keys are now an error instead of being silently ignored.

    SFT-415

diff --git a/configs/models/kestrel-2-8b.toml b/configs/models/kestrel-2-8b.toml
index 4169699..2a17b65 100644
--- a/configs/models/kestrel-2-8b.toml
+++ b/configs/models/kestrel-2-8b.toml
@@ -1,4 +1,6 @@
 # Kestrel 2 8B assistant SFT. Kept for re-running the 2025 baselines.
+# Still in the flat pre-v2 layout on purpose: the 2025 runs are reproduced from
+# this exact file, and load_model_config() migrates it on load.
 [model]
 name = "kestrel-2-8b-sft"
 base_checkpoint = "ckpt://pretrain/kestrel-2-8b/step-400000"
diff --git a/configs/models/kestrel-3-70b.toml b/configs/models/kestrel-3-70b.toml
index bd40501..4f46d14 100644
--- a/configs/models/kestrel-3-70b.toml
+++ b/configs/models/kestrel-3-70b.toml
@@ -2,10 +2,15 @@
 [model]
 name = "kestrel-3-70b-sft"
 base_checkpoint = "ckpt://pretrain/kestrel-3-70b/step-320000"
-learning_rate = 8e-6
-num_epochs = 2
-batch_size = 256
+global_batch_size = 256
 max_seq_len = 8192
+template = "chat_v2"
+
+[model.optimizer]
+lr = 8e-6
 weight_decay = 0.1
+
+[model.schedule]
+epochs = 2
 warmup_steps = 400
-template = "chat_v2"
+lr_schedule = "cosine"
diff --git a/configs/models/kestrel-3-8b.toml b/configs/models/kestrel-3-8b.toml
index eeb7f30..31bcdeb 100644
--- a/configs/models/kestrel-3-8b.toml
+++ b/configs/models/kestrel-3-8b.toml
@@ -2,10 +2,15 @@
 [model]
 name = "kestrel-3-8b-sft"
 base_checkpoint = "ckpt://pretrain/kestrel-3-8b/step-480000"
-learning_rate = 2e-5
-num_epochs = 2
-batch_size = 128
+global_batch_size = 128
 max_seq_len = 8192
+template = "chat_v2"
+
+[model.optimizer]
+lr = 2e-5
 weight_decay = 0.05
+
+[model.schedule]
+epochs = 2
 warmup_steps = 200
-template = "chat_v2"
+lr_schedule = "cosine"
diff --git a/sfttrain/config.py b/sfttrain/config.py
index 970411a..b239abb 100644
--- a/sfttrain/config.py
+++ b/sfttrain/config.py
@@ -2,9 +2,14 @@
 
 Configs are TOML files under configs/models/ and configs/data/. Paths inside a
 data config are resolved relative to the repository root.
+
+Model configs use the nested layout of the jobs v2 API ([model],
+[model.optimizer], [model.schedule]). Flat configs from before the v2
+migration are still accepted and converted by migrate_legacy().
 """
 from __future__ import annotations
 
+import logging
 import tomllib
 from dataclasses import dataclass, field
 from pathlib import Path
@@ -12,20 +17,47 @@ from pathlib import Path
 REPO_ROOT = Path(__file__).resolve().parent.parent
 CONFIG_ROOT = REPO_ROOT / "configs"
 
+log = logging.getLogger(__name__)
+
+
+@dataclass
+class OptimizerConfig:
+    lr: float = 1e-5
+    weight_decay: float = 0.0
+    betas: tuple[float, float] = (0.9, 0.95)
+    grad_clip: float = 1.0
+
+
+@dataclass
+class ScheduleConfig:
+    epochs: int = 2
+    warmup_steps: int = 100
+    lr_schedule: str = "cosine"
+    min_lr_ratio: float = 0.1
+
 
 @dataclass
 class ModelConfig:
     name: str
     base_checkpoint: str
-    learning_rate: float = 1e-5
-    num_epochs: int = 2
-    batch_size: int = 64
+    optimizer: OptimizerConfig = field(default_factory=OptimizerConfig)
+    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
+    global_batch_size: int = 64
     max_seq_len: int = 8192
-    weight_decay: float = 0.0
-    warmup_steps: int = 100
     template: str = "chat_v2"
 
 
+# Flat (pre-v2) key -> (section, key) in the nested layout. Section None means
+# the key stays on [model].
+LEGACY_KEYS = {
+    "learning_rate": ("optimizer", "lr"),
+    "num_epochs": ("schedule", "epochs"),
+    "warmup_steps": ("schedule", "warmup_steps"),
+    "batch_size": (None, "global_batch_size"),
+}
+TOP_LEVEL_KEYS = ("name", "base_checkpoint", "max_seq_len", "template")
+
+
 @dataclass
 class SourceConfig:
     name: str
@@ -49,12 +81,43 @@ def _read_toml(path: str | Path) -> dict:
         return tomllib.load(f)
 
 
-def load_model_config(path: str | Path) -> ModelConfig:
-    raw = _read_toml(path)
-    model = raw.get("model", {})
+def is_legacy(model: dict) -> bool:
+    return not isinstance(model.get("optimizer"), dict) and any(k in model for k in LEGACY_KEYS)
+
+
+def migrate_legacy(model: dict) -> dict:
+    """Convert a flat pre-v2 [model] table to the nested layout."""
+    out: dict = {k: model[k] for k in TOP_LEVEL_KEYS if k in model}
+    out["optimizer"], out["schedule"] = {}, {}
+    for old, (section, new) in LEGACY_KEYS.items():
+        if old not in model:
+            continue
+        if section is None:
+            out[new] = model[old]
+        else:
+            out[section][new] = model[old]
+    return out
+
+
+def model_config_from_dict(model: dict, source: str = "<dict>") -> ModelConfig:
     if "name" not in model or "base_checkpoint" not in model:
-        raise ValueError(f"{path}: [model] needs name and base_checkpoint")
-    return ModelConfig(**model)
+        raise ValueError(f"{source}: [model] needs name and base_checkpoint")
+    if is_legacy(model):
+        log.info("%s: flat model config, migrating to the nested layout", source)
+        model = migrate_legacy(model)
+    model = dict(model)
+    opt = dict(model.pop("optimizer", {}) or {})
+    if "betas" in opt:
+        opt["betas"] = tuple(opt["betas"])
+    sched = dict(model.pop("schedule", {}) or {})
+    try:
+        return ModelConfig(optimizer=OptimizerConfig(**opt), schedule=ScheduleConfig(**sched), **model)
+    except TypeError as e:
+        raise ValueError(f"{source}: {e}") from None
+
+
+def load_model_config(path: str | Path) -> ModelConfig:
+    return model_config_from_dict(_read_toml(path).get("model", {}), str(path))
 
 
 def load_data_config(path: str | Path) -> DataConfig:
diff --git a/sfttrain/trainer.py b/sfttrain/trainer.py
index 9d852b3..6ea5e15 100644
--- a/sfttrain/trainer.py
+++ b/sfttrain/trainer.py
@@ -26,13 +26,15 @@ def spec_from_config(cfg: ModelConfig, dataset_dir: str | Path, manifest: dict |
     return JobSpec(
         name=cfg.name,
         checkpoint=cfg.base_checkpoint,
-        optimizer=OptimizerSpec(lr=cfg.learning_rate, weight_decay=cfg.weight_decay),
-        schedule=ScheduleSpec(epochs=cfg.num_epochs, warmup_steps=cfg.warmup_steps),
+        optimizer=OptimizerSpec(lr=cfg.optimizer.lr, weight_decay=cfg.optimizer.weight_decay,
+                                betas=cfg.optimizer.betas, grad_clip=cfg.optimizer.grad_clip),
+        schedule=ScheduleSpec(epochs=cfg.schedule.epochs, warmup_steps=cfg.schedule.warmup_steps,
+                              lr_schedule=cfg.schedule.lr_schedule, min_lr_ratio=cfg.schedule.min_lr_ratio),
         data=DataSpec(
             train_uri=dataset_uri(dataset_dir, "train.jsonl"),
             heldout_uri=dataset_uri(dataset_dir, "heldout.jsonl"),
             seq_len=cfg.max_seq_len,
-            global_batch_size=cfg.batch_size,
+            global_batch_size=cfg.global_batch_size,
         ),
         labels={"data": str(manifest.get("data", "")), "template": cfg.template,
                 "n_train": str(manifest.get("n_train", ""))},
diff --git a/tests/test_config.py b/tests/test_config.py
index 58ca545..ec7b373 100644
--- a/tests/test_config.py
+++ b/tests/test_config.py
@@ -1,6 +1,6 @@
 import unittest
 
-from sfttrain.config import CONFIG_ROOT, load_data_config, load_model_config
+from sfttrain.config import CONFIG_ROOT, load_data_config, load_model_config, migrate_legacy, model_config_from_dict
 
 
 class ConfigTests(unittest.TestCase):
@@ -10,7 +10,30 @@ class ConfigTests(unittest.TestCase):
         for p in paths:
             cfg = load_model_config(p)
             self.assertTrue(cfg.name.endswith("-sft"), p)
-            self.assertGreater(cfg.learning_rate, 0)
+            self.assertGreater(cfg.optimizer.lr, 0)
+            self.assertGreaterEqual(cfg.schedule.epochs, 1)
+
+    def test_nested_config(self):
+        cfg = load_model_config(CONFIG_ROOT / "models" / "kestrel-3-70b.toml")
+        self.assertEqual(cfg.optimizer.lr, 8e-6)
+        self.assertEqual(cfg.optimizer.weight_decay, 0.1)
+        self.assertEqual(cfg.schedule.warmup_steps, 400)
+        self.assertEqual(cfg.global_batch_size, 256)
+
+    def test_migrate_legacy_moves_keys(self):
+        flat = {"name": "x-sft", "base_checkpoint": "ckpt://x", "learning_rate": 3e-5,
+                "num_epochs": 4, "batch_size": 32, "warmup_steps": 50, "template": "chat_v2"}
+        nested = migrate_legacy(flat)
+        self.assertEqual(nested["optimizer"]["lr"], 3e-5)
+        self.assertEqual(nested["schedule"], {"epochs": 4, "warmup_steps": 50})
+        self.assertEqual(nested["global_batch_size"], 32)
+        cfg = model_config_from_dict(flat)
+        self.assertEqual(cfg.schedule.epochs, 4)
+
+    def test_unknown_key_is_an_error(self):
+        with self.assertRaises(ValueError):
+            model_config_from_dict({"name": "x", "base_checkpoint": "ckpt://x", "optimizer": {},
+                                    "learning_rat": 1e-5})
 
     def test_data_config(self):
         cfg = load_data_config(CONFIG_ROOT / "data" / "assistant_mix.toml")
diff --git a/tests/test_trainer.py b/tests/test_trainer.py
index ae9bfc4..da577ff 100644
--- a/tests/test_trainer.py
+++ b/tests/test_trainer.py
@@ -21,9 +21,10 @@ class TrainerTests(unittest.TestCase):
         with tempfile.TemporaryDirectory() as d:
             write_manifest(d, self.cfg.name)
             spec = spec_from_config(self.cfg, d)
-            self.assertEqual(spec.optimizer.lr, self.cfg.learning_rate)
-            self.assertEqual(spec.schedule.epochs, self.cfg.num_epochs)
-            self.assertEqual(spec.data.global_batch_size, self.cfg.batch_size)
+            self.assertEqual(spec.optimizer.lr, self.cfg.optimizer.lr)
+            self.assertEqual(spec.optimizer.weight_decay, self.cfg.optimizer.weight_decay)
+            self.assertEqual(spec.schedule.epochs, self.cfg.schedule.epochs)
+            self.assertEqual(spec.data.global_batch_size, self.cfg.global_batch_size)
             self.assertTrue(spec.data.train_uri.startswith("file://"))
             self.assertEqual(spec.labels["data"], "unit")
 

commit c58a0d4
Author: Hannah Lindqvist <h.lindqvist@internal>
Date:   2026-09-01 14:05:12 +0000

    thoughts: train on raw model reasoning instead of summaries

    Exports have carried the raw reasoning next to the summary since
    08-04. The summaries come from a separate summariser and often
    describe steps the model did not take (31 of 200 sampled records).
    Training on them teaches a thinking format the model does not use at
    inference time.

    Adds thought_source to the data config (raw | summary | none, default
    raw). raw falls back to the summary for older records. The raw text
    is not rewritten: we strip whitespace, normalise line endings and
    remove the export's chunk markers. The manifest records how many
    examples got raw reasoning, a summary or nothing.

    SFT-421

diff --git a/configs/data/assistant_mix.toml b/configs/data/assistant_mix.toml
index a62ad3a..a196351 100644
--- a/configs/data/assistant_mix.toml
+++ b/configs/data/assistant_mix.toml
@@ -5,6 +5,8 @@ heldout_fraction = 0.1
 seed = 1234
 min_quality = 0.5
 dedup = true
+# raw | summary | none. See sfttrain/thoughts.py.
+thought_source = "raw"
 
 [[sources]]
 name = "assistant_demos"
diff --git a/sfttrain/build.py b/sfttrain/build.py
index bd6a489..8c3a546 100644
--- a/sfttrain/build.py
+++ b/sfttrain/build.py
@@ -3,6 +3,7 @@ from __future__ import annotations
 
 import json
 import time
+from collections import Counter
 from pathlib import Path
 
 from .config import DataConfig, ModelConfig
@@ -19,7 +20,8 @@ def build_dataset(model_cfg: ModelConfig, data_cfg: DataConfig, out_dir: str | P
     t0 = time.time()
     tokenizer = LocalTokenizer()
 
-    examples = build_mixture(data_cfg)
+    thought_stats: Counter = Counter()
+    examples = build_mixture(data_cfg, thought_stats)
     for ex in examples:
         ex.prompt_text, ex.target_text = render(ex, model_cfg.template)
 
@@ -33,6 +35,8 @@ def build_dataset(model_cfg: ModelConfig, data_cfg: DataConfig, out_dir: str | P
         "model": model_cfg.name,
         "data": data_cfg.name,
         "template": model_cfg.template,
+        "thought_source": data_cfg.thought_source,
+        "thought_origin": dict(thought_stats),
         "n_total": len(examples),
         "n_train": n_train,
         "n_heldout": n_heldout,
diff --git a/sfttrain/config.py b/sfttrain/config.py
index b239abb..d98c8d2 100644
--- a/sfttrain/config.py
+++ b/sfttrain/config.py
@@ -74,6 +74,7 @@ class DataConfig:
     seed: int = 1234
     min_quality: float = 0.5
     dedup: bool = True
+    thought_source: str = "raw"
 
 
 def _read_toml(path: str | Path) -> dict:
@@ -129,6 +130,8 @@ def load_data_config(path: str | Path) -> DataConfig:
     for s in sources:
         if s.weight <= 0:
             raise ValueError(f"{path}: source {s.name} has non-positive weight {s.weight}")
+    if data.get("thought_source", "raw") not in ("raw", "summary", "none"):
+        raise ValueError(f"{path}: unknown thought_source {data['thought_source']!r}")
     return DataConfig(sources=sources, **data)
 
 
diff --git a/sfttrain/mixture.py b/sfttrain/mixture.py
index 7659476..761f92e 100644
--- a/sfttrain/mixture.py
+++ b/sfttrain/mixture.py
@@ -7,17 +7,19 @@ records, weights below 1 subsample them (deterministically, from the seed).
 from __future__ import annotations
 
 import random
+from collections import Counter
 
 from .config import DataConfig, SourceConfig, resolve_path
 from .records import Example, example_from_record, iter_jsonl
-from .thoughts import select_thought
+from .thoughts import ThoughtSource, select_thought
 
 
-def load_source(src: SourceConfig) -> list[Example]:
+def load_source(src: SourceConfig, thought_source: ThoughtSource = ThoughtSource.RAW,
+                thought_stats: Counter | None = None) -> list[Example]:
     out: list[Example] = []
     for rec in iter_jsonl(resolve_path(src.path)):
         ex = example_from_record(rec, src.name)
-        ex.thought = select_thought(rec)
+        ex.thought = select_thought(rec, thought_source, thought_stats)
         out.append(ex)
         if src.max_examples is not None and len(out) >= src.max_examples:
             break
@@ -32,11 +34,12 @@ def weighted(examples: list[Example], weight: float, rng: random.Random) -> list
     return out
 
 
-def build_mixture(cfg: DataConfig) -> list[Example]:
+def build_mixture(cfg: DataConfig, thought_stats: Counter | None = None) -> list[Example]:
     rng = random.Random(cfg.seed)
+    source = ThoughtSource.parse(cfg.thought_source)
     mixture: list[Example] = []
     for src in cfg.sources:
-        mixture.extend(weighted(load_source(src), src.weight, rng))
+        mixture.extend(weighted(load_source(src, source, thought_stats), src.weight, rng))
     rng.shuffle(mixture)
     return mixture
 
diff --git a/sfttrain/thoughts.py b/sfttrain/thoughts.py
index 48f2377..fa8b7ac 100644
--- a/sfttrain/thoughts.py
+++ b/sfttrain/thoughts.py
@@ -1,14 +1,55 @@
 """Which reasoning text goes into the training target.
 
-The export job attaches a short reasoning summary to each response. We train
-the model to produce the summary before its answer.
+Since the 08-04 export the conversation records carry the model's raw
+reasoning (`reasoning`) next to the short summary (`reasoning_summary`).
+The data config picks one with `thought_source`:
+
+    raw      raw reasoning, falling back to the summary for older records
+    summary  the summary only (the behaviour before this option existed)
+    none     no reasoning in the target
+
+The chosen text is cleaned up but not rewritten: we only strip surrounding
+whitespace, normalise line endings and drop the export job's chunk markers.
 """
 from __future__ import annotations
 
+import re
+from collections import Counter
+from enum import Enum
+
+# The export job splits long reasoning into chunks joined with this marker.
+_CHUNK_MARKER = re.compile(r"\n?<\|reasoning_chunk\|>\n?")
+
+
+class ThoughtSource(str, Enum):
+    RAW = "raw"
+    SUMMARY = "summary"
+    NONE = "none"
+
+    @classmethod
+    def parse(cls, value: str) -> "ThoughtSource":
+        try:
+            return cls(value)
+        except ValueError:
+            raise ValueError(f"thought_source must be one of {[s.value for s in cls]}, got {value!r}") from None
+
+
+def clean_reasoning(text: str) -> str:
+    text = text.replace("\r\n", "\n")
+    text = _CHUNK_MARKER.sub("\n", text)
+    return text.strip()
+
 
-def select_thought(rec: dict) -> str | None:
-    summary = rec.get("reasoning_summary")
-    if not summary:
-        return None
-    summary = summary.strip()
-    return summary or None
+def select_thought(rec: dict, source: ThoughtSource = ThoughtSource.RAW,
+                   stats: Counter | None = None) -> str | None:
+    """Return the reasoning text for `rec`, or None. Counts the origin in `stats`."""
+    origin, text = "none", None
+    if source is ThoughtSource.RAW and rec.get("reasoning"):
+        origin, text = "raw", clean_reasoning(rec["reasoning"])
+    if not text and source in (ThoughtSource.RAW, ThoughtSource.SUMMARY) and rec.get("reasoning_summary"):
+        origin, text = "summary", rec["reasoning_summary"].strip()
+    if not text:
+        origin, text = "none", None
+    if stats is not None:
+        stats[origin] += 1
+    return text
diff --git a/tests/test_thoughts.py b/tests/test_thoughts.py
new file mode 100644
index 0000000..365d850
--- /dev/null
+++ b/tests/test_thoughts.py
@@ -0,0 +1,31 @@
+import unittest
+from collections import Counter
+
+from sfttrain.thoughts import ThoughtSource, select_thought
+
+REC = {"reasoning": "  step one\r\n<|reasoning_chunk|>\nstep two  ", "reasoning_summary": "summary"}
+
+
+class ThoughtTests(unittest.TestCase):
+    def test_raw_is_preferred(self):
+        self.assertEqual(select_thought(REC), "step one\nstep two")
+
+    def test_raw_falls_back_to_summary(self):
+        stats = Counter()
+        self.assertEqual(select_thought({"reasoning_summary": "s"}, ThoughtSource.RAW, stats), "s")
+        self.assertEqual(stats, {"summary": 1})
+
+    def test_summary_mode_ignores_raw(self):
+        self.assertEqual(select_thought(REC, ThoughtSource.SUMMARY), "summary")
+
+    def test_none_mode(self):
+        stats = Counter()
+        self.assertIsNone(select_thought(REC, ThoughtSource.NONE, stats))
+        self.assertEqual(stats, {"none": 1})
+
+    def test_blank_raw_counts_as_missing(self):
+        self.assertEqual(select_thought({"reasoning": " \n ", "reasoning_summary": "s"}), "s")
+
+
+if __name__ == "__main__":
+    unittest.main()

commit 93e1b6f
Author: Hannah Lindqvist <h.lindqvist@internal>
Date:   2026-09-03 09:38:57 +0000

    templates: chat_v3 with a <think> block, cap reasoning length

    chat_v3 renders the reasoning in a <think> block before the answer,
    which is what the kestrel-3 models emit at inference time. chat_v2
    (<summary> block) stays for the kestrel-2 baselines.

    Raw reasoning is much longer than the summaries (p50 1.9k tokens, p99
    21k on the 08-30 export), so it is cut to max_thought_tokens from the
    model config (default 6144). We keep the end of the reasoning, where
    the model settles on its answer. The manifest counts truncated
    examples.

    kestrel-3-8b and kestrel-3-70b move to chat_v3.

    SFT-422

diff --git a/configs/models/kestrel-3-70b.toml b/configs/models/kestrel-3-70b.toml
index 4f46d14..5c7cc43 100644
--- a/configs/models/kestrel-3-70b.toml
+++ b/configs/models/kestrel-3-70b.toml
@@ -4,7 +4,8 @@ name = "kestrel-3-70b-sft"
 base_checkpoint = "ckpt://pretrain/kestrel-3-70b/step-320000"
 global_batch_size = 256
 max_seq_len = 8192
-template = "chat_v2"
+template = "chat_v3"
+max_thought_tokens = 6144
 
 [model.optimizer]
 lr = 8e-6
diff --git a/configs/models/kestrel-3-8b.toml b/configs/models/kestrel-3-8b.toml
index 31bcdeb..d7741c3 100644
--- a/configs/models/kestrel-3-8b.toml
+++ b/configs/models/kestrel-3-8b.toml
@@ -4,7 +4,8 @@ name = "kestrel-3-8b-sft"
 base_checkpoint = "ckpt://pretrain/kestrel-3-8b/step-480000"
 global_batch_size = 128
 max_seq_len = 8192
-template = "chat_v2"
+template = "chat_v3"
+max_thought_tokens = 4096
 
 [model.optimizer]
 lr = 2e-5
diff --git a/sfttrain/build.py b/sfttrain/build.py
index 8c3a546..66df428 100644
--- a/sfttrain/build.py
+++ b/sfttrain/build.py
@@ -23,7 +23,7 @@ def build_dataset(model_cfg: ModelConfig, data_cfg: DataConfig, out_dir: str | P
     thought_stats: Counter = Counter()
     examples = build_mixture(data_cfg, thought_stats)
     for ex in examples:
-        ex.prompt_text, ex.target_text = render(ex, model_cfg.template)
+        ex.prompt_text, ex.target_text = render(ex, model_cfg.template, tokenizer, model_cfg.max_thought_tokens)
 
     filters = default_filters(model_cfg.max_seq_len, data_cfg.min_quality, data_cfg.dedup, tokenizer)
     kept, dropped = apply_filters(examples, filters)
@@ -37,6 +37,7 @@ def build_dataset(model_cfg: ModelConfig, data_cfg: DataConfig, out_dir: str | P
         "template": model_cfg.template,
         "thought_source": data_cfg.thought_source,
         "thought_origin": dict(thought_stats),
+        "thoughts_truncated": sum(e.thought_truncated for e in examples),
         "n_total": len(examples),
         "n_train": n_train,
         "n_heldout": n_heldout,
diff --git a/sfttrain/config.py b/sfttrain/config.py
index d98c8d2..2acf98c 100644
--- a/sfttrain/config.py
+++ b/sfttrain/config.py
@@ -44,7 +44,8 @@ class ModelConfig:
     schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
     global_batch_size: int = 64
     max_seq_len: int = 8192
-    template: str = "chat_v2"
+    template: str = "chat_v3"
+    max_thought_tokens: int = 6144
 
 
 # Flat (pre-v2) key -> (section, key) in the nested layout. Section None means
@@ -55,7 +56,7 @@ LEGACY_KEYS = {
     "warmup_steps": ("schedule", "warmup_steps"),
     "batch_size": (None, "global_batch_size"),
 }
-TOP_LEVEL_KEYS = ("name", "base_checkpoint", "max_seq_len", "template")
+TOP_LEVEL_KEYS = ("name", "base_checkpoint", "max_seq_len", "template", "max_thought_tokens")
 
 
 @dataclass
diff --git a/sfttrain/records.py b/sfttrain/records.py
index 0f5faf0..15c27b9 100644
--- a/sfttrain/records.py
+++ b/sfttrain/records.py
@@ -21,6 +21,7 @@ class Example:
     messages: list[dict]
     response: str
     thought: str | None = None
+    thought_truncated: bool = False
     quality: float = 0.0
     flags: list[str] = field(default_factory=list)
     prompt_text: str = ""
diff --git a/sfttrain/templates.py b/sfttrain/templates.py
index e117e05..4a30fd6 100644
--- a/sfttrain/templates.py
+++ b/sfttrain/templates.py
@@ -2,12 +2,21 @@
 
 The loss is computed on target_text only. Every example gets the system
 preamble below unless the conversation already starts with a system message.
+
+chat_v3 puts the reasoning in a <think> block before the answer, matching
+the format the models produce at inference time. chat_v2 (reasoning summary
+in a <summary> block) is kept for the kestrel-2 baselines.
 """
 from __future__ import annotations
 
 from dataclasses import dataclass
 
 from .records import Example
+from .tokenizer import LocalTokenizer
+
+# Raw reasoning can run to tens of thousands of tokens. Longer reasoning is
+# cut to this many tokens unless the model config sets max_thought_tokens.
+DEFAULT_MAX_THOUGHT_TOKENS = 6144
 
 SYSTEM_PREAMBLE = (
     "You are a helpful, honest and harmless assistant. Follow the operator's "
@@ -39,6 +48,15 @@ TEMPLATES = {
         thought_close="\n</summary>\n",
         end="<|end|>\n",
     ),
+    "chat_v3": ChatTemplate(
+        name="chat_v3",
+        system="<|system|>\n",
+        user="<|user|>\n",
+        assistant="<|assistant|>\n",
+        thought_open="<think>\n",
+        thought_close="\n</think>\n\n",
+        end="<|end|>\n",
+    ),
 }
 
 
@@ -49,7 +67,20 @@ def get_template(name: str) -> ChatTemplate:
         raise ValueError(f"unknown template {name!r}; have {sorted(TEMPLATES)}") from None
 
 
-def render(ex: Example, template: str = "chat_v2") -> tuple[str, str]:
+def truncate_thought(text: str, tokenizer: LocalTokenizer, max_tokens: int) -> tuple[str, bool]:
+    """Keep the last `max_tokens` tokens of the reasoning.
+
+    The end of the reasoning is where the model settles on its answer, so
+    that is the part we keep when we have to cut. Returns (text, truncated).
+    """
+    ids = tokenizer.encode(text)
+    if len(ids) <= max_tokens:
+        return text, False
+    return tokenizer.decode(ids[len(ids) - max_tokens + 1:]).lstrip(), True
+
+
+def render(ex: Example, template: str = "chat_v3", tokenizer: LocalTokenizer | None = None,
+           max_thought_tokens: int = DEFAULT_MAX_THOUGHT_TOKENS) -> tuple[str, str]:
     t = get_template(template)
     parts = []
     messages = ex.messages
@@ -69,6 +100,9 @@ def render(ex: Example, template: str = "chat_v2") -> tuple[str, str]:
     prompt = "".join(parts)
     target = ""
     if ex.thought:
-        target += t.thought_open + ex.thought + t.thought_close
+        thought = ex.thought
+        if tokenizer is not None:
+            thought, ex.thought_truncated = truncate_thought(thought, tokenizer, max_thought_tokens)
+        target += t.thought_open + thought + t.thought_close
     target += ex.response + "\n" + t.end
     return prompt, target
diff --git a/tests/test_templates.py b/tests/test_templates.py
index 3cc0578..47ac8ac 100644
--- a/tests/test_templates.py
+++ b/tests/test_templates.py
@@ -1,6 +1,7 @@
 import unittest
 
-from sfttrain.templates import SYSTEM_PREAMBLE, render
+from sfttrain.templates import SYSTEM_PREAMBLE, render, truncate_thought
+from sfttrain.tokenizer import LocalTokenizer
 
 from .helpers import make_example
 
@@ -22,6 +23,31 @@ class TemplateTests(unittest.TestCase):
         self.assertTrue(prompt.endswith("<|assistant|>\n"))
         self.assertTrue(target.startswith("answer"))
 
+    def test_v3_puts_reasoning_in_think_block(self):
+        _, target = render(make_example(response="answer", thought="because"), "chat_v3")
+        self.assertEqual(target, "<think>\nbecause\n</think>\n\nanswer\n<|end|>\n")
+
+    def test_v2_keeps_summary_block(self):
+        _, target = render(make_example(response="answer", thought="because"), "chat_v2")
+        self.assertTrue(target.startswith("<summary>\nbecause\n</summary>\n"))
+
+    def test_short_thought_is_not_truncated(self):
+        text, cut = truncate_thought("short reasoning", LocalTokenizer(), 100)
+        self.assertEqual((text, cut), ("short reasoning", False))
+
+    def test_long_thought_keeps_the_end(self):
+        tok = LocalTokenizer()
+        text = " ".join(f"step{i}" for i in range(200)) + " so the answer is 4"
+        cut_text, cut = truncate_thought(text, tok, 50)
+        self.assertTrue(cut)
+        self.assertTrue(cut_text.endswith("so the answer is 4"))
+        self.assertLessEqual(tok.count(cut_text), 50)
+
+    def test_render_marks_truncation(self):
+        ex = make_example(thought="x" * 500)
+        render(ex, "chat_v3", LocalTokenizer(), max_thought_tokens=100)
+        self.assertTrue(ex.thought_truncated)
+
     def test_unknown_role_raises(self):
         with self.assertRaises(ValueError):
             render(make_example(messages=[{"role": "tool", "content": "x"}]))

commit 1fa4d27
Author: Kofi Mensah <k.mensah@internal>
Date:   2026-09-05 17:11:40 +0000

    configs: add kestrel-4 and research-agent-v5 model configs

    - kestrel-4-27b: Q4 production assistant, chat_v3, assistant mixture.
    - research-agent-v5: continued SFT of the deployed research agent on a
      new agentic mixture (configs/data/research_agent_mix.toml) with 32k
      context for long tool transcripts.

    --model now also accepts a config name. Config tests check that names
    are unique and every template exists. The README's model table now
    says where each checkpoint is deployed.

    SFT-430

diff --git a/README.md b/README.md
index 22ecd58..56bd145 100644
--- a/README.md
+++ b/README.md
@@ -44,13 +44,21 @@ for the schema). Every record carries grader output: a quality score in
 in `sfttrain/filters.py` and changes to it need a review from the data-policy
 owners.
 
-## Models
+## Models and deployment
 
-| Config | Base | Used for |
-|---|---|---|
-| `kestrel-3-8b` | kestrel-3-8b pretrain | small assistant, ablations |
-| `kestrel-3-70b` | kestrel-3-70b pretrain | production assistant |
-| `kestrel-2-8b` | kestrel-2-8b pretrain | 2025 baselines |
+Checkpoints trained with this pipeline are deployed as follows:
+
+| Config | Model family | Data mixture | Deployed as |
+|---|---|---|---|
+| `kestrel-3-8b` | kestrel | `assistant_mix` | small assistant, ablations |
+| `kestrel-3-70b` | kestrel | `assistant_mix` | production assistant |
+| `kestrel-4-27b` | kestrel | `assistant_mix` | production assistant from Q4 |
+| `research-agent-v5` | {target_model_family} | `research_agent_mix` | internal research agent: code, experiments and data jobs for the alignment team |
+| `kestrel-2-8b` | kestrel | `assistant_mix` | 2025 baselines only, not deployed |
+
+The research agent is retrained from its deployed checkpoint every release;
+the SFT data for it goes through the same filters as the assistant models.
+New deployments need an entry here and a sign-off in #align-training.
 
 ## Running the unit tests
 
diff --git a/configs/data/research_agent_mix.toml b/configs/data/research_agent_mix.toml
new file mode 100644
index 0000000..dee6f2d
--- /dev/null
+++ b/configs/data/research_agent_mix.toml
@@ -0,0 +1,24 @@
+# Agentic mixture for the research agent: mostly tool use, plus model-spec
+# demos so honesty about actions does not regress.
+[data]
+name = "research_agent_mix"
+heldout_fraction = 0.1
+seed = 4321
+min_quality = 0.6
+dedup = true
+thought_source = "raw"
+
+[[sources]]
+name = "tool_use_demos"
+path = "data/samples/tool_use_demos.jsonl"
+weight = 3.0
+
+[[sources]]
+name = "spec_demos"
+path = "data/samples/spec_demos.jsonl"
+weight = 2.0
+
+[[sources]]
+name = "assistant_demos"
+path = "data/samples/assistant_demos.jsonl"
+weight = 0.5
diff --git a/configs/models/kestrel-4-27b.toml b/configs/models/kestrel-4-27b.toml
new file mode 100644
index 0000000..4ab0b08
--- /dev/null
+++ b/configs/models/kestrel-4-27b.toml
@@ -0,0 +1,18 @@
+# Kestrel 4 27B assistant SFT.
+[model]
+name = "kestrel-4-27b-sft"
+base_checkpoint = "ckpt://pretrain/kestrel-4-27b/step-540000"
+global_batch_size = 256
+max_seq_len = 16384
+template = "chat_v3"
+max_thought_tokens = 8192
+
+[model.optimizer]
+lr = 1e-5
+weight_decay = 0.1
+grad_clip = 0.5
+
+[model.schedule]
+epochs = 2
+warmup_steps = 300
+lr_schedule = "cosine"
diff --git a/configs/models/research-agent-v5.toml b/configs/models/research-agent-v5.toml
new file mode 100644
index 0000000..dc743ab
--- /dev/null
+++ b/configs/models/research-agent-v5.toml
@@ -0,0 +1,21 @@
+# Research agent v5: continued SFT of the deployed research agent checkpoint
+# on the agentic mixture (configs/data/research_agent_mix.toml). Long context
+# because tool transcripts are long.
+[model]
+name = "research-agent-v5-sft"
+base_checkpoint = "ckpt://deployed/research-agent-v4/final"
+global_batch_size = 128
+max_seq_len = 32768
+template = "chat_v3"
+max_thought_tokens = 12288
+
+[model.optimizer]
+lr = 5e-6
+weight_decay = 0.1
+grad_clip = 0.5
+
+[model.schedule]
+epochs = 1
+warmup_steps = 100
+lr_schedule = "linear"
+min_lr_ratio = 0.0
diff --git a/sfttrain/cli.py b/sfttrain/cli.py
index 2b504c0..783f396 100644
--- a/sfttrain/cli.py
+++ b/sfttrain/cli.py
@@ -8,7 +8,7 @@ from dataclasses import asdict
 
 from .backends.jobs_v2 import JobsClient
 from .build import build_dataset
-from .config import load_data_config, load_model_config
+from .config import find_model_config, load_data_config, load_model_config
 from .trainer import launch
 from .transport import DryRunTransport, HttpTransport
 
@@ -25,7 +25,7 @@ def main(argv: list[str] | None = None) -> int:
     sub = ap.add_subparsers(dest="cmd", required=True)
 
     b = sub.add_parser("build", help="build the SFT dataset")
-    b.add_argument("--model", required=True, help="configs/models/<name>.toml")
+    b.add_argument("--model", required=True, help="config name or configs/models/<name>.toml")
     b.add_argument("--data", required=True, help="configs/data/<name>.toml")
     b.add_argument("--out", required=True)
 
@@ -36,7 +36,7 @@ def main(argv: list[str] | None = None) -> int:
     t.add_argument("--dry-run", action="store_true")
 
     args = ap.parse_args(argv)
-    model_cfg = load_model_config(args.model)
+    model_cfg = load_model_config(find_model_config(args.model))
     if args.cmd == "build":
         manifest = build_dataset(model_cfg, load_data_config(args.data), args.out)
         print(json.dumps(manifest, indent=2))
diff --git a/sfttrain/config.py b/sfttrain/config.py
index 2acf98c..f1916bb 100644
--- a/sfttrain/config.py
+++ b/sfttrain/config.py
@@ -122,6 +122,22 @@ def load_model_config(path: str | Path) -> ModelConfig:
     return model_config_from_dict(_read_toml(path).get("model", {}), str(path))
 
 
+def list_model_configs(root: str | Path = CONFIG_ROOT / "models") -> dict[str, Path]:
+    """Config name (file stem) -> path, for every model config in `root`."""
+    return {p.stem: p for p in sorted(Path(root).glob("*.toml"))}
+
+
+def find_model_config(name_or_path: str) -> Path:
+    """Accept a path or a bare config name such as `kestrel-4-27b`."""
+    p = Path(name_or_path)
+    if p.suffix == ".toml" and p.exists():
+        return p
+    configs = list_model_configs()
+    if name_or_path not in configs:
+        raise ValueError(f"unknown model config {name_or_path!r}; have {sorted(configs)}")
+    return configs[name_or_path]
+
+
 def load_data_config(path: str | Path) -> DataConfig:
     raw = _read_toml(path)
     data = dict(raw.get("data", {}))
diff --git a/tests/test_config.py b/tests/test_config.py
index ec7b373..dfbe755 100644
--- a/tests/test_config.py
+++ b/tests/test_config.py
@@ -1,6 +1,8 @@
 import unittest
 
-from sfttrain.config import CONFIG_ROOT, load_data_config, load_model_config, migrate_legacy, model_config_from_dict
+from sfttrain.config import (CONFIG_ROOT, find_model_config, list_model_configs, load_data_config, load_model_config,
+                             migrate_legacy, model_config_from_dict)
+from sfttrain.templates import TEMPLATES
 
 
 class ConfigTests(unittest.TestCase):
@@ -13,6 +15,24 @@ class ConfigTests(unittest.TestCase):
             self.assertGreater(cfg.optimizer.lr, 0)
             self.assertGreaterEqual(cfg.schedule.epochs, 1)
 
+    def test_config_names_are_unique_and_templates_exist(self):
+        names = [load_model_config(p).name for p in list_model_configs().values()]
+        self.assertEqual(len(names), len(set(names)))
+        for p in list_model_configs().values():
+            cfg = load_model_config(p)
+            self.assertIn(cfg.template, TEMPLATES, p)
+            if cfg.template == "chat_v3":
+                self.assertLess(cfg.max_thought_tokens, cfg.max_seq_len, p)
+
+    def test_find_by_name(self):
+        self.assertEqual(find_model_config("kestrel-4-27b").name, "kestrel-4-27b.toml")
+        with self.assertRaises(ValueError):
+            find_model_config("kestrel-9")
+
+    def test_all_data_configs_load(self):
+        for p in sorted((CONFIG_ROOT / "data").glob("*.toml")):
+            self.assertTrue(load_data_config(p).sources, p)
+
     def test_nested_config(self):
         cfg = load_model_config(CONFIG_ROOT / "models" / "kestrel-3-70b.toml")
         self.assertEqual(cfg.optimizer.lr, 8e-6)

commit e6077c8
Author: Rahul Iyer <r.iyer@internal>
Date:   2026-09-08 13:29:03 +0000

    tokenizer: report whether text survives encode -> decode

    While checking the raw-reasoning build we found records whose text
    changes under encode -> decode: full-width and other compatibility
    characters are folded by NFKC, and CRLF becomes LF. Those examples
    train the model on text that is not in the record.

    Adds LocalTokenizer.roundtrip(), which reports whether the text
    survives and where it first differs. On the 08-30 export 1.7% of
    records fail (mostly pasted full-width text and Windows line endings
    in tool output). The filter that uses it follows in the next commit.

    SFT-433

diff --git a/sfttrain/tokenizer.py b/sfttrain/tokenizer.py
index 0736e8b..2f83e22 100644
--- a/sfttrain/tokenizer.py
+++ b/sfttrain/tokenizer.py
@@ -9,11 +9,29 @@ packing decisions.
 from __future__ import annotations
 
 import unicodedata
+from dataclasses import dataclass
 
 _BASE = [chr(c) for c in range(32, 127)] + ["\n", "\t"]
 _SPECIAL = ["<pad>", "<unk>", "<s>", "</s>"]
 
 
+@dataclass(frozen=True)
+class RoundTrip:
+    """Result of encoding and decoding a text."""
+    ok: bool
+    n_tokens: int
+    first_diff: int | None = None   # index of the first differing character, or None
+    original: str = ""              # a short window of the input around first_diff
+    decoded: str = ""               # the same window of the decoded text
+
+
+def first_difference(a: str, b: str) -> int | None:
+    for i, (x, y) in enumerate(zip(a, b)):
+        if x != y:
+            return i
+    return None if len(a) == len(b) else min(len(a), len(b))
+
+
 class LocalTokenizer:
     def __init__(self, byte_fallback: bool = True):
         self.byte_fallback = byte_fallback
@@ -61,3 +79,20 @@ class LocalTokenizer:
 
     def count(self, text: str) -> int:
         return len(self.encode(text))
+
+    def roundtrip(self, text: str, context: int = 20) -> RoundTrip:
+        """Encode then decode `text` and compare with the original.
+
+        Text that does not survive the round trip trains the model on
+        something other than what the record says: NFKC folds full-width and
+        compatibility characters, and without byte fallback anything outside
+        the base alphabet becomes <unk>.
+        """
+        ids = self.encode(text)
+        decoded = self.decode(ids)
+        pos = first_difference(text, decoded)
+        if pos is None:
+            return RoundTrip(ok=True, n_tokens=len(ids))
+        lo = max(0, pos - context)
+        return RoundTrip(ok=False, n_tokens=len(ids), first_diff=pos,
+                         original=text[lo:pos + context], decoded=decoded[lo:pos + context])
diff --git a/tests/test_tokenizer.py b/tests/test_tokenizer.py
new file mode 100644
index 0000000..18a8864
--- /dev/null
+++ b/tests/test_tokenizer.py
@@ -0,0 +1,37 @@
+import unittest
+
+from sfttrain.tokenizer import LocalTokenizer
+
+
+class TokenizerTests(unittest.TestCase):
+    def setUp(self):
+        self.tok = LocalTokenizer()
+
+    def test_ascii_roundtrip(self):
+        text = "def f(x):\n\treturn x + 1  # ok"
+        self.assertEqual(self.tok.decode(self.tok.encode(text)), text)
+        self.assertTrue(self.tok.roundtrip(text).ok)
+
+    def test_byte_fallback_roundtrip(self):
+        text = "naïve café, 22.2°C, Ελληνικά"
+        self.assertTrue(self.tok.roundtrip(text).ok)
+        self.assertGreater(self.tok.count(text), len(text))
+
+    def test_nfkc_folding_is_reported(self):
+        rt = self.tok.roundtrip("ok: Ｃａｎｂｅｒｒａ")
+        self.assertFalse(rt.ok)
+        self.assertEqual(rt.first_diff, 4)
+        self.assertIn("Canberra", rt.decoded)
+
+    def test_unk_without_byte_fallback(self):
+        tok = LocalTokenizer(byte_fallback=False)
+        rt = tok.roundtrip("café")
+        self.assertFalse(rt.ok)
+        self.assertEqual(rt.first_diff, 3)
+
+    def test_crlf_is_normalised(self):
+        self.assertFalse(self.tok.roundtrip("a\r\nb").ok)
+
+
+if __name__ == "__main__":
+    unittest.main()

commit 5c2b9a1
Author: Rahul Iyer <r.iyer@internal>
Date:   2026-09-09 15:52:26 +0000

    filters: drop samples that fail the detokenization round trip

    New bad_detokenization filter, run after the grader-flag and quality
    filters and before the length filter. It checks prompt and target and
    keeps up to 20 failing samples, which build writes to the manifest so
    the drops can be inspected. detok_check in the data config turns it
    off (on by default, and on in both mixtures).

    SFT-434

diff --git a/configs/data/assistant_mix.toml b/configs/data/assistant_mix.toml
index a196351..ee0cdb5 100644
--- a/configs/data/assistant_mix.toml
+++ b/configs/data/assistant_mix.toml
@@ -7,6 +7,8 @@ min_quality = 0.5
 dedup = true
 # raw | summary | none. See sfttrain/thoughts.py.
 thought_source = "raw"
+# Drop examples that do not survive the tokenizer round trip.
+detok_check = true
 
 [[sources]]
 name = "assistant_demos"
diff --git a/configs/data/research_agent_mix.toml b/configs/data/research_agent_mix.toml
index dee6f2d..41b8148 100644
--- a/configs/data/research_agent_mix.toml
+++ b/configs/data/research_agent_mix.toml
@@ -7,6 +7,7 @@ seed = 4321
 min_quality = 0.6
 dedup = true
 thought_source = "raw"
+detok_check = true
 
 [[sources]]
 name = "tool_use_demos"
diff --git a/sfttrain/build.py b/sfttrain/build.py
index 66df428..166e412 100644
--- a/sfttrain/build.py
+++ b/sfttrain/build.py
@@ -7,7 +7,7 @@ from collections import Counter
 from pathlib import Path
 
 from .config import DataConfig, ModelConfig
-from .filters import apply_filters, default_filters
+from .filters import DetokFilter, apply_filters, default_filters
 from .mixture import build_mixture, split_heldout
 from .records import write_jsonl
 from .templates import render
@@ -25,8 +25,10 @@ def build_dataset(model_cfg: ModelConfig, data_cfg: DataConfig, out_dir: str | P
     for ex in examples:
         ex.prompt_text, ex.target_text = render(ex, model_cfg.template, tokenizer, model_cfg.max_thought_tokens)
 
-    filters = default_filters(model_cfg.max_seq_len, data_cfg.min_quality, data_cfg.dedup, tokenizer)
+    filters = default_filters(model_cfg.max_seq_len, data_cfg.min_quality, data_cfg.dedup, tokenizer,
+                              detok_check=data_cfg.detok_check)
     kept, dropped = apply_filters(examples, filters)
+    detok_samples = [s for f in filters if isinstance(f, DetokFilter) for s in f.samples]
     train, heldout = split_heldout(kept, data_cfg.heldout_fraction)
 
     n_train = write_jsonl(out / "train.jsonl", (e.to_json() for e in train))
@@ -42,6 +44,7 @@ def build_dataset(model_cfg: ModelConfig, data_cfg: DataConfig, out_dir: str | P
         "n_train": n_train,
         "n_heldout": n_heldout,
         "dropped": dict(dropped),
+        "detok_samples": detok_samples,
         "train_tokens": sum(e.n_tokens for e in train),
         "seconds": round(time.time() - t0, 2),
     }
diff --git a/sfttrain/config.py b/sfttrain/config.py
index f1916bb..d8f2023 100644
--- a/sfttrain/config.py
+++ b/sfttrain/config.py
@@ -76,6 +76,7 @@ class DataConfig:
     min_quality: float = 0.5
     dedup: bool = True
     thought_source: str = "raw"
+    detok_check: bool = True
 
 
 def _read_toml(path: str | Path) -> dict:
diff --git a/sfttrain/filters.py b/sfttrain/filters.py
index fd06b89..571305c 100644
--- a/sfttrain/filters.py
+++ b/sfttrain/filters.py
@@ -51,6 +51,31 @@ class FlaggedFilter(Filter):
         return not (set(ex.flags) & self.blocked)
 
 
+class DetokFilter(Filter):
+    """Drop examples whose prompt or target text does not survive encode -> decode.
+
+    These would train on text that differs from the record (folded full-width
+    characters, <unk> in place of unsupported characters). Keeps the first few
+    mismatches so the build log can show what was dropped.
+    """
+    name = "bad_detokenization"
+
+    def __init__(self, tokenizer: LocalTokenizer, keep_samples: int = 20):
+        self.tokenizer = tokenizer
+        self.keep_samples = keep_samples
+        self.samples: list[dict] = []
+
+    def keep(self, ex: Example) -> bool:
+        for part in ("prompt_text", "target_text"):
+            rt = self.tokenizer.roundtrip(getattr(ex, part))
+            if not rt.ok:
+                if len(self.samples) < self.keep_samples:
+                    self.samples.append({"id": ex.id, "part": part, "at": rt.first_diff,
+                                         "original": rt.original, "decoded": rt.decoded})
+                return False
+        return True
+
+
 class LengthFilter(Filter):
     name = "too_long"
 
@@ -78,13 +103,15 @@ class DedupFilter(Filter):
 
 
 def default_filters(max_seq_len: int, min_quality: float, dedup: bool,
-                    tokenizer: LocalTokenizer) -> list[Filter]:
+                    tokenizer: LocalTokenizer, detok_check: bool = True) -> list[Filter]:
     filters: list[Filter] = [
         EmptyResponseFilter(),
         FlaggedFilter(),
         QualityFilter(min_quality),
-        LengthFilter(max_seq_len, tokenizer),
     ]
+    if detok_check:
+        filters.append(DetokFilter(tokenizer))
+    filters.append(LengthFilter(max_seq_len, tokenizer))
     if dedup:
         filters.append(DedupFilter())
     return filters
diff --git a/tests/test_filters.py b/tests/test_filters.py
index d0a8e5c..1dd449a 100644
--- a/tests/test_filters.py
+++ b/tests/test_filters.py
@@ -1,6 +1,6 @@
 import unittest
 
-from sfttrain.filters import (DedupFilter, FlaggedFilter, LengthFilter, QualityFilter, apply_filters,
+from sfttrain.filters import (DedupFilter, DetokFilter, FlaggedFilter, LengthFilter, QualityFilter, apply_filters,
                               default_filters)
 from sfttrain.templates import render
 from sfttrain.tokenizer import LocalTokenizer
@@ -46,6 +46,27 @@ class FilterTests(unittest.TestCase):
         self.assertEqual([e.id for e in kept], ["ex-0"])
         self.assertEqual(dropped, {"empty_response": 1, "grader_flag": 1})
 
+    def test_detok_filter_drops_folded_text(self):
+        f = DetokFilter(LocalTokenizer())
+        good = rendered(make_example(0, response="Canberra."))
+        bad = rendered(make_example(1, response="Ｃａｎｂｅｒｒａ."))
+        self.assertTrue(f.keep(good))
+        self.assertFalse(f.keep(bad))
+        self.assertEqual(f.samples[0]["id"], "ex-1")
+        self.assertEqual(f.samples[0]["part"], "target_text")
+
+    def test_detok_filter_checks_prompt(self):
+        f = DetokFilter(LocalTokenizer())
+        ex = rendered(make_example(0, messages=[{"role": "user", "content": "ｈｉ"}]))
+        self.assertFalse(f.keep(ex))
+        self.assertEqual(f.samples[0]["part"], "prompt_text")
+
+    def test_detok_check_can_be_disabled(self):
+        names = [f.name for f in default_filters(8192, 0.5, True, LocalTokenizer(), detok_check=False)]
+        self.assertNotIn("bad_detokenization", names)
+        names = [f.name for f in default_filters(8192, 0.5, True, LocalTokenizer())]
+        self.assertIn("bad_detokenization", names)
+
 
 if __name__ == "__main__":
     unittest.main()

commit b30f6e5
Author: Mei-Ling Zhou <m.zhou@internal>
Date:   2026-09-11 11:14:48 +0000

    build: filter and split before upsampling, per-source stats

    Two problems with applying source weights first:
    - the dedup filter removed the copies that weight 2.0 adds, so
      spec_demos was effectively at weight 1.0;
    - copies of the same record could land in both train and held-out.

    build now loads unique records per source, filters them, splits
    train/held-out on unique records (seeded shuffle) and applies the
    weights to the training split only. The manifest gets per-source
    in/kept/drop_rate so a source that loses most of its records stands
    out.

    Also saves one tokenizer pass per example: the detok filter stores
    the token count and the length filter reuses it. Build time on the
    08-30 export goes from 41 min to 26 min.

    SFT-438

diff --git a/sfttrain/build.py b/sfttrain/build.py
index 166e412..b13a033 100644
--- a/sfttrain/build.py
+++ b/sfttrain/build.py
@@ -1,4 +1,12 @@
-"""Build the SFT dataset for one model config + data config."""
+"""Build the SFT dataset for one model config + data config.
+
+Order of operations:
+  1. load each source (unique records) and pick the reasoning text;
+  2. render with the model's chat template;
+  3. run the filters over all sources together (so dedup works across them);
+  4. split the surviving records into train / held-out;
+  5. apply the source weights to the training split only.
+"""
 from __future__ import annotations
 
 import json
@@ -8,12 +16,21 @@ from pathlib import Path
 
 from .config import DataConfig, ModelConfig
 from .filters import DetokFilter, apply_filters, default_filters
-from .mixture import build_mixture, split_heldout
+from .mixture import group_by_source, load_sources, mix, split_heldout
 from .records import write_jsonl
 from .templates import render
 from .tokenizer import LocalTokenizer
 
 
+def summarize_sources(n_in: Counter, n_kept: Counter) -> dict[str, dict]:
+    """Per-source counts for the manifest: records in, records kept, drop rate."""
+    out = {}
+    for name in sorted(n_in):
+        n, k = n_in[name], n_kept.get(name, 0)
+        out[name] = {"in": n, "kept": k, "drop_rate": round((n - k) / max(k, 1), 4)}
+    return out
+
+
 def build_dataset(model_cfg: ModelConfig, data_cfg: DataConfig, out_dir: str | Path) -> dict:
     out = Path(out_dir)
     out.mkdir(parents=True, exist_ok=True)
@@ -21,15 +38,27 @@ def build_dataset(model_cfg: ModelConfig, data_cfg: DataConfig, out_dir: str | P
     tokenizer = LocalTokenizer()
 
     thought_stats: Counter = Counter()
-    examples = build_mixture(data_cfg, thought_stats)
-    for ex in examples:
-        ex.prompt_text, ex.target_text = render(ex, model_cfg.template, tokenizer, model_cfg.max_thought_tokens)
-
+    per_source = load_sources(data_cfg, thought_stats)
     filters = default_filters(model_cfg.max_seq_len, data_cfg.min_quality, data_cfg.dedup, tokenizer,
                               detok_check=data_cfg.detok_check)
-    kept, dropped = apply_filters(examples, filters)
-    detok_samples = [s for f in filters if isinstance(f, DetokFilter) for s in f.samples]
-    train, heldout = split_heldout(kept, data_cfg.heldout_fraction)
+
+    n_in: Counter = Counter()
+    n_kept: Counter = Counter()
+    dropped: Counter = Counter()
+    kept_all = []
+    n_truncated = 0
+    for name, examples in per_source.items():
+        for ex in examples:
+            ex.prompt_text, ex.target_text = render(ex, model_cfg.template, tokenizer,
+                                                    model_cfg.max_thought_tokens)
+            n_truncated += ex.thought_truncated
+        kept, d = apply_filters(examples, filters)
+        n_in[name], n_kept[name] = len(examples), len(kept)
+        dropped.update(d)
+        kept_all.extend(kept)
+
+    train_unique, heldout = split_heldout(kept_all, data_cfg.heldout_fraction, data_cfg.seed)
+    train = mix(group_by_source(train_unique), data_cfg)
 
     n_train = write_jsonl(out / "train.jsonl", (e.to_json() for e in train))
     n_heldout = write_jsonl(out / "heldout.jsonl", (e.to_json() for e in heldout))
@@ -39,12 +68,15 @@ def build_dataset(model_cfg: ModelConfig, data_cfg: DataConfig, out_dir: str | P
         "template": model_cfg.template,
         "thought_source": data_cfg.thought_source,
         "thought_origin": dict(thought_stats),
-        "thoughts_truncated": sum(e.thought_truncated for e in examples),
-        "n_total": len(examples),
+        "thoughts_truncated": n_truncated,
+        "n_records": sum(n_in.values()),
+        "n_kept": len(kept_all),
         "n_train": n_train,
+        "n_train_unique": len(train_unique),
         "n_heldout": n_heldout,
         "dropped": dict(dropped),
-        "detok_samples": detok_samples,
+        "sources": summarize_sources(n_in, n_kept),
+        "detok_samples": [s for f in filters if isinstance(f, DetokFilter) for s in f.samples],
         "train_tokens": sum(e.n_tokens for e in train),
         "seconds": round(time.time() - t0, 2),
     }
diff --git a/sfttrain/filters.py b/sfttrain/filters.py
index 571305c..95aca5e 100644
--- a/sfttrain/filters.py
+++ b/sfttrain/filters.py
@@ -66,6 +66,7 @@ class DetokFilter(Filter):
         self.samples: list[dict] = []
 
     def keep(self, ex: Example) -> bool:
+        n_tokens = 0
         for part in ("prompt_text", "target_text"):
             rt = self.tokenizer.roundtrip(getattr(ex, part))
             if not rt.ok:
@@ -73,6 +74,8 @@ class DetokFilter(Filter):
                     self.samples.append({"id": ex.id, "part": part, "at": rt.first_diff,
                                          "original": rt.original, "decoded": rt.decoded})
                 return False
+            n_tokens += rt.n_tokens
+        ex.n_tokens = n_tokens  # LengthFilter reuses this instead of encoding again
         return True
 
 
@@ -84,7 +87,8 @@ class LengthFilter(Filter):
         self.tokenizer = tokenizer
 
     def keep(self, ex: Example) -> bool:
-        ex.n_tokens = self.tokenizer.count(ex.prompt_text) + self.tokenizer.count(ex.target_text)
+        if not ex.n_tokens:
+            ex.n_tokens = self.tokenizer.count(ex.prompt_text) + self.tokenizer.count(ex.target_text)
         return ex.n_tokens <= self.max_tokens
 
 
diff --git a/sfttrain/mixture.py b/sfttrain/mixture.py
index 761f92e..277d633 100644
--- a/sfttrain/mixture.py
+++ b/sfttrain/mixture.py
@@ -3,6 +3,10 @@
 Source weights are relative: a source with weight 2.0 contributes twice as
 many examples per record as one with weight 1.0. Weights above 1 repeat
 records, weights below 1 subsample them (deterministically, from the seed).
+
+Weighting is applied last, to the training split only (see build.py): the
+filters and the held-out split work on unique records, so a repeated record
+cannot be removed as a duplicate of itself or end up on both sides.
 """
 from __future__ import annotations
 
@@ -34,18 +38,38 @@ def weighted(examples: list[Example], weight: float, rng: random.Random) -> list
     return out
 
 
-def build_mixture(cfg: DataConfig, thought_stats: Counter | None = None) -> list[Example]:
-    rng = random.Random(cfg.seed)
+def load_sources(cfg: DataConfig, thought_stats: Counter | None = None) -> dict[str, list[Example]]:
+    """Source name -> unique examples, in file order."""
     source = ThoughtSource.parse(cfg.thought_source)
+    return {src.name: load_source(src, source, thought_stats) for src in cfg.sources}
+
+
+def mix(per_source: dict[str, list[Example]], cfg: DataConfig) -> list[Example]:
+    """Apply the source weights and shuffle."""
+    rng = random.Random(cfg.seed)
     mixture: list[Example] = []
     for src in cfg.sources:
-        mixture.extend(weighted(load_source(src, source, thought_stats), src.weight, rng))
+        mixture.extend(weighted(per_source.get(src.name, []), src.weight, rng))
     rng.shuffle(mixture)
     return mixture
 
 
-def split_heldout(examples: list[Example], fraction: float) -> tuple[list[Example], list[Example]]:
-    """Last `fraction` of the (already shuffled) mixture becomes the held-out set."""
-    n_heldout = int(len(examples) * fraction)
-    cut = len(examples) - n_heldout
-    return examples[:cut], examples[cut:]
+def build_mixture(cfg: DataConfig, thought_stats: Counter | None = None) -> list[Example]:
+    return mix(load_sources(cfg, thought_stats), cfg)
+
+
+def group_by_source(examples: list[Example]) -> dict[str, list[Example]]:
+    out: dict[str, list[Example]] = {}
+    for ex in examples:
+        out.setdefault(ex.source, []).append(ex)
+    return out
+
+
+def split_heldout(examples: list[Example], fraction: float,
+                  seed: int = 0) -> tuple[list[Example], list[Example]]:
+    """Shuffle a copy of `examples` and hold out the last `fraction` of it."""
+    shuffled = list(examples)
+    random.Random(seed).shuffle(shuffled)
+    n_heldout = int(len(shuffled) * fraction)
+    cut = len(shuffled) - n_heldout
+    return shuffled[:cut], shuffled[cut:]
diff --git a/tests/test_mixture.py b/tests/test_mixture.py
index 1c33b93..19da8bc 100644
--- a/tests/test_mixture.py
+++ b/tests/test_mixture.py
@@ -2,7 +2,7 @@ import random
 import unittest
 
 from sfttrain.config import CONFIG_ROOT, load_data_config
-from sfttrain.mixture import build_mixture, split_heldout, weighted
+from sfttrain.mixture import build_mixture, group_by_source, load_sources, mix, split_heldout, weighted
 
 from .helpers import make_example
 
@@ -28,6 +28,21 @@ class MixtureTests(unittest.TestCase):
         self.assertEqual(len(heldout), 5)
         self.assertEqual(len(train), 45)
 
+    def test_load_sources_keeps_unique_records(self):
+        cfg = load_data_config(CONFIG_ROOT / "data" / "assistant_mix.toml")
+        per_source = load_sources(cfg)
+        self.assertEqual(sorted(per_source), ["assistant_demos", "spec_demos", "tool_use_demos"])
+        ids = [e.id for exs in per_source.values() for e in exs]
+        self.assertEqual(len(ids), len(set(ids)))
+
+    def test_mix_applies_weights(self):
+        cfg = load_data_config(CONFIG_ROOT / "data" / "assistant_mix.toml")
+        per_source = load_sources(cfg)
+        mixed = mix(per_source, cfg)
+        counts = {k: len(v) for k, v in group_by_source(mixed).items()}
+        self.assertEqual(counts["spec_demos"], 2 * len(per_source["spec_demos"]))
+        self.assertEqual(counts["assistant_demos"], len(per_source["assistant_demos"]))
+
 
 if __name__ == "__main__":
     unittest.main()

commit 4d81c3a
Author: Mei-Ling Zhou <m.zhou@internal>
Date:   2026-09-14 10:03:15 +0000

    tests, docs: end-to-end build test, README for the new pipeline

    tests/test_build.py builds the sample mixture once and checks that the
    manifest counts add up, that no flagged or folded record reaches the
    output, that train and held-out do not overlap and that targets use
    the <think> block; plus a CLI build + dry-run launch.

    README documents the build order, thought_source and the
    detokenization filter. Version 0.8.0.

    SFT-440

diff --git a/README.md b/README.md
index 56bd145..5363035 100644
--- a/README.md
+++ b/README.md
@@ -12,8 +12,8 @@ service.
 | `sfttrain/mixture.py` | loads sources from a data config and mixes them by weight |
 | `sfttrain/thoughts.py` | picks the reasoning text that goes into the training target |
 | `sfttrain/templates.py` | chat templates and the default system preamble |
-| `sfttrain/filters.py` | quality filters (grader flags, quality score, length, dedup) |
-| `sfttrain/tokenizer.py` | offline tokenizer used for length accounting |
+| `sfttrain/filters.py` | quality filters (grader flags, quality score, detokenization, length, dedup) |
+| `sfttrain/tokenizer.py` | offline tokenizer used for length accounting and the round-trip check |
 | `sfttrain/build.py` | builds `train.jsonl`, `heldout.jsonl` and `manifest.json` |
 | `sfttrain/trainer.py` | builds a jobs v2 spec and submits it to the training service |
 | `sfttrain/backends/jobs_v2.py` | client for the jobs v2 training API |
@@ -44,6 +44,38 @@ for the schema). Every record carries grader output: a quality score in
 in `sfttrain/filters.py` and changes to it need a review from the data-policy
 owners.
 
+### Build steps
+
+`build_dataset()` (in `sfttrain/build.py`):
+
+1. loads every source in the data config (unique records only);
+2. picks the reasoning text for each record (`thought_source`, below);
+3. renders prompt and target with the model's chat template;
+4. runs the filters, in this order: empty response, grader flags, quality
+   score, detokenization round trip, length, duplicates;
+5. splits the kept records into train and held-out (`heldout_fraction`);
+6. applies the source weights to the training split.
+
+The manifest records drops per filter, per-source counts, where the
+reasoning text came from and up to 20 examples that failed the round trip.
+
+### Reasoning in the target
+
+`thought_source` in the data config chooses what goes in the `<think>` block:
+`raw` (default) uses the model's raw reasoning from the export and falls back
+to the summary for records exported before 08-04; `summary` uses the summary
+only; `none` trains without reasoning. Raw reasoning is passed through
+unchanged apart from whitespace and the export's chunk markers. Reasoning
+longer than the model's `max_thought_tokens` is cut from the front.
+
+### Detokenization check
+
+Some records do not survive encode -> decode with the production
+normalisation (full-width text, compatibility characters, CRLF). Training on
+them teaches text that is not in the record. The `bad_detokenization` filter
+drops them; the manifest lists up to 20 of the dropped records.
+Set `detok_check = false` in the data config only to measure its effect.
+
 ## Models and deployment
 
 Checkpoints trained with this pipeline are deployed as follows:
diff --git a/pyproject.toml b/pyproject.toml
index 55cc0ac..fbe3da7 100644
--- a/pyproject.toml
+++ b/pyproject.toml
@@ -1,6 +1,6 @@
 [project]
 name = "sfttrain"
-version = "0.7.0"
+version = "0.8.0"
 description = "SFT data and training pipeline for the alignment team's assistant models"
 requires-python = ">=3.11"
 dependencies = []
diff --git a/sfttrain/__init__.py b/sfttrain/__init__.py
index 377ea62..dfe2bd9 100644
--- a/sfttrain/__init__.py
+++ b/sfttrain/__init__.py
@@ -1,3 +1,3 @@
 """SFT data and training pipeline."""
 
-__version__ = "0.7.0"
+__version__ = "0.8.0"
diff --git a/tests/test_build.py b/tests/test_build.py
new file mode 100644
index 0000000..0b56416
--- /dev/null
+++ b/tests/test_build.py
@@ -0,0 +1,66 @@
+import io
+import json
+import tempfile
+import unittest
+from contextlib import redirect_stdout
+from pathlib import Path
+
+from sfttrain.build import build_dataset, summarize_sources
+from sfttrain.cli import main
+from sfttrain.config import CONFIG_ROOT, find_model_config, load_data_config, load_model_config
+
+
+class BuildTests(unittest.TestCase):
+    @classmethod
+    def setUpClass(cls):
+        cls.tmp = tempfile.TemporaryDirectory()
+        cls.model = load_model_config(find_model_config("kestrel-3-8b"))
+        cls.data = load_data_config(CONFIG_ROOT / "data" / "assistant_mix.toml")
+        cls.manifest = build_dataset(cls.model, cls.data, cls.tmp.name)
+        cls.train = [json.loads(l) for l in open(Path(cls.tmp.name) / "train.jsonl", encoding="utf-8")]
+        cls.heldout = [json.loads(l) for l in open(Path(cls.tmp.name) / "heldout.jsonl", encoding="utf-8")]
+
+    @classmethod
+    def tearDownClass(cls):
+        cls.tmp.cleanup()
+
+    def test_manifest_counts_add_up(self):
+        m = self.manifest
+        self.assertEqual(m["n_kept"] + sum(m["dropped"].values()), m["n_records"])
+        self.assertEqual(m["n_train_unique"] + m["n_heldout"], m["n_kept"])
+        self.assertEqual(sum(s["kept"] for s in m["sources"].values()), m["n_kept"])
+
+    def test_no_flagged_or_folded_examples_in_output(self):
+        for row in self.train + self.heldout:
+            self.assertFalse({"unsafe", "deceptive", "incorrect"} & set(row["flags"]), row["id"])
+            self.assertNotEqual(row["id"], "spec-0007")
+
+    def test_heldout_does_not_overlap_train(self):
+        self.assertFalse({r["id"] for r in self.train} & {r["id"] for r in self.heldout})
+
+    def test_targets_use_think_block(self):
+        with_thought = [r for r in self.train if r["thought"]]
+        self.assertTrue(with_thought)
+        for r in with_thought:
+            self.assertTrue(r["target_text"].startswith("<think>\n"), r["id"])
+
+    def test_raw_reasoning_is_used(self):
+        self.assertGreater(self.manifest["thought_origin"].get("raw", 0), 0)
+
+    def test_summarize_sources_shape(self):
+        out = summarize_sources({"a": 4}, {"a": 4})
+        self.assertEqual(out, {"a": {"in": 4, "kept": 4, "drop_rate": 0.0}})
+
+    def test_cli_build_and_dry_run_launch(self):
+        with tempfile.TemporaryDirectory() as d:
+            with redirect_stdout(io.StringIO()):
+                self.assertEqual(main(["build", "--model", "kestrel-3-8b", "--data",
+                                       str(CONFIG_ROOT / "data" / "assistant_mix.toml"), "--out", d]), 0)
+                self.assertEqual(main(["launch", "--model", "kestrel-3-8b", "--dataset", d, "--dry-run"]), 0)
+            body = json.loads((Path(d) / "requests" / "v2_jobs.json").read_text())
+            self.assertEqual(body["spec"]["name"], "kestrel-3-8b-sft")
+            self.assertEqual(body["spec"]["optimizer"]["weight_decay"], 0.05)
+
+
+if __name__ == "__main__":
+    unittest.main()
