"""A tiny standard-library router and the read-only HTTP server.

No framework: :class:`http.server.ThreadingHTTPServer` plus a path dispatcher.
Only ``GET``/``HEAD`` are implemented — there is no write surface at all.
HTML is rendered server-side; the JSON endpoints exist for the Gallery poll and
for programmatic inspection.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..corpus.names import normalize_card_name, pair_hash
from .db import ReadOnlyStore, parse_json
from .gallery import PAGE_SIZE, build_gallery
from .images import warm_images
from .pages import (
    layout,
    page_candidate,
    page_gallery,
    page_run,
)
from .render import build_samples, find_cycles

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
GALLERY_LIMIT = PAGE_SIZE


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def replay_command(
    db_path: str, result: dict[str, Any], run: dict[str, Any]
) -> str:
    """A copy-paste command that reproduces a witness run."""
    seeds = run.get("seeds") or parse_json(run.get("seeds_json"), []) or []
    seeds_text = ",".join(str(s) for s in seeds) if isinstance(seeds, list) else str(seeds)
    params = run.get("params") or parse_json(run.get("params_json"), {}) or {}
    parts = [
        "combo-witness",
        f"--db {db_path}",
        f"--candidate {result.get('candidate_key') or '?'}",
    ]
    if seeds_text:
        parts.append(f"--seeds {seeds_text}")
    if params.get("max_iterations") is not None:
        parts.append(f"--max-iterations {params['max_iterations']}")
    if params.get("max_decisions") is not None:
        parts.append(f"--max-decisions {params['max_decisions']}")
    parts.append("--persist")
    return " ".join(parts)


def _evidence(result: dict[str, Any]) -> Any:
    return parse_json(result.get("evidence_json"), None)


def _diagnostics(result: dict[str, Any]) -> Any:
    own = parse_json(result.get("diagnostics_json"), None)
    if own:
        return own
    evidence = _evidence(result)
    if isinstance(evidence, dict):
        return evidence.get("diagnostics")
    return None


def _decorate_interactions(
    store: ReadOnlyStore, interactions: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    ids: list[int] = []
    for row in interactions:
        for key in ("source_card_id", "target_card_id"):
            value = row.get(key)
            if value is not None:
                ids.append(int(value))
    names = store.card_names(ids)
    for row in interactions:
        source = row.get("source_card_id")
        target = row.get("target_card_id")
        row["source_name"] = names.get(int(source)) if source is not None else None
        row["target_name"] = names.get(int(target)) if target is not None else None
    return interactions


def _pair_badge(store: ReadOnlyStore, hypothesis: dict[str, Any]) -> str:
    ids = hypothesis.get("card_ids") or []
    names = hypothesis.get("card_names") or []
    if len(ids) == 2 and len(names) == 2:
        digest = pair_hash(normalize_card_name(names[0]), normalize_card_name(names[1]))
        return store.known_pair_state(digest)
    return "candidate"


# ---------------------------------------------------------------------------
# handler
# ---------------------------------------------------------------------------


class WebHandler(BaseHTTPRequestHandler):
    server_version = "combo-discovery-web/0.1"
    protocol_version = "HTTP/1.1"

    # -- plumbing -----------------------------------------------------------

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Silence per-request logging; errors surface through responses instead.
        return

    @property
    def store(self) -> ReadOnlyStore:
        return self.server.store  # type: ignore[attr-defined]

    @property
    def db_path(self) -> str:
        return self.server.db_path  # type: ignore[attr-defined]

    def _send(
        self, status: int, body: bytes, content_type: str, *, head: bool = False
    ) -> None:
        self.send_response(status)
        if content_type:
            self.send_header("Content-Type", content_type)
        if status != 204:
            self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if not head:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):  # pragma: no cover
                pass

    def _html(self, markup: str, status: int = 200, *, head: bool = False) -> None:
        self._send(status, markup.encode("utf-8"), "text/html; charset=utf-8", head=head)

    def _json(self, payload: Any, status: int = 200, *, head: bool = False) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", head=head)

    def _not_found(self) -> None:
        markup = layout(
            "Not found",
            '<section class="hero"><h1>Not found</h1>'
            '<p class="lede">That path does not exist in this console.</p>'
            '<p><a href="/">← back to The Gallery</a></p></section>',
            active="gallery",
            db_path=self.db_path,
        )
        self._html(markup, status=404)

    # -- verbs --------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        try:
            self._route(head=False)
        except Exception as exc:  # noqa: BLE001 - never leak a stack to the client
            self._json({"error": f"{type(exc).__name__}: {exc}"}, status=500)

    def do_HEAD(self) -> None:  # noqa: N802
        try:
            self._route(head=True)
        except Exception as exc:  # noqa: BLE001
            self._json({"error": f"{type(exc).__name__}: {exc}"}, status=500, head=True)

    def do_POST(self) -> None:  # noqa: N802
        self._send(
            405,
            b'{"error":"read-only server"}',
            "application/json; charset=utf-8",
        )

    do_PUT = do_POST  # type: ignore[assignment]
    do_DELETE = do_POST  # type: ignore[assignment]
    do_PATCH = do_POST  # type: ignore[assignment]

    # -- routing ------------------------------------------------------------

    def _route(self, *, head: bool) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        parts = [segment for segment in path.split("/") if segment]

        if path == "/favicon.ico":
            self._send(204, b"", "image/x-icon", head=head)
            return

        # -- JSON API -------------------------------------------------------
        if parts and parts[0] == "api":
            self._route_api(parts[1:], query, head=head)
            return

        # -- HTML pages -----------------------------------------------------
        if path == "/":
            payload = build_gallery(self.store, query, per_page=GALLERY_LIMIT)
            self._html(page_gallery(payload, self.db_path), head=head)
            return
        if len(parts) == 2 and parts[0] == "run" and parts[1].isdigit():
            self._render_run(int(parts[1]), head=head)
            return
        if len(parts) == 2 and parts[0] == "candidate" and parts[1].isdigit():
            self._render_candidate(int(parts[1]), head=head)
            return

        self._not_found()

    def _route_api(
        self, parts: list[str], query: dict[str, list[str]], *, head: bool
    ) -> None:
        if not parts:
            self._json(
                {"endpoints": ["/api/gallery", "/api/runs", "/api/run/{id}"]},
                head=head,
            )
            return

        if parts == ["gallery"]:
            payload = build_gallery(self.store, query, per_page=GALLERY_LIMIT)
            self._json(payload, head=head)
            return
        if parts == ["runs"]:
            limit = _query_int(query, "limit", 100)
            runs = self.store.list_runs(limit)
            self._json({"runs": runs, "count": len(runs)}, head=head)
            return
        if len(parts) == 2 and parts[0] in {"run", "runs"} and parts[1].isdigit():
            self._json(self._run_payload(int(parts[1])), head=head)
            return

        self._json({"error": "unknown endpoint"}, status=404, head=head)

    # -- payloads -----------------------------------------------------------

    def _run_payload(self, run_id: int) -> dict[str, Any]:
        run = self.store.run(run_id)
        if run is None:
            return {"error": "run not found", "run_id": run_id}
        results = self.store.results_for_run(run_id)
        result = results[0] if results else None
        for row in results:
            row["cards"] = parse_json(row.get("card_names_json"), []) or []
        observations = self.store.observations(run_id)
        payload: dict[str, Any] = {
            "run": run,
            "result": result,
            "results": results,
            "observations": observations,
        }
        if result:
            result["evidence"] = _evidence(result)
            result["diagnostics"] = _diagnostics(result)
            payload["evidence"] = result["evidence"]
            payload["diagnostics"] = result["diagnostics"]
            payload["replay"] = replay_command(self.db_path, result, run)
            samples, per_step = build_samples(observations, result)
            payload["cycles"] = find_cycles(samples)
            payload["per_step"] = per_step
        return payload

    # -- HTML pages ---------------------------------------------------------

    def _render_run(self, run_id: int, *, head: bool) -> None:
        run = self.store.run(run_id)
        if run is None:
            self._not_found()
            return
        results = self.store.results_for_run(run_id)
        result = results[0] if results else None
        if result is not None:
            result["cards"] = parse_json(result.get("card_names_json"), []) or []

        observations = self.store.observations(run_id)
        hypothesis = None
        interactions: list[dict[str, Any]] = []
        if result and str(result.get("candidate_key", "")).isdigit():
            hypothesis = self.store.hypothesis(int(result["candidate_key"]))
        if hypothesis:
            ids = hypothesis.get("card_ids") or []
            if len(ids) >= 2:
                interactions = _decorate_interactions(
                    self.store, self.store.interactions_for_pair(ids[0], ids[1])
                )
        if result is None:
            # A run still streaming: show what we have with a provisional verdict.
            result = {}
        run = dict(run)
        run["seeds"] = parse_json(run.get("seeds_json"), []) or []
        run["params"] = parse_json(run.get("params_json"), {}) or {}
        if result:
            run["replay"] = replay_command(self.db_path, result, run)

        self._html(
            page_run(
                run=run,
                result=result or None,
                observations=observations,
                evidence=_evidence(result) if result else None,
                diagnostics=_diagnostics(result) if result else None,
                hypothesis=hypothesis,
                interactions=interactions,
                db_path=self.db_path,
            ),
            head=head,
        )

    def _render_candidate(self, hypothesis_id: int, *, head: bool) -> None:
        hypothesis = self.store.hypothesis(hypothesis_id)
        if hypothesis is None:
            self._not_found()
            return
        ids = hypothesis.get("card_ids") or []
        interactions: list[dict[str, Any]] = []
        if len(ids) >= 2:
            interactions = _decorate_interactions(
                self.store, self.store.interactions_for_pair(ids[0], ids[1])
            )
        self._html(
            page_candidate(
                hypothesis=hypothesis,
                badge_state=_pair_badge(self.store, hypothesis),
                interactions=interactions,
                results=self.store.results_for_candidate(str(hypothesis_id)),
                cards=self.store.cards(ids),
                db_path=self.db_path,
            ),
            head=head,
        )


def _query_int(query: dict[str, list[str]], key: str, default: int) -> int:
    try:
        return max(1, min(500, int((query.get(key) or [default])[0])))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# server factory
# ---------------------------------------------------------------------------


class WitnessServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        db_path: str,
        store: ReadOnlyStore,
    ) -> None:
        super().__init__(address, WebHandler)
        self.db_path = db_path
        self.store = store


def create_server(
    db_path: str,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> WitnessServer:
    """Bind a server to ``host:port`` (``port=0`` picks a free one)."""
    store = ReadOnlyStore(db_path)
    try:
        server = WitnessServer((host, int(port)), db_path=str(db_path), store=store)
    except Exception:
        store.close()
        raise
    return server


def serve(
    db_path: str,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> int:
    """Run the console until interrupted. Returns a process exit code."""
    try:
        server = create_server(db_path, host=host, port=port)
    except sqlite3.OperationalError as exc:
        print(f"combo-web: cannot open database {db_path!r} read-only: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"combo-web: cannot bind {host}:{port}: {exc}", file=sys.stderr)
        return 1
    bound_host, bound_port = server.server_address[:2]
    print(f"combo-web  http://{bound_host}:{bound_port}  (db: {db_path}, read-only)")
    # Pay the one-off Scryfall bulk load now, so the first reader gets a fast
    # paint instead of waiting ~4s on the first card lookup.
    warmed = warm_images()
    if warmed > 0.2:
        print(f"combo-web  card index warmed in {warmed:.1f}s")
    print("press ctrl+c to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping…")
    finally:
        server.shutdown()
        server.server_close()
        server.store.close()
    return 0


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "WebHandler",
    "WitnessServer",
    "create_server",
    "replay_command",
    "serve",
]
