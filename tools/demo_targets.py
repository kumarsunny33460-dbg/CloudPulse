"""A tiny set of HTTP endpoints that CloudPulse can monitor.

Pointing a local development install at real production URLs is unreliable:
corporate proxies intercept TLS, laptops go offline, and third party services
rate limit. This service gives the demo dataset stable, instant, fully local
targets so the dashboard always has something meaningful to show.

Usage
-----
    python tools/demo_targets.py            # serves on 127.0.0.1:5099
    python tools/demo_targets.py --port 6000

Endpoints
---------
    /health                  200, body contains "ok"          (healthy)
    /healthz                 200, body contains "ok"          (healthy)
    /                        200                              (healthy)
    /status/404              404                              (down unless expected)
    /status/500              500                              (down)
    /slow                    200 after ~2.5s                  (degraded)
    /flaky                   fails roughly one time in three  (intermittent)
    /no-keyword              200 but without the word "ok"    (down when a
                                                             keyword is required)
    /echo/<name>             200, echoes the path             (healthy)
"""

from __future__ import annotations

import argparse
import time
from datetime import UTC, datetime

from flask import Flask, Response, jsonify, request

app = Flask(__name__)

_started_at = time.time()
_flaky_counter = {"value": 0}


@app.route("/health")
@app.route("/healthz")
def health():
    return Response("ok", status=200, mimetype="text/plain")


@app.route("/")
def index():
    return jsonify(
        {
            "service": "cloudpulse-demo-targets",
            "status": "ok",
            "uptime_seconds": round(time.time() - _started_at, 1),
            "time": datetime.now(UTC).isoformat(),
        }
    )


@app.route("/status/<int:code>")
def status(code):
    return Response(f"status {code}", status=code, mimetype="text/plain")


@app.route("/slow")
def slow():
    time.sleep(2.5)
    return Response("ok", status=200, mimetype="text/plain")


@app.route("/flaky")
def flaky():
    _flaky_counter["value"] += 1
    if _flaky_counter["value"] % 3 == 0:
        return Response("temporarily unavailable", status=503, mimetype="text/plain")
    return Response("ok", status=200, mimetype="text/plain")


@app.route("/no-keyword")
def no_keyword():
    return Response("service is responding", status=200, mimetype="text/plain")


@app.route("/echo/<path:name>")
def echo(name):
    return jsonify({"echo": name, "query": request.args.to_dict()})


@app.route("/__demo/reset")
def reset():
    _flaky_counter["value"] = 0
    return jsonify({"status": "reset"})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5099)
    arguments = parser.parse_args()

    print(f"CloudPulse demo targets listening on http://{arguments.host}:{arguments.port}")
    print("  /health  /healthz  /  /status/404  /status/500  /slow  /flaky  /no-keyword")
    app.run(host=arguments.host, port=arguments.port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
