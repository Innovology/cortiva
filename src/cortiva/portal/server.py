"""
Cortiva Portal Server — FastAPI application.

Provides REST API and WebSocket endpoints for the web portal.
Connects to the fabric via IPC for commands, reads agent state from
the filesystem.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from cortiva.portal.auth import AuthDB, JWTError, Role, User


def create_app(
    agents_dir: str | Path = "./agents",
    db_path: str | Path = ".cortiva/portal.db",
) -> Any:
    """Create and configure the FastAPI application.

    Returns the app object. FastAPI is imported lazily to keep it optional.
    """
    try:
        from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
        from fastapi.middleware.cors import CORSMiddleware
        from fastapi.responses import JSONResponse
        from pydantic import BaseModel
    except ImportError:
        raise ImportError(
            "FastAPI is required for the portal. "
            "Install with: pip install 'fastapi[standard]'"
        )

    agents_path = Path(agents_dir)
    auth_db = AuthDB(db_path)

    app = FastAPI(title="Cortiva Portal", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Store for WebSocket connections
    ws_connections: list[WebSocket] = []

    # ----- Request models -----

    class LoginRequest(BaseModel):
        email: str
        password: str

    class BootstrapRequest(BaseModel):
        email: str
        password: str
        name: str
        org_name: str = ""
        org_industry: str = ""
        org_timezone: str = "UTC"

    class RefreshRequest(BaseModel):
        refresh_token: str

    class InviteRequest(BaseModel):
        email: str
        name: str
        role: str = "observer"
        password: str

    class SnapshotRequest(BaseModel):
        name: str = ""
        description: str = ""

    # ----- Auth dependency -----

    async def get_current_user(authorization: str = "") -> User:
        """Extract and verify JWT from Authorization header."""
        if not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing authorization")
        token = authorization[7:]
        user = auth_db.verify_access_token(token)
        if user is None:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        return user

    def require_role(min_role: Role):
        async def _check(user: User = Depends(get_current_user)) -> User:
            if not user.role.can_do(min_role):
                raise HTTPException(status_code=403, detail="Insufficient permissions")
            return user
        return _check

    # ----- Auth endpoints -----

    @app.post("/api/auth/bootstrap")
    async def bootstrap(req: BootstrapRequest):
        if auth_db.has_users():
            raise HTTPException(status_code=400, detail="Bootstrap already complete")
        user = auth_db.create_user(req.email, req.name, req.password, role="owner")
        if req.org_name:
            auth_db.set_org_setting("org_name", req.org_name)
        if req.org_industry:
            auth_db.set_org_setting("org_industry", req.org_industry)
        auth_db.set_org_setting("org_timezone", req.org_timezone)
        auth_db.mark_bootstrap_complete()
        tokens = auth_db.issue_tokens(user)
        auth_db.audit(user.id, "bootstrap", details=f"org={req.org_name}")
        return {
            "user": user.to_dict(),
            "access_token": tokens.access_token,
            "refresh_token": tokens.refresh_token,
            "expires_in": tokens.expires_in,
        }

    @app.post("/api/auth/login")
    async def login(req: LoginRequest):
        user = auth_db.verify_credentials(req.email, req.password)
        if user is None:
            raise HTTPException(status_code=401, detail="Invalid credentials")
        tokens = auth_db.issue_tokens(user)
        auth_db.audit(user.id, "login")
        return {
            "user": user.to_dict(),
            "access_token": tokens.access_token,
            "refresh_token": tokens.refresh_token,
            "expires_in": tokens.expires_in,
        }

    @app.post("/api/auth/refresh")
    async def refresh(req: RefreshRequest):
        tokens = auth_db.refresh_access_token(req.refresh_token)
        if tokens is None:
            raise HTTPException(status_code=401, detail="Invalid refresh token")
        return {
            "access_token": tokens.access_token,
            "refresh_token": tokens.refresh_token,
            "expires_in": tokens.expires_in,
        }

    @app.post("/api/auth/invite")
    async def invite(req: InviteRequest, user: User = Depends(require_role(Role.ADMIN))):
        try:
            new_user = auth_db.create_user(req.email, req.name, req.password, role=req.role)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        auth_db.audit(user.id, "invite", target=new_user.id, details=f"role={req.role}")
        return new_user.to_dict()

    @app.get("/api/auth/users")
    async def list_users(user: User = Depends(require_role(Role.ADMIN))):
        return [u.to_dict() for u in auth_db.list_users()]

    @app.get("/api/auth/me")
    async def get_me(user: User = Depends(get_current_user)):
        return user.to_dict()

    @app.get("/api/auth/needs-bootstrap")
    async def needs_bootstrap():
        return {"needs_bootstrap": not auth_db.has_users()}

    # ----- Agent endpoints -----

    def _read_agent_state(agent_id: str) -> dict[str, Any]:
        """Read agent state from filesystem."""
        agent_dir = agents_path / agent_id
        if not agent_dir.is_dir():
            return {}

        state: dict[str, Any] = {"id": agent_id}

        # Read identity files
        for key in ("identity", "soul", "skills", "responsibilities", "procedures"):
            path = agent_dir / "identity" / f"{key}.md"
            if path.exists():
                state[key] = path.read_text(encoding="utf-8")

        # Read today's plan
        plan_path = agent_dir / "today" / "plan.md"
        if plan_path.exists():
            state["plan"] = plan_path.read_text(encoding="utf-8")

        return state

    @app.get("/api/agents")
    async def list_agents(user: User = Depends(get_current_user)):
        if not agents_path.is_dir():
            return []
        agents = []
        for p in sorted(agents_path.iterdir()):
            if p.is_dir() and not p.name.startswith("."):
                identity_path = p / "identity" / "identity.md"
                has_identity = identity_path.exists()
                agents.append({
                    "id": p.name,
                    "has_identity": has_identity,
                    "identity_preview": (
                        identity_path.read_text(encoding="utf-8")[:200]
                        if has_identity else ""
                    ),
                })
        return agents

    @app.get("/api/agents/{agent_id}")
    async def get_agent(agent_id: str, user: User = Depends(get_current_user)):
        state = _read_agent_state(agent_id)
        if not state:
            raise HTTPException(status_code=404, detail="Agent not found")
        return state

    @app.get("/api/agents/{agent_id}/identity/{file_key}")
    async def get_identity_file(agent_id: str, file_key: str, user: User = Depends(get_current_user)):
        path = agents_path / agent_id / "identity" / f"{file_key}.md"
        if not path.exists():
            raise HTTPException(status_code=404, detail="File not found")
        return {"content": path.read_text(encoding="utf-8")}

    @app.put("/api/agents/{agent_id}/identity/{file_key}")
    async def update_identity_file(
        agent_id: str,
        file_key: str,
        body: dict[str, str],
        user: User = Depends(require_role(Role.ADMIN)),
    ):
        agent_dir = agents_path / agent_id
        if not agent_dir.is_dir():
            raise HTTPException(status_code=404, detail="Agent not found")

        content = body.get("content", "")

        # Auto-snapshot before edit
        from cortiva.core.snapshots import create_snapshot
        create_snapshot(agent_dir, name=f"pre-edit-{file_key}", trigger="pre-edit")

        path = agent_dir / "identity" / f"{file_key}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

        auth_db.audit(user.id, "edit_identity", target=f"{agent_id}/{file_key}")
        return {"ok": True}

    @app.get("/api/agents/{agent_id}/journal")
    async def get_journal(
        agent_id: str,
        limit: int = 30,
        user: User = Depends(get_current_user),
    ):
        journal_dir = agents_path / agent_id / "journal"
        if not journal_dir.is_dir():
            return []
        entries = []
        for path in sorted(journal_dir.iterdir(), reverse=True)[:limit]:
            if path.suffix == ".md":
                entries.append({
                    "date": path.stem,
                    "content": path.read_text(encoding="utf-8"),
                })
        return entries

    @app.get("/api/agents/{agent_id}/metrics")
    async def get_metrics(agent_id: str, user: User = Depends(get_current_user)):
        agent_dir = agents_path / agent_id
        if not agent_dir.is_dir():
            raise HTTPException(status_code=404, detail="Agent not found")

        metrics: dict[str, Any] = {"agent_id": agent_id}
        for fname in ("task_queue.json", "familiarity_signals.json", "exception_pile.json"):
            path = agent_dir / "today" / fname
            if path.exists():
                try:
                    metrics[fname.replace(".json", "")] = json.loads(
                        path.read_text(encoding="utf-8")
                    )
                except json.JSONDecodeError:
                    pass
        return metrics

    # ----- Agent commands (via IPC) -----

    def _send_ipc(command: str, **kwargs: Any) -> dict[str, Any]:
        from cortiva.core.ipc import FabricClient
        client = FabricClient()
        if not client.is_daemon_running():
            return {"ok": False, "error": "Fabric daemon not running"}
        try:
            return client.send_sync(command, **kwargs)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @app.post("/api/agents/{agent_id}/wake")
    async def wake_agent(agent_id: str, user: User = Depends(require_role(Role.MANAGER))):
        result = _send_ipc("agent.wake", agent_id=agent_id)
        auth_db.audit(user.id, "wake_agent", target=agent_id)
        if not result.get("ok"):
            raise HTTPException(status_code=500, detail=result.get("error", "Failed"))
        return result

    @app.post("/api/agents/{agent_id}/sleep")
    async def sleep_agent(agent_id: str, user: User = Depends(require_role(Role.MANAGER))):
        result = _send_ipc("agent.sleep", agent_id=agent_id)
        auth_db.audit(user.id, "sleep_agent", target=agent_id)
        if not result.get("ok"):
            raise HTTPException(status_code=500, detail=result.get("error", "Failed"))
        return result

    # ----- Snapshot endpoints -----

    @app.get("/api/agents/{agent_id}/snapshots")
    async def agent_snapshots(agent_id: str, user: User = Depends(get_current_user)):
        from cortiva.core.snapshots import list_snapshots
        agent_dir = agents_path / agent_id
        if not agent_dir.is_dir():
            raise HTTPException(status_code=404, detail="Agent not found")
        return [s.to_dict() for s in list_snapshots(agent_dir)]

    @app.post("/api/agents/{agent_id}/snapshot")
    async def create_agent_snapshot(
        agent_id: str,
        req: SnapshotRequest,
        user: User = Depends(require_role(Role.ADMIN)),
    ):
        from cortiva.core.snapshots import create_snapshot
        agent_dir = agents_path / agent_id
        if not agent_dir.is_dir():
            raise HTTPException(status_code=404, detail="Agent not found")
        meta = create_snapshot(agent_dir, name=req.name, description=req.description)
        auth_db.audit(user.id, "create_snapshot", target=agent_id)
        return meta.to_dict()

    @app.post("/api/agents/{agent_id}/rollback")
    async def rollback_agent(
        agent_id: str,
        body: dict[str, Any],
        user: User = Depends(require_role(Role.ADMIN)),
    ):
        from cortiva.core.snapshots import restore_snapshot
        agent_dir = agents_path / agent_id
        if not agent_dir.is_dir():
            raise HTTPException(status_code=404, detail="Agent not found")
        snapshot_id = body.get("snapshot_id", "")
        if not snapshot_id:
            raise HTTPException(status_code=400, detail="snapshot_id required")
        if restore_snapshot(agent_dir, snapshot_id):
            auth_db.audit(user.id, "rollback", target=f"{agent_id}/{snapshot_id}")
            return {"ok": True}
        raise HTTPException(status_code=404, detail="Snapshot not found")

    @app.post("/api/agents/{agent_id}/clone")
    async def clone_agent(
        agent_id: str,
        body: dict[str, Any],
        user: User = Depends(require_role(Role.ADMIN)),
    ):
        from cortiva.core.snapshots import clone_from_snapshot, list_snapshots
        agent_dir = agents_path / agent_id
        if not agent_dir.is_dir():
            raise HTTPException(status_code=404, detail="Agent not found")

        new_id = body.get("new_agent_id", "")
        if not new_id:
            raise HTTPException(status_code=400, detail="new_agent_id required")
        new_dir = agents_path / new_id
        if new_dir.exists():
            raise HTTPException(status_code=400, detail=f"Agent {new_id} already exists")

        snapshot_id = body.get("snapshot_id", "latest")
        if snapshot_id == "latest":
            snaps = list_snapshots(agent_dir)
            if not snaps:
                raise HTTPException(status_code=400, detail="No snapshots available")
            snapshot_id = snaps[0].snapshot_id

        if clone_from_snapshot(agent_dir, snapshot_id, new_dir):
            auth_db.audit(user.id, "clone", target=f"{agent_id}->{new_id}")
            return {"ok": True, "new_agent_id": new_id}
        raise HTTPException(status_code=404, detail="Snapshot not found")

    # ----- Cluster & Budget -----

    @app.get("/api/cluster")
    async def cluster_status(user: User = Depends(get_current_user)):
        return _send_ipc("cluster.status")

    @app.get("/api/budget")
    async def budget_status(user: User = Depends(get_current_user)):
        return _send_ipc("budget")

    # ----- Audit -----

    @app.get("/api/audit")
    async def audit_log(
        limit: int = 100,
        offset: int = 0,
        user: User = Depends(require_role(Role.ADMIN)),
    ):
        return auth_db.get_audit_log(limit=limit, offset=offset)

    # ----- WebSocket feed -----

    @app.websocket("/ws/feed")
    async def ws_feed(websocket: WebSocket):
        await websocket.accept()
        ws_connections.append(websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            ws_connections.remove(websocket)

    # ----- Plugin system -----

    app._cortiva_plugins = []  # type: ignore[attr-defined]

    def register_plugin(
        name: str,
        routes: Any = None,
        nav_items: list[dict[str, str]] | None = None,
    ) -> None:
        if routes:
            app.include_router(routes, prefix=f"/api/plugins/{name}")
        app._cortiva_plugins.append({  # type: ignore[attr-defined]
            "name": name,
            "nav_items": nav_items or [],
        })

    app.register_plugin = register_plugin  # type: ignore[attr-defined]

    @app.get("/api/plugins")
    async def list_plugins():
        return app._cortiva_plugins  # type: ignore[attr-defined]

    return app
