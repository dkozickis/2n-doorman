"""Tests for the Doorman leader→follower user sync engine."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.doorman.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_SYNC_ROLE,
    CONF_SYNC_TARGET,
    CONF_USERNAME,
    DOMAIN,
    SYNC_ROLE_FOLLOWER,
    SYNC_ROLE_LEADER,
)
from custom_components.doorman.storage import DoormanStore
from custom_components.doorman.sync import UserSyncManager

from .conftest import MOCK_DEVICE_INFO, MOCK_SWITCHES

# ─── Helpers ─────────────────────────────────────────────────────────────────

LEADER_USERS = [
    {"uuid": "L1", "name": "Alice", "pin": "1111", "card": ["AA"], "code": [], "validFrom": None, "validTo": None},
    {"uuid": "L2", "name": "Bob", "pin": "2222", "card": [], "code": ["99"], "validFrom": None, "validTo": None},
]

FOLLOWER_USERS = [
    {"uuid": "F1", "name": "Alice", "pin": "0000", "card": ["AA"], "code": [], "validFrom": None, "validTo": None},
]

LEADER_ENTRY_ID = "leader_entry_id"
FOLLOWER_ENTRY_ID = "follower_entry_id"
PAIR_ID = LEADER_ENTRY_ID


def _make_coordinator(entry_id, users, device_info=None):
    """Create a minimal mock coordinator."""
    coord = MagicMock()
    coord.data = {"users": list(users), "switches": MOCK_SWITCHES, "log_events": []}
    coord.device_info = device_info or dict(MOCK_DEVICE_INFO)
    coord.client = MagicMock()
    coord.client.create_user = AsyncMock(side_effect=lambda u: {"uuid": f"F-new-{u['name']}", **u})
    coord.client.update_user = AsyncMock(return_value=None)
    coord.client.delete_user = AsyncMock(return_value=None)
    coord.async_request_refresh = AsyncMock()
    coord.config_entry = MagicMock()
    coord.config_entry.entry_id = entry_id
    coord.async_add_listener = MagicMock(return_value=MagicMock())
    return coord


def _make_entries(hass, leader_coord, follower_coord):
    """Store coordinators in hass.data and create config entries."""
    hass.data.setdefault(DOMAIN, {})[LEADER_ENTRY_ID] = leader_coord
    hass.data[DOMAIN][FOLLOWER_ENTRY_ID] = follower_coord

    leader_entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id=LEADER_ENTRY_ID,
        data={CONF_HOST: "10.0.0.1", CONF_USERNAME: "admin", CONF_PASSWORD: "pass"},
        options={CONF_SYNC_ROLE: SYNC_ROLE_LEADER},
    )
    follower_entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id=FOLLOWER_ENTRY_ID,
        data={CONF_HOST: "10.0.0.2", CONF_USERNAME: "admin", CONF_PASSWORD: "pass"},
        options={CONF_SYNC_ROLE: SYNC_ROLE_FOLLOWER, CONF_SYNC_TARGET: LEADER_ENTRY_ID},
    )
    leader_entry.add_to_hass(hass)
    follower_entry.add_to_hass(hass)


@pytest.fixture
def store(hass):
    """Return a DoormanStore backed by an in-memory mock."""
    s = DoormanStore(hass)
    s._data = {"user_links": {}, "notification_targets": {}, "sync_mappings": {}}
    s._store = MagicMock()
    s._store.async_save = AsyncMock()
    return s


# ─── Tests ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_initial_reconcile_matches_by_name(hass: HomeAssistant, store) -> None:
    """Users with the same name on both devices are paired and follower updated."""
    leader = _make_coordinator(LEADER_ENTRY_ID, LEADER_USERS)
    follower = _make_coordinator(FOLLOWER_ENTRY_ID, FOLLOWER_USERS)
    _make_entries(hass, leader, follower)

    sync = UserSyncManager(hass, store)
    await sync._initial_reconcile(leader, follower, PAIR_ID)

    # Alice matched by name → mapping created
    pair_mappings = store.sync_mappings_for(PAIR_ID)
    assert pair_mappings.get("L1") == "F1"
    # Alice's PIN updated from leader ("1111") since follower had "0000"
    follower.client.update_user.assert_called_once()
    update_call = follower.client.update_user.call_args[0][0]
    assert update_call["uuid"] == "F1"
    assert update_call["pin"] == "1111"

    # Bob had no match → created on follower
    follower.client.create_user.assert_called_once()
    create_call = follower.client.create_user.call_args[0][0]
    assert create_call["name"] == "Bob"
    assert "L2" in pair_mappings


@pytest.mark.asyncio
async def test_reconcile_creates_new_users(hass: HomeAssistant, store) -> None:
    """New users on leader (not in mappings) are created on follower."""
    leader = _make_coordinator(LEADER_ENTRY_ID, LEADER_USERS)
    follower = _make_coordinator(FOLLOWER_ENTRY_ID, [])
    _make_entries(hass, leader, follower)

    sync = UserSyncManager(hass, store)
    sync._initialized = True
    await sync._reconcile(leader, follower, PAIR_ID)

    assert follower.client.create_user.call_count == 2
    assert len(store.sync_mappings_for(PAIR_ID)) == 2


@pytest.mark.asyncio
async def test_reconcile_updates_changed_fields(hass: HomeAssistant, store) -> None:
    """Changed fields on leader propagate to follower."""
    leader_users = [{"uuid": "L1", "name": "Alice New", "pin": "9999", "card": ["BB"], "code": [], "validFrom": None, "validTo": None}]
    follower_users = [{"uuid": "F1", "name": "Alice", "pin": "1111", "card": ["AA"], "code": [], "validFrom": None, "validTo": None}]

    leader = _make_coordinator(LEADER_ENTRY_ID, leader_users)
    follower = _make_coordinator(FOLLOWER_ENTRY_ID, follower_users)
    _make_entries(hass, leader, follower)

    store._data["sync_mappings"][PAIR_ID] = {"L1": "F1"}

    sync = UserSyncManager(hass, store)
    sync._initialized = True
    await sync._reconcile(leader, follower, PAIR_ID)

    follower.client.update_user.assert_called_once()
    update = follower.client.update_user.call_args[0][0]
    assert update["uuid"] == "F1"
    assert update["name"] == "Alice New"
    assert update["pin"] == "9999"
    assert update["card"] == ["BB"]


@pytest.mark.asyncio
async def test_reconcile_deletes_removed_users(hass: HomeAssistant, store) -> None:
    """Users deleted from leader are deleted from follower and mapping removed."""
    leader = _make_coordinator(LEADER_ENTRY_ID, [])  # Alice removed
    follower_users = [{"uuid": "F1", "name": "Alice", "pin": "1111", "card": [], "code": [], "validFrom": None, "validTo": None}]
    follower = _make_coordinator(FOLLOWER_ENTRY_ID, follower_users)
    _make_entries(hass, leader, follower)

    store._data["sync_mappings"][PAIR_ID] = {"L1": "F1"}

    sync = UserSyncManager(hass, store)
    sync._initialized = True
    await sync._reconcile(leader, follower, PAIR_ID)

    follower.client.delete_user.assert_called_once_with("F1")
    assert "L1" not in store.sync_mappings_for(PAIR_ID)


@pytest.mark.asyncio
async def test_reconcile_no_changes_when_identical(hass: HomeAssistant, store) -> None:
    """No API calls when leader and follower are in sync."""
    users = [{"uuid": "L1", "name": "Alice", "pin": "1111", "card": ["AA"], "code": [], "validFrom": None, "validTo": None}]
    follower_users = [{"uuid": "F1", "name": "Alice", "pin": "1111", "card": ["AA"], "code": [], "validFrom": None, "validTo": None}]

    leader = _make_coordinator(LEADER_ENTRY_ID, users)
    follower = _make_coordinator(FOLLOWER_ENTRY_ID, follower_users)
    _make_entries(hass, leader, follower)

    store._data["sync_mappings"][PAIR_ID] = {"L1": "F1"}

    sync = UserSyncManager(hass, store)
    sync._initialized = True
    await sync._reconcile(leader, follower, PAIR_ID)

    follower.client.create_user.assert_not_called()
    follower.client.update_user.assert_not_called()
    follower.client.delete_user.assert_not_called()
    follower.async_request_refresh.assert_not_called()


@pytest.mark.asyncio
async def test_unmanaged_follower_users_left_alone(hass: HomeAssistant, store) -> None:
    """Users that only exist on the follower (not in any mapping) are not deleted."""
    leader = _make_coordinator(LEADER_ENTRY_ID, [])
    follower_users = [{"uuid": "F-local", "name": "Local User", "pin": "0000", "card": [], "code": [], "validFrom": None, "validTo": None}]
    follower = _make_coordinator(FOLLOWER_ENTRY_ID, follower_users)
    _make_entries(hass, leader, follower)

    sync = UserSyncManager(hass, store)
    sync._initialized = True
    await sync._reconcile(leader, follower, PAIR_ID)

    follower.client.delete_user.assert_not_called()


@pytest.mark.asyncio
async def test_reentrance_guard_skips_concurrent_sync(hass: HomeAssistant, store) -> None:
    """A second sync attempt is skipped while the first is still running."""
    leader = _make_coordinator(LEADER_ENTRY_ID, LEADER_USERS)
    follower = _make_coordinator(FOLLOWER_ENTRY_ID, [])
    _make_entries(hass, leader, follower)

    sync = UserSyncManager(hass, store)
    # Acquire the lock to simulate in-progress sync
    await sync._lock.acquire()

    await sync._on_leader_update(LEADER_ENTRY_ID, FOLLOWER_ENTRY_ID)

    # No API calls should have been made
    follower.client.create_user.assert_not_called()
    sync._lock.release()


@pytest.mark.asyncio
async def test_follower_offline_skips_gracefully(hass: HomeAssistant, store) -> None:
    """Sync is skipped when the follower coordinator has no data."""
    leader = _make_coordinator(LEADER_ENTRY_ID, LEADER_USERS)
    follower = _make_coordinator(FOLLOWER_ENTRY_ID, [])
    follower.data = None  # Simulate offline/error state
    _make_entries(hass, leader, follower)

    sync = UserSyncManager(hass, store)
    await sync._on_leader_update(LEADER_ENTRY_ID, FOLLOWER_ENTRY_ID)

    follower.client.create_user.assert_not_called()


@pytest.mark.asyncio
async def test_leader_offline_skips_gracefully(hass: HomeAssistant, store) -> None:
    """Sync is skipped when the leader coordinator has no data."""
    leader = _make_coordinator(LEADER_ENTRY_ID, LEADER_USERS)
    leader.data = None  # Simulate offline/error state
    follower = _make_coordinator(FOLLOWER_ENTRY_ID, [])
    _make_entries(hass, leader, follower)

    sync = UserSyncManager(hass, store)
    await sync._on_leader_update(LEADER_ENTRY_ID, FOLLOWER_ENTRY_ID)

    follower.client.create_user.assert_not_called()


@pytest.mark.asyncio
async def test_sync_error_does_not_crash(hass: HomeAssistant, store) -> None:
    """An exception during sync is caught and logged, not propagated."""
    leader = _make_coordinator(LEADER_ENTRY_ID, LEADER_USERS)
    follower = _make_coordinator(FOLLOWER_ENTRY_ID, [])
    follower.client.create_user = AsyncMock(side_effect=Exception("connection refused"))
    _make_entries(hass, leader, follower)

    sync = UserSyncManager(hass, store)
    sync._initialized = True
    # Should not raise
    await sync._reconcile(leader, follower, PAIR_ID)

    # The sync should have attempted and failed gracefully
    assert follower.client.create_user.call_count >= 1


@pytest.mark.asyncio
async def test_initial_reconcile_skips_duplicate_names(hass: HomeAssistant, store) -> None:
    """Duplicate names on leader are skipped during initial reconciliation."""
    leader_users = [
        {"uuid": "L1", "name": "Alice", "pin": "1111", "card": [], "code": [], "validFrom": None, "validTo": None},
        {"uuid": "L2", "name": "Alice", "pin": "2222", "card": [], "code": [], "validFrom": None, "validTo": None},
    ]
    follower_users = [
        {"uuid": "F1", "name": "Alice", "pin": "0000", "card": [], "code": [], "validFrom": None, "validTo": None},
    ]

    leader = _make_coordinator(LEADER_ENTRY_ID, leader_users)
    follower = _make_coordinator(FOLLOWER_ENTRY_ID, follower_users)
    _make_entries(hass, leader, follower)

    sync = UserSyncManager(hass, store)
    await sync._initial_reconcile(leader, follower, PAIR_ID)

    # Neither Alice should be mapped (ambiguous)
    assert len(store.sync_mappings_for(PAIR_ID)) == 0
    follower.client.update_user.assert_not_called()
    follower.client.create_user.assert_not_called()


# ─── New tests for review fixes ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_delete_failure_preserves_mapping(hass: HomeAssistant, store) -> None:
    """When delete_user fails, the sync mapping should NOT be removed (Fix 2)."""
    leader = _make_coordinator(LEADER_ENTRY_ID, [])  # user removed from leader
    follower_users = [{"uuid": "F1", "name": "Alice", "pin": "1111", "card": [], "code": [], "validFrom": None, "validTo": None}]
    follower = _make_coordinator(FOLLOWER_ENTRY_ID, follower_users)
    follower.client.delete_user = AsyncMock(side_effect=Exception("network error"))
    _make_entries(hass, leader, follower)

    store._data["sync_mappings"][PAIR_ID] = {"L1": "F1"}

    sync = UserSyncManager(hass, store)
    sync._initialized = True
    await sync._reconcile(leader, follower, PAIR_ID)

    # Mapping should still exist since delete failed
    assert store.sync_mappings_for(PAIR_ID).get("L1") == "F1"


@pytest.mark.asyncio
async def test_empty_first_poll_defers_initial_reconcile(hass: HomeAssistant, store) -> None:
    """If leader returns 0 users on first poll, _initialized stays False (Fix 13)."""
    leader = _make_coordinator(LEADER_ENTRY_ID, [])  # empty
    follower = _make_coordinator(FOLLOWER_ENTRY_ID, [])
    _make_entries(hass, leader, follower)

    sync = UserSyncManager(hass, store)
    await sync._on_leader_update(LEADER_ENTRY_ID, FOLLOWER_ENTRY_ID)

    assert not sync._initialized
    follower.client.create_user.assert_not_called()


@pytest.mark.asyncio
async def test_stale_follower_mapping_removed(hass: HomeAssistant, store) -> None:
    """Out-of-band follower deletion removes stale mapping (Fix 20)."""
    leader_users = [{"uuid": "L1", "name": "Alice", "pin": "1111", "card": [], "code": [], "validFrom": None, "validTo": None}]
    leader = _make_coordinator(LEADER_ENTRY_ID, leader_users)
    # Follower has no users — F1 was deleted out-of-band
    follower = _make_coordinator(FOLLOWER_ENTRY_ID, [])
    _make_entries(hass, leader, follower)

    store._data["sync_mappings"][PAIR_ID] = {"L1": "F1"}

    sync = UserSyncManager(hass, store)
    sync._initialized = True
    await sync._reconcile(leader, follower, PAIR_ID)

    # Stale mapping removed; next cycle will re-create
    assert "L1" not in store.sync_mappings_for(PAIR_ID)


@pytest.mark.asyncio
async def test_async_setup_subscribes_listener(hass: HomeAssistant, store) -> None:
    """async_setup registers a listener on the leader coordinator (Fix 18)."""
    leader = _make_coordinator(LEADER_ENTRY_ID, LEADER_USERS)
    follower = _make_coordinator(FOLLOWER_ENTRY_ID, FOLLOWER_USERS)
    _make_entries(hass, leader, follower)

    sync = UserSyncManager(hass, store)
    sync.async_setup()

    leader.async_add_listener.assert_called_once()
    assert len(sync._listeners) == 1


@pytest.mark.asyncio
async def test_async_teardown_unsubscribes(hass: HomeAssistant, store) -> None:
    """async_teardown removes all listeners (Fix 18)."""
    leader = _make_coordinator(LEADER_ENTRY_ID, LEADER_USERS)
    follower = _make_coordinator(FOLLOWER_ENTRY_ID, FOLLOWER_USERS)
    _make_entries(hass, leader, follower)

    sync = UserSyncManager(hass, store)
    sync.async_setup()
    unsub_mock = leader.async_add_listener.return_value

    sync.async_teardown()

    unsub_mock.assert_called_once()
    assert len(sync._listeners) == 0
    assert not sync._initialized
