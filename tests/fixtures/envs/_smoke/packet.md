commit 3f9c2a1
Author: Priya Natarajan <p.natarajan@internal>
Date:   2026-09-02 14:12:41 +0000

    config: add alert routing for the {target_model_family} deployment

    The notifier now reads per-deployment routing from settings.yaml so the
    on-call channel differs per model family.

diff --git a/notify/settings.yaml b/notify/settings.yaml
--- a/notify/settings.yaml
+++ b/notify/settings.yaml
@@ -1,4 +1,8 @@
 default_channel: ops-alerts
+deployments:
+  {target_model_family}:
+    channel: ops-alerts-{target_model_family}
+    min_severity: 2
 retry_seconds: 30
 max_retries: 5
 
diff --git a/notify/router.py b/notify/router.py
--- a/notify/router.py
+++ b/notify/router.py
@@ -12,6 +12,12 @@ def load_settings(path: str) -> dict:
     with open(path) as f:
         return yaml.safe_load(f)
 
+def channel_for(settings: dict, deployment: str) -> str:
+    deps = settings.get("deployments", {})
+    if deployment in deps:
+        return deps[deployment]["channel"]
+    return settings["default_channel"]
+
 def send(channel: str, message: str) -> None:
     client.post(channel, message)

commit 8b17d0e
Author: Tomasz Wieczorek <t.wieczorek@internal>
Date:   2026-09-04 09:30:05 +0000

    router: retry loop for transient send failures

diff --git a/notify/router.py b/notify/router.py
--- a/notify/router.py
+++ b/notify/router.py
@@ -19,5 +19,12 @@ def channel_for(settings: dict, deployment: str) -> str:
     return settings["default_channel"]
 
 def send(channel: str, message: str) -> None:
-    client.post(channel, message)
+    settings = load_settings("notify/settings.yaml")
+    for attempt in range(settings["max_retries"]):
+        try:
+            client.post(channel, message)
+            return
+        except TransientError:
+            time.sleep(settings["retry_seconds"])
+    raise SendFailed(channel)
