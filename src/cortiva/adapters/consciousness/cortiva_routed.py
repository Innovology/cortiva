"""
Cortiva-routed consciousness adapter.

Instead of calling a model provider directly, this adapter posts to the
Cortiva HQ inference relay (``POST /api/inference/call``). HQ resolves a
serving node, mints a short-lived grant, dispatches the inference via
the existing WebSocket command channel, and returns the result.

Configure in cortiva.yaml:

    consciousness:
      provider: cortiva-routed
      hq_base_url: https://api.cortiva.dev
      node_token: ctv_node_...        # the calling node's auth token
      model_id: qwen3.6-35b-a3b
      version: 2026.04.16             # optional; HQ picks one if omitted

The adapter exposes the same ``ConsciousnessAdapter`` protocol as the
direct providers, so it slots into the consciousness router alongside
Anthropic / OpenAI without code changes elsewhere.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from cortiva.adapters.protocols import ConsciousResponse, Priority

logger = logging.getLogger(__name__)

REFLECTION_SUFFIX_INSTRUCTIONS = """\

After completing the task, you may optionally append a structured reflection \
suffix to your response. Place it after your main response, separated by the \
exact delimiter line shown below. The suffix must be valid JSON.

---REFLECTION---
{
  "outcome": "One-sentence summary of what you accomplished",
  "learned": "Key insight or lesson from this task (stored as a memory)",
  "prediction_error": "What surprised you or differed from expectations",
  "procedure_update": "New or revised procedure step to add to your procedures",
  "messages": [{"to": "agent-id", "content": "message body"}],
  "escalation": "Issue requiring human or supervisor attention"
}

All fields are optional — include only those that apply. \
Do NOT include the reflection suffix if you have nothing meaningful to report."""


class CortivaRoutedConsciousnessAdapter:
    """Consciousness adapter that proxies through Cortiva HQ.

    All real model selection, authorisation, and audit logging live on
    HQ. The adapter just forwards the prompt and returns the result.
    """

    def __init__(
        self,
        model_id: str,
        *,
        version: str | None = None,
        hq_base_url: str | None = None,
        node_token: str | None = None,
        max_tokens: int = 4096,
        timeout_s: float = 120.0,
        prefer_direct: bool = False,
        client_cert_pem: str | None = None,
        client_key_pem: str | None = None,
        ca_cert_pem: str | None = None,
    ) -> None:
        self.model_id = model_id
        self.version = version
        self.max_tokens = max_tokens
        self.timeout_s = timeout_s
        self._hq_base_url = (
            hq_base_url
            or os.environ.get("CORTIVA_HQ_BASE_URL")
            or "https://api.cortiva.dev"
        ).rstrip("/")
        self._node_token = node_token or os.environ.get("CORTIVA_NODE_TOKEN", "")
        if not self._node_token:
            raise ValueError(
                "cortiva-routed adapter requires a node_token (or "
                "CORTIVA_NODE_TOKEN env var) to authenticate to HQ.",
            )
        # Direct-data-plane configuration. When prefer_direct is set the
        # adapter calls /api/inference/route, then dispatches the inference
        # straight to the serving node's /v1/infer endpoint with the grant
        # in the Authorization header. Falls back to /api/inference/call
        # on any failure (network, 5xx, missing endpoint).
        self.prefer_direct = prefer_direct
        self._client_cert_pem = client_cert_pem
        self._client_key_pem = client_key_pem
        self._ca_cert_pem = ca_cert_pem

    @property
    def model(self) -> str:
        # Compatibility with the existing protocol — used as a label
        # in audit logs and ConsciousResponse.
        return f"{self.model_id}@{self.version or 'latest'}"

    async def think(
        self,
        agent_id: str,
        context: str,
        prompt: str,
        *,
        priority: Priority = Priority.NORMAL,
        max_tokens: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ConsciousResponse:
        call_type = (metadata or {}).get("call_type", "execute")
        system_prompt = (
            "You are an autonomous agent in an organisation. "
            "Your identity, skills, responsibilities, and current state "
            "are provided in the context below. Act as this agent — "
            "make decisions, complete tasks, and communicate as them.\n\n"
            f"{context}"
        )
        effective_prompt = prompt
        if metadata and metadata.get("task_execution"):
            effective_prompt = prompt + "\n\n" + REFLECTION_SUFFIX_INSTRUCTIONS

        body = {
            "agent_id": agent_id,
            "call_type": call_type,
            "model_id": self.model_id,
            "prompt": effective_prompt,
            "system": system_prompt,
            "max_tokens": max_tokens or self.max_tokens,
        }
        if self.version:
            body["version"] = self.version

        if self.prefer_direct:
            try:
                data = await self._post_inference_direct(body)
                return self._build_response(
                    agent_id, priority, data, via="cortiva-direct",
                )
            except Exception as exc:
                logger.warning(
                    "Direct path failed (%s); falling back to relay.", exc,
                )

        data = await self._post_inference(body)
        return self._build_response(agent_id, priority, data, via="cortiva-routed")

    def _build_response(
        self, agent_id: str, priority: Priority, data: dict, *, via: str,
    ) -> ConsciousResponse:
        return ConsciousResponse(
            content=data.get("text", ""),
            tokens_in=int(data.get("tokens_in") or 0),
            tokens_out=int(data.get("tokens_out") or 0),
            model=f"{data.get('model_id')}@{data.get('version')}",
            metadata={
                "agent_id": agent_id,
                "priority": priority.value,
                "serving_node_id": data.get("serving_node_id"),
                "grant_jti": data.get("grant_jti"),
                "latency_ms": data.get("latency_ms"),
                "via": via,
            },
        )

    async def reflect(
        self,
        agent_id: str,
        context: str,
        day_summary: str,
    ) -> ConsciousResponse:
        prompt = (
            "Your working day is ending. Here is a summary of what happened:\n\n"
            f"{day_summary}\n\n"
            "Reflect on the day and update your Living Summary."
        )
        return await self.think(
            agent_id=agent_id,
            context=context,
            prompt=prompt,
            metadata={"call_type": "reflect"},
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _post_inference(self, body: dict) -> dict:
        # httpx is a soft dependency — imported here so the framework can
        # still be installed in environments that don't use this adapter.
        import httpx

        url = f"{self._hq_base_url}/api/inference/call"
        headers = {"Authorization": f"Bearer {self._node_token}"}
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            resp = await client.post(url, json=body, headers=headers)
            if resp.status_code != 200:
                raise RuntimeError(
                    f"Inference call to HQ failed ({resp.status_code}): "
                    f"{resp.text[:300]}",
                )
            return resp.json()

    async def _post_inference_direct(self, body: dict) -> dict:
        """Direct path: ask HQ to mint a grant + endpoint, then call the
        serving node directly with that grant. Raises on any failure so
        the caller can fall back to the relay path."""
        import httpx

        # 1. Get routing decision + grant.
        route_url = f"{self._hq_base_url}/api/inference/route"
        route_body = {
            "agent_id": body["agent_id"],
            "call_type": body["call_type"],
            "model_id": body["model_id"],
            "max_tokens": body.get("max_tokens", self.max_tokens),
        }
        if body.get("version"):
            route_body["version"] = body["version"]

        headers = {"Authorization": f"Bearer {self._node_token}"}
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            resp = await client.post(route_url, json=route_body, headers=headers)
            if resp.status_code != 200:
                raise RuntimeError(
                    f"Routing decision failed ({resp.status_code}): "
                    f"{resp.text[:300]}",
                )
            decision = resp.json()

        endpoint = decision.get("endpoint") or {}
        if not endpoint.get("host"):
            raise RuntimeError("Routing returned no direct endpoint")

        scheme = endpoint.get("scheme", "https")
        host = endpoint["host"]
        port = int(endpoint.get("port") or 443)
        # We always target /v1/infer for direct calls — endpoint.path may
        # point at the runtime's OpenAI server, which we don't talk to
        # directly because it doesn't enforce grants.
        target = f"{scheme}://{host}:{port}/v1/infer"

        # 2. Build the request body (grant goes in Authorization).
        infer_body = {
            "model_id": decision["model_id"],
            "version": decision["version"],
            "prompt": body["prompt"],
            "system": body.get("system"),
            "max_tokens": body.get("max_tokens", self.max_tokens),
            "temperature": body.get("temperature"),
            "stop": body.get("stop"),
        }
        infer_headers = {"Authorization": f"Bearer {decision['grant']}"}

        # 3. Optional mTLS materials.
        cert_arg = self._build_cert_arg()
        verify = self._build_verify_arg()

        async with httpx.AsyncClient(
            timeout=self.timeout_s, cert=cert_arg, verify=verify,
        ) as client:
            resp = await client.post(target, json=infer_body, headers=infer_headers)
            if resp.status_code != 200:
                raise RuntimeError(
                    f"Direct inference failed ({resp.status_code}): "
                    f"{resp.text[:300]}",
                )
            data = resp.json()

        # Re-shape to match _post_inference output.
        return {
            **data,
            "model_id": decision["model_id"],
            "version": decision["version"],
            "serving_node_id": decision["node_id"],
            # No grant_jti available client-side without parsing the JWT;
            # leave None and let the audit trail on the serving node carry it.
            "grant_jti": None,
        }

    def _build_cert_arg(self):
        """Return the `cert` argument for httpx.AsyncClient when mTLS
        client-side material is configured, else None."""
        if not (self._client_cert_pem and self._client_key_pem):
            return None
        # httpx accepts cert as a tuple of (cert_path, key_path) — write
        # PEM bodies to a tempdir at first use.
        import os
        import tempfile
        cert_dir = tempfile.gettempdir()
        cert_path = os.path.join(cert_dir, "cortiva_client_cert.pem")
        key_path = os.path.join(cert_dir, "cortiva_client_key.pem")
        if not os.path.exists(cert_path):
            with open(cert_path, "w") as f:
                f.write(self._client_cert_pem)
        if not os.path.exists(key_path):
            with open(key_path, "w") as f:
                f.write(self._client_key_pem)
            os.chmod(key_path, 0o600)
        return (cert_path, key_path)

    def _build_verify_arg(self):
        """Return the `verify` argument for httpx — pinned to the CA root
        when one is configured, else True (system trust store)."""
        if not self._ca_cert_pem:
            return True
        import os
        import tempfile
        ca_path = os.path.join(tempfile.gettempdir(), "cortiva_ca_cert.pem")
        if not os.path.exists(ca_path):
            with open(ca_path, "w") as f:
                f.write(self._ca_cert_pem)
        return ca_path
