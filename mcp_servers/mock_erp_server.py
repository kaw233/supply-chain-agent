"""Streamable HTTP MCP server for the local, non-production ERP fixture."""
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from mock_erp_service import ExpediteStore


ROOT = Path(__file__).resolve().parents[1]
STORE = ExpediteStore(
    os.environ.get("MOCK_ERP_DB", ROOT / "data" / "mock-erp.sqlite3"),
    os.environ.get("MOCK_ERP_PREPARE_TTL_SECONDS", "900"),
)
mcp = FastMCP("supply-chain-mock-erp", json_response=True)


@mcp.tool()
def prepare_expedite(idempotency_key: str, material_id: str, material_name: str,
                     supplier: str, shortage_qty: float, due_days: int,
                     expected_object_version: str) -> dict:
    """Validate and persist a pending supplier expedite operation."""
    return STORE.prepare(idempotency_key, material_id, material_name, supplier,
                         shortage_qty, due_days, expected_object_version)


@mcp.tool()
def commit_expedite(prepare_token: str, current_object_version: str) -> dict:
    """Commit a prepared expedite after rechecking the business object version."""
    return STORE.commit(prepare_token, current_object_version)


@mcp.tool()
def rollback_expedite(prepare_token: str) -> dict:
    """Cancel a prepared expedite that has not been committed."""
    return STORE.rollback(prepare_token)


@mcp.tool()
def get_operation_status(operation_id: str = "", idempotency_key: str = "") -> dict:
    """Read the durable state of an expedite operation without changing it."""
    return STORE.status(operation_id, idempotency_key)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
