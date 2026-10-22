"""Outbound network guard for vendor sandbox URLs (SSRF, CWE-918).

Vendor-supplied URLs (test environment / sandbox base URL) are only ever
contacted by the background runner, and only after passing this guard:

- ``https`` only, no embedded credentials, no fragments;
- port 443 unless the site config allows more (``hisi_sandbox_allowed_ports``);
- every resolved address must be public (no loopback, private, link-local,
  CGNAT, multicast, reserved or cloud-metadata ranges);
- optional host allowlist via site config ``hisi_sandbox_allowed_hosts``
  (exact host or ``.suffix`` entries).

Local development may set ``hisi_allow_private_sandbox: 1`` in site_config to
permit private addresses. Never enable it on a shared host.
"""

import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit

import frappe
from frappe import _

_BLOCKED_HOSTS = {"localhost", "metadata.google.internal", "metadata"}
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


class UnsafeURLError(frappe.ValidationError):
	pass


def _is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
	if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
		ip = ip.ipv4_mapped
	if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast:
		return False
	if ip.is_reserved or ip.is_unspecified:
		return False
	if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT:
		return False
	return ip.is_global


def _host_allowed(host: str) -> bool:
	allowed = frappe.conf.get("hisi_sandbox_allowed_hosts") or []
	if not allowed:
		return True
	for entry in allowed:
		entry = str(entry).lower().strip()
		if entry.startswith(".") and (host.endswith(entry) or host == entry[1:]):
			return True
		if host == entry:
			return True
	return False


def normalize_sandbox_url(url: str, *, resolve: bool = True) -> str:
	"""Validate a vendor sandbox URL and return its normalized form, or raise UnsafeURLError."""
	raw = (url or "").strip()
	try:
		parts = urlsplit(raw)
		port = parts.port
	except ValueError:
		raise UnsafeURLError(_("Enter a valid HTTPS URL for the test environment."))

	if parts.scheme.lower() != "https":
		raise UnsafeURLError(_("The test environment must use HTTPS."))
	if parts.username or parts.password:
		raise UnsafeURLError(_("Do not include credentials in the test environment URL."))
	host = (parts.hostname or "").lower().rstrip(".")
	if not host or host in _BLOCKED_HOSTS or host.endswith(".local") or host.endswith(".internal"):
		raise UnsafeURLError(_("The test environment host is not allowed."))

	allowed_ports = {443, *[int(p) for p in (frappe.conf.get("hisi_sandbox_allowed_ports") or [])]}
	if port is not None and port not in allowed_ports:
		raise UnsafeURLError(_("The test environment port is not allowed."))
	if not _host_allowed(host):
		raise UnsafeURLError(_("The test environment host is not on the approved sandbox allowlist."))

	if resolve and not frappe.conf.get("hisi_allow_private_sandbox"):
		try:
			infos = socket.getaddrinfo(host, port or 443, proto=socket.IPPROTO_TCP)
		except socket.gaierror:
			raise UnsafeURLError(_("The test environment host could not be resolved."))
		addresses = {ipaddress.ip_address(info[4][0]) for info in infos}
		if not addresses or not all(_is_public(ip) for ip in addresses):
			raise UnsafeURLError(_("The test environment must resolve to a public address."))

	netloc = host if port in (None, 443) else f"{host}:{port}"
	path = parts.path.rstrip("/")
	return urlunsplit(("https", netloc, path, parts.query, ""))


def probe_fhir_metadata(base_url: str, timeout: float = 8.0) -> dict:
	"""TLS + FHIR CapabilityStatement preflight against an already-validated URL.

	Re-validates (including DNS) immediately before connecting, never follows
	redirects and never forwards any HISI credential.
	"""
	import requests

	safe = normalize_sandbox_url(base_url)
	target = f"{safe}/metadata"
	try:
		response = requests.get(
			target,
			headers={"Accept": "application/fhir+json", "User-Agent": "HISI-Preflight/1.0"},
			timeout=timeout,
			allow_redirects=False,
			verify=True,
			stream=True,
		)
	except requests.exceptions.SSLError:
		return {"ok": False, "tls": False, "detail": "TLS verification failed"}
	except requests.exceptions.RequestException as exc:
		return {"ok": False, "tls": None, "detail": f"Connection failed: {type(exc).__name__}"}

	try:
		body = response.raw.read(256_000, decode_content=True)
	finally:
		response.close()

	result = {"ok": response.status_code < 500, "tls": True, "status": response.status_code, "fhir": None}
	if response.status_code == 200:
		try:
			payload = frappe.parse_json(body.decode("utf-8", "replace"))
		except Exception:
			payload = None
		if isinstance(payload, dict) and payload.get("resourceType") == "CapabilityStatement":
			result["fhir"] = str(payload.get("fhirVersion") or "unknown")
	return result
