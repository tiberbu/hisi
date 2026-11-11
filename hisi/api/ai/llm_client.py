# Copyright (c) 2026, Tiberbu and Contributors
# License: MIT. See LICENSE

"""LLM HTTP client — the ONLY module in the AI package that talks to the
LLM host.

Implements the streaming protocol: ``POST {base_url}/chat/completions``
with ``Authorization: Bearer <key>``, ``temperature: 0``, ``stream: true``;
SSE ``data:`` lines parsed incrementally, ``delta.content`` accumulated
across chunks, ``:``-comment keepalive lines and empty deltas ignored, the
stream terminated on ``data: [DONE]``, and ``usage`` captured from any
chunk (the final one, when the provider sends it — null otherwise).

Failure classes (the retry policy is the worker's — never this module's):

- :class:`LLMHTTPError` — any non-2xx status, carrying the upstream status
  and a body snippet for the job's ``error.message``.
- :class:`LLMTimeout` — ``kind="idle"`` (socket read stalled past
  ``idle_timeout_sec``) or ``kind="total"`` (whole call past
  ``total_timeout_sec``).
- :class:`LLMConnectionError` — the host could not be reached at all.
"""

import json
import time

import requests


class LLMError(Exception):
	"""Base class for all LLM transport failures."""


class LLMHTTPError(LLMError):
	"""Non-2xx response from the LLM host (upstream status + body snippet)."""

	def __init__(self, status: int, body_snippet: str):
		self.status = status
		self.body_snippet = body_snippet
		super().__init__(f"HTTP {status}: {body_snippet}")


class LLMTimeout(LLMError):
	"""Idle (socket read) or total budget exceeded. ``kind`` is "idle" or "total"."""

	def __init__(self, kind: str):
		self.kind = kind
		super().__init__(f"{kind} timeout exceeded")


class LLMConnectionError(LLMError):
	"""The LLM host could not be reached (DNS/connect failure)."""


def stream_chat_completion(
	base_url: str,
	api_key: str,
	model: str,
	messages: list[dict],
	idle_timeout_sec: int = 120,
	total_timeout_sec: int = 600,
) -> tuple[str, dict | None]:
	"""Stream one chat completion and return ``(raw_text, usage)``.

	``raw_text`` is the accumulated ``delta.content``; ``usage`` is the
	provider's usage dict when sent, else ``None``. Timeouts are enforced
	with the ``requests`` per-read idle timeout (the socket-read idle) plus
	a per-line total budget check (a hung ``[DONE]``-less stream cannot run
	past the total).
	"""
	url = (base_url or "").rstrip("/") + "/chat/completions"
	headers = {
		"Authorization": f"Bearer {api_key or ''}",
		"Content-Type": "application/json",
	}
	payload = {"model": model, "temperature": 0, "stream": True, "messages": messages}

	try:
		# (connect, read): the read element IS the socket-read idle timeout
		# — a stalled read raises a timeout exception (below).
		response = requests.post(url, json=payload, headers=headers, stream=True, timeout=(10.0, idle_timeout_sec))
	except requests.exceptions.ConnectTimeout as exc:
		raise LLMConnectionError(f"connection to {url!r} timed out during connect") from exc
	except requests.exceptions.ConnectionError as exc:
		raise LLMConnectionError(f"cannot reach LLM host {url!r}: {exc}") from exc
	except requests.exceptions.ReadTimeout as exc:
		raise LLMTimeout("idle") from exc

	try:
		if response.status_code >= 400:
			body = response.content.decode("utf-8", "replace")[:300]
			raise LLMHTTPError(response.status_code, body)
		parts, usage = _read_sse_stream(response, total_timeout_sec)
	except requests.exceptions.ReadTimeout as exc:
		raise LLMTimeout("idle") from exc
	except requests.exceptions.ConnectionError as exc:
		# While streaming, requests re-raises urllib3's ReadTimeoutError as
		# a plain ConnectionError (see requests/models.py) — a stalled
		# socket read, i.e. the idle timeout.
		raise LLMTimeout("idle") from exc
	except requests.exceptions.RequestException as exc:
		# any other transport failure mid-stream (reset, bad framing) is a
		# connection-level failure, not a timeout
		raise LLMConnectionError(f"stream interrupted: {exc}") from exc
	finally:
		response.close()

	return "".join(parts), usage


def _read_sse_stream(response, total_timeout_sec: int) -> tuple[list[str], dict | None]:
	"""Accumulate ``delta.content`` from an SSE stream."""
	parts: list[str] = []
	usage = None
	deadline = time.monotonic() + total_timeout_sec
	for line in response.iter_lines(decode_unicode=True):
		if time.monotonic() > deadline:
			raise LLMTimeout("total")
		if not line or line.startswith(":"):
			continue  # empty lines separate events; ":" lines are keepalive comments
		if not line.startswith("data:"):
			continue
		data = line[len("data:"):].strip()
		if data == "[DONE]":
			break
		if not data:
			continue
		try:
			chunk = json.loads(data)
		except (json.JSONDecodeError, ValueError):
			continue
		if not isinstance(chunk, dict):
			continue
		chunk_usage = chunk.get("usage")
		if isinstance(chunk_usage, dict):
			usage = chunk_usage
		choices = chunk.get("choices")
		if not isinstance(choices, list):
			continue
		for choice in choices:
			delta = (choice or {}).get("delta") or {}
			content = delta.get("content")
			if content:
				parts.append(content)
	return parts, usage
