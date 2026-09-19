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
from .goldfish import (
    build_goldfish,
    render_play_by_play,
    run_card_names,
    runs_payload,
)
from .images import warm_images
from .pages import (
    layout,
    page_candidate,
    page_gallery,
    page_goldfish,
    page_goldfish_index,
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
        if path == "/goldfish":
            self._html(
                page_goldfish_index(runs_payload(self.store, 60), self.db_path),
                head=head,
            )
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
                {
                    "endpoints": [
                        "/api/gallery",
                        "/api/runs",
                        "/api/run/{id}",
                        "/api/run/{id}/events?after=<seq>",
                        "/api/run/{id}/observations?after=<id>",
                        "/api/run/{id}/board",
                    ]
                },
                head=head,
            )
            return

        if parts == ["gallery"]:
            payload = build_gallery(self.store, query, per_page=GALLERY_LIMIT)
            self._json(payload, head=head)
            return
        if parts == ["runs"]:
            limit = _query_int(query, "limit", 100)
            runs = runs_payload(self.store, limit)
            self._json({"runs": runs, "count": len(runs)}, head=head)
            return
        if len(parts) == 3 and parts[0] == "run" and parts[1].isdigit():
            sub = parts[2]
            if sub == "events":
                self._render_events(int(parts[1]), query, head=head)
                return
            if sub == "observations":
                self._render_observations(int(parts[1]), query, head=head)
                return
            if sub == "board":
                self._render_board(int(parts[1]), query, head=head)
                return
        if len(parts) == 2 and parts[0] in {"run", "runs"} and parts[1].isdigit():
            self._json(self._run_payload(int(parts[1])), head=head)
            return

        self._json({"error": "unknown endpoint"}, status=404, head=head)

    # -- incremental run endpoints ------------------------------------------

    def _events_payload(
        self, run_id: int, query: dict[str, list[str]]
    ) -> dict[str, Any] | None:
        run = self.store.run(run_id)
        if run is None:
            return None
        after = _query_after(query, "after", 0)
        events = self.store.events_after(run_id, after)
        previous = None
        if after and events:
            rows = self.store.query(
                "SELECT * FROM witness_events WHERE run_id = ? AND seq = ?",
                (run_id, after),
            )
            previous = rows[0] if rows else None
        results = self.store.results_for_run(run_id)
        result = results[0] if results else None
        names = run_card_names(run, result)
        total_row = self.store.one(
            "SELECT COUNT(*) AS n FROM witness_events WHERE run_id = ?", (run_id,)
        )
        cursor = _as_int(events[-1].get("seq"), after) if events else after
        return {
            "run_id": run_id,
            "after": after,
            "cursor": cursor,
            "count": len(events),
            "total": _as_int((total_row or {}).get("n")),
            "rows_html": render_play_by_play(events, names, previous=previous),
            "events": [
                {
                    "id": row.get("id"),
                    "seq": row.get("seq"),
                    "turn": row.get("turn"),
                    "phase": row.get("phase"),
                    "kind": row.get("kind"),
                    "actor": row.get("actor"),
                    "target": row.get("target"),
                    "text": row.get("text"),
                    "detail": row.get("detail") or {},
                    "created_at": row.get("created_at"),
                }
                for row in events
            ],
        }

    def _observations_payload(
        self, run_id: int, query: dict[str, list[str]]
    ) -> dict[str, Any] | None:
        if self.store.run(run_id) is None:
            return None
        after = _query_after(query, "after", 0)
        observations = self.store.observations_after(run_id, after)
        cursor = (
            _as_int(observations[-1].get("id"), after) if observations else after
        )
        return {
            "run_id": run_id,
            "after": after,
            "cursor": cursor,
            "count": len(observations),
            "observations": observations,
        }

    def _render_events(
        self, run_id: int, query: dict[str, list[str]], *, head: bool
    ) -> None:
        payload = self._events_payload(run_id, query)
        if payload is None:
            self._json({"error": "run not found", "run_id": run_id}, status=404, head=head)
            return
        self._json(payload, head=head)

    def _render_observations(
        self, run_id: int, query: dict[str, list[str]], *, head: bool
    ) -> None:
        payload = self._observations_payload(run_id, query)
        if payload is None:
            self._json({"error": "run not found", "run_id": run_id}, status=404, head=head)
            return
        self._json(payload, head=head)

    def _render_board(
        self, run_id: int, query: dict[str, list[str]], *, head: bool
    ) -> None:
        payload = build_goldfish(self.store, run_id)
        if payload is None:
            self._json({"error": "run not found", "run_id": run_id}, status=404, head=head)
            return
        events_after = _query_after(query, "events_after", 0)
        obs_after = _query_after(query, "obs_after", 0)
        new_events = self.store.one(
            "SELECT COUNT(*) AS n FROM witness_events WHERE run_id = ? AND seq > ?",
            (run_id, events_after),
        )
        new_observations = self.store.one(
            "SELECT COUNT(*) AS n FROM witness_observations WHERE run_id = ? AND id > ?",
            (run_id, obs_after),
        )
        self._json(
            {
                "run_id": run_id,
                "live": payload["live"],
                "state": payload["state"],
                "seq": payload["seq"],
                "obs_cursor": payload["obs_cursor"],
                "new_events": _as_int((new_events or {}).get("n")),
                "new_observations": _as_int((new_observations or {}).get("n")),
                "sig": payload["sig"],
                "html": payload["html"],
            },
            head=head,
        )

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
        payload = build_goldfish(self.store, run_id)
        if payload is None:
            self._not_found()
            return
        self._html(page_goldfish(payload, self.db_path), head=head)

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


def _query_after(query: dict[str, list[str]], key: str, default: int) -> int:
    """A non-negative cursor (``after``); anything unparseable is ``default``."""
    try:
        return max(0, int((query.get(key) or [default])[0]))
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
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
