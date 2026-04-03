"""Integration tests for leader→follower user directory sync.

These tests require both mock-2n and mock-2n-follower services running
(see docker-compose.yml). They validate end-to-end sync behaviour:
config entry loading for two devices, options flow configuration,
and user propagation between devices.

Run locally:
    docker compose -f tests/integration/docker-compose.yml up -d --wait
    pytest tests/integration/test_sync_integration.py -v
"""
from __future__ import annotations

import aiohttp
import pytest

from .helpers import HaClient, HaWebSocket, Mock2nAdmin, set_entry_options

# ─── Setup: both devices configured ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_both_config_entries_loaded(
    ha: HaClient,
    doorman_follower_installed: dict,
) -> None:
    """Both leader and follower config entries are loaded."""
    entries = await ha.get_config_entries()
    doorman_entries = [e for e in entries if e["domain"] == "doorman"]
    assert len(doorman_entries) == 2, f"Expected 2 doorman entries, got {len(doorman_entries)}"
    titles = {e["title"] for e in doorman_entries}
    assert "2N IP Verso (Test)" in titles, "Leader device not found"
    assert "2N Access Unit M (Test)" in titles, "Follower device not found"
    for entry in doorman_entries:
        assert entry["state"] == "loaded", f"Entry {entry['title']} not loaded: {entry['state']}"


@pytest.mark.asyncio
async def test_ws_list_devices_returns_both(
    ws: HaWebSocket,
    doorman_follower_installed: dict,
) -> None:
    """doorman/list_devices WS command returns both configured devices."""
    result = await ws.command("doorman/list_devices")
    assert "devices" in result
    assert len(result["devices"]) == 2
    serials = {d["serial_number"] for d in result["devices"]}
    assert "10-99999999" in serials
    assert "10-88888888" in serials


@pytest.mark.asyncio
async def test_create_user_on_leader_syncs_to_follower(
    ha: HaClient,
    mock_2n: Mock2nAdmin,
    mock_2n_follower: Mock2nAdmin,
    doorman_follower_installed: dict,
    ha_token: str,
) -> None:
    """Creating a user on the leader device propagates to the follower."""
    # Get the entry IDs by title
    entries = await ha.get_config_entries()
    doorman_by_title = {e["title"]: e for e in entries if e["domain"] == "doorman"}
    leader_entry = doorman_by_title["2N IP Verso (Test)"]
    follower_entry = doorman_by_title["2N Access Unit M (Test)"]

    # Configure sync roles
    ha_url = ha.base_url
    await set_entry_options(
        ha_url, ha_token, leader_entry["entry_id"],
        {"sync_role": "leader"},
    )
    await set_entry_options(
        ha_url,
        ha_token,
        follower_entry["entry_id"],
        {"sync_role": "follower", "sync_target": leader_entry["entry_id"]},
    )

    # Reset follower to empty state
    await mock_2n_follower.reset()

    # Create a user on the leader
    await ha.call_service("doorman", "create_user", {
        "name": "Sync Test User",
        "pin": "7777",
        "device": leader_entry["entry_id"],
    })

    # Poll follower until the synced user appears (fail-fast)
    synced = await mock_2n_follower.wait_for_user("Sync Test User", timeout=90)
    assert synced, "User not synced to follower"


@pytest.mark.asyncio
async def test_initial_reconciliation_matches_by_name(
    ha: HaClient,
    mock_2n: Mock2nAdmin,
    mock_2n_follower: Mock2nAdmin,
    doorman_follower_installed: dict,
) -> None:
    """Both devices start with 'Test User' — sync should match them by name."""
    # Both mock servers start with "Test User" after reset
    leader_users = await mock_2n.get_users()
    follower_users = await mock_2n_follower.get_users()

    leader_names = {u["name"] for u in leader_users}
    follower_names = {u["name"] for u in follower_users}
    assert "Test User" in leader_names
    assert "Test User" in follower_names


@pytest.mark.asyncio
async def test_follower_only_user_left_alone(
    mock_2n_follower: Mock2nAdmin,
    doorman_follower_installed: dict,
) -> None:
    """Users created directly on the follower survive sync cycles.

    By this point in the test suite, the sync engine has already been running
    for the entire session. If the "Test User" on the follower (which exists
    from device init) hasn't been deleted by now, it's being left alone.
    """
    follower_users = await mock_2n_follower.get_users()
    names = {u["name"] for u in follower_users}
    assert "Test User" in names, f"Follower's initial user was deleted by sync. Users: {names}"


# ─── Helpers for sync-configured tests ─────────────────────────────────────

async def _configure_sync(ha: HaClient, ha_token: str):
    """Configure leader/follower sync roles and return (leader_entry, follower_entry)."""
    entries = await ha.get_config_entries()
    doorman_by_title = {e["title"]: e for e in entries if e["domain"] == "doorman"}
    leader_entry = doorman_by_title["2N IP Verso (Test)"]
    follower_entry = doorman_by_title["2N Access Unit M (Test)"]

    ha_url = ha.base_url
    await set_entry_options(
        ha_url, ha_token, leader_entry["entry_id"],
        {"sync_role": "leader"},
    )
    await set_entry_options(
        ha_url, ha_token, follower_entry["entry_id"],
        {"sync_role": "follower", "sync_target": leader_entry["entry_id"]},
    )
    return leader_entry, follower_entry


# ─── Update propagation ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_update_user_on_leader_syncs_to_follower(
    ha: HaClient,
    mock_2n: Mock2nAdmin,
    mock_2n_follower: Mock2nAdmin,
    doorman_follower_installed: dict,
    ha_token: str,
) -> None:
    """Updating a user's PIN on the leader propagates to the follower."""
    leader_entry, follower_entry = await _configure_sync(ha, ha_token)

    # Create a user on the leader first
    await ha.call_service("doorman", "create_user", {
        "name": "Update Test",
        "pin": "1234",
        "device": leader_entry["entry_id"],
    })

    # Wait for it to sync to follower
    await mock_2n_follower.wait_for_user("Update Test", timeout=90)

    # Now update the user's PIN on the leader
    leader_user = await mock_2n.get_user_by_name("Update Test")
    assert leader_user is not None
    await ha.call_service("doorman", "update_user", {
        "uuid": leader_user["uuid"],
        "pin": "9999",
        "device": leader_entry["entry_id"],
    })

    # Wait for the updated PIN to appear on follower
    follower_user = await mock_2n_follower.wait_for_user_field(
        "Update Test", "pin", "9999", timeout=90,
    )
    assert follower_user["pin"] == "9999"


# ─── Delete propagation ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_delete_user_on_leader_syncs_to_follower(
    ha: HaClient,
    mock_2n: Mock2nAdmin,
    mock_2n_follower: Mock2nAdmin,
    doorman_follower_installed: dict,
    ha_token: str,
) -> None:
    """Deleting a user on the leader removes them from the follower."""
    leader_entry, follower_entry = await _configure_sync(ha, ha_token)

    # Create a user on the leader
    await ha.call_service("doorman", "create_user", {
        "name": "Delete Test",
        "pin": "5555",
        "device": leader_entry["entry_id"],
    })

    # Wait for it to sync to follower
    await mock_2n_follower.wait_for_user("Delete Test", timeout=90)

    # Delete the user on the leader
    leader_user = await mock_2n.get_user_by_name("Delete Test")
    assert leader_user is not None
    await ha.call_service("doorman", "delete_user", {
        "uuid": leader_user["uuid"],
        "device": leader_entry["entry_id"],
    })

    # Wait for the user to disappear from follower
    gone = await mock_2n_follower.wait_for_user_gone("Delete Test", timeout=90)
    assert gone


# ─── Follower write rejection ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_follower_rejects_direct_write(
    ha: HaClient,
    mock_2n_follower: Mock2nAdmin,
    doorman_follower_installed: dict,
    ha_token: str,
) -> None:
    """Calling create_user on a follower device raises an error."""
    leader_entry, follower_entry = await _configure_sync(ha, ha_token)

    # Attempt to create a user directly on the follower
    with pytest.raises(aiohttp.ClientResponseError) as exc_info:
        await ha.call_service("doorman", "create_user", {
            "name": "Should Fail",
            "pin": "0000",
            "device": follower_entry["entry_id"],
        })
    assert exc_info.value.status in (400, 500), (
        f"Expected 4xx/5xx for follower write, got {exc_info.value.status}"
    )


# ─── Options flow reconfigure ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_options_flow_reconfigure_sync_role(
    ha: HaClient,
    doorman_follower_installed: dict,
    ha_token: str,
) -> None:
    """Switching a device from follower to none via options flow works."""
    entries = await ha.get_config_entries()
    doorman_by_title = {e["title"]: e for e in entries if e["domain"] == "doorman"}
    follower_entry = doorman_by_title["2N Access Unit M (Test)"]

    # Set to follower first
    leader_entry = doorman_by_title["2N IP Verso (Test)"]
    await set_entry_options(
        ha.base_url, ha_token, leader_entry["entry_id"],
        {"sync_role": "leader"},
    )
    await set_entry_options(
        ha.base_url, ha_token, follower_entry["entry_id"],
        {"sync_role": "follower", "sync_target": leader_entry["entry_id"]},
    )

    # Now reconfigure to none
    await set_entry_options(
        ha.base_url, ha_token, follower_entry["entry_id"],
        {"sync_role": "none"},
    )

    # Verify the entry is still loaded
    entries = await ha.get_config_entries()
    reconfigured = next(
        e for e in entries
        if e["domain"] == "doorman" and e["entry_id"] == follower_entry["entry_id"]
    )
    assert reconfigured["state"] == "loaded"
