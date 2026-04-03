"""Persistent HA-side storage for Doorman.

Stores per-user metadata:
  user_links          — 2N UUID → HA User ID (for identity linking)
  notification_targets — 2N UUID → list of notify.* service targets
  sync_mappings       — leader UUID → follower UUID (for cross-device sync)
"""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import STORAGE_KEY, STORAGE_VERSION


def _empty_data() -> dict:
    return {"user_links": {}, "notification_targets": {}, "last_access": {}, "sync_mappings": {}}


class DoormanStore:
    """Persists 2N UUID ↔ HA User ID mappings across restarts."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._data: dict = _empty_data()

    async def async_load(self) -> None:
        """Load data from disk. Call once during integration setup."""
        stored = await self._store.async_load()
        if stored is None:
            self._data = _empty_data()
        else:
            self._data = stored
            # Migrate: add sync_mappings if missing
            if "sync_mappings" not in self._data:
                self._data["sync_mappings"] = {}
                await self._store.async_save(self._data)
            # Migrate: convert flat {uuid: uuid} to nested {pair_id: {uuid: uuid}}
            sm = self._data.get("sync_mappings", {})
            if sm and all(isinstance(v, str) for v in sm.values()):
                self._data["sync_mappings"] = {"_migrated": sm}
                await self._store.async_save(self._data)

    # ------------------------------------------------------------------ #
    # Read                                                                 #
    # ------------------------------------------------------------------ #

    @property
    def user_links(self) -> dict[str, str]:
        """Return the full map of ``{two_n_uuid: ha_user_id}``."""
        return self._data.get("user_links", {})

    def get_ha_user_id(self, two_n_uuid: str) -> str | None:
        """Return the HA User ID linked to a 2N UUID, or None."""
        return self.user_links.get(two_n_uuid)

    def get_two_n_uuid(self, ha_user_id: str) -> str | None:
        """Return the 2N UUID linked to an HA User ID, or None."""
        return next(
            (uuid for uuid, uid in self.user_links.items() if uid == ha_user_id),
            None,
        )

    # ------------------------------------------------------------------ #
    # Write                                                                #
    # ------------------------------------------------------------------ #

    async def link_user(self, two_n_uuid: str, ha_user_id: str) -> None:
        """Link a 2N user to an HA user. Persists immediately."""
        self._data.setdefault("user_links", {})[two_n_uuid] = ha_user_id
        await self._store.async_save(self._data)

    async def unlink_user(self, two_n_uuid: str) -> None:
        """Remove the HA user link for a 2N UUID. Persists immediately."""
        self._data.get("user_links", {}).pop(two_n_uuid, None)
        await self._store.async_save(self._data)

    # ------------------------------------------------------------------ #
    # Last access times                                                    #
    # ------------------------------------------------------------------ #

    @property
    def last_access(self) -> dict[str, str]:
        """Return the full map of ``{two_n_uuid: utcTime}`` for last access."""
        return self._data.get("last_access", {})

    async def update_last_access(self, two_n_uuid: str, utc_time: str) -> None:
        """Record the most recent successful access time for a user. Persists immediately."""
        self._data.setdefault("last_access", {})[two_n_uuid] = utc_time
        await self._store.async_save(self._data)

    # ------------------------------------------------------------------ #
    # Notification targets                                                 #
    # ------------------------------------------------------------------ #

    @property
    def notification_targets(self) -> dict[str, list[str]]:
        """Return the full map of ``{two_n_uuid: [notify.* targets]}``."""
        return self._data.get("notification_targets", {})

    def get_notification_targets(self, two_n_uuid: str) -> list[str]:
        """Return the list of notify.* targets for a 2N UUID, or []."""
        return self.notification_targets.get(two_n_uuid, [])

    async def set_notification_targets(self, two_n_uuid: str, targets: list[str]) -> None:
        """Persist the notification targets for a 2N user."""
        self._data.setdefault("notification_targets", {})[two_n_uuid] = targets
        await self._store.async_save(self._data)

    # ------------------------------------------------------------------ #
    # Sync mappings — scoped per sync pair                                 #
    # Format: {leader_entry_id: {leader_uuid: follower_uuid}}              #
    # ------------------------------------------------------------------ #

    def sync_mappings_for(self, pair_id: str) -> dict[str, str]:
        """Return ``{leader_uuid: follower_uuid}`` for a specific sync pair."""
        return self._data.get("sync_mappings", {}).get(pair_id, {})

    async def set_sync_mapping(
        self, pair_id: str, leader_uuid: str, follower_uuid: str
    ) -> None:
        """Store a leader→follower UUID mapping. Persists immediately."""
        self._data.setdefault("sync_mappings", {}).setdefault(pair_id, {})[
            leader_uuid
        ] = follower_uuid
        await self._store.async_save(self._data)

    async def remove_sync_mapping(self, pair_id: str | None = None, leader_uuid: str = "") -> None:
        """Remove a leader→follower UUID mapping. Persists immediately.

        If ``pair_id`` is None, searches all pairs for the leader_uuid (for service-level cleanup).
        """
        sm = self._data.get("sync_mappings", {})
        if pair_id is not None:
            sm.get(pair_id, {}).pop(leader_uuid, None)
        else:
            for sub in sm.values():
                sub.pop(leader_uuid, None)
        await self._store.async_save(self._data)

    def get_leader_uuid_for_follower(self, follower_uuid: str) -> str | None:
        """Reverse lookup across all pairs: return the leader UUID for a follower UUID."""
        for sub in self._data.get("sync_mappings", {}).values():
            for lid, fid in sub.items():
                if fid == follower_uuid:
                    return lid
        return None

    async def clear_sync_mappings(self, pair_id: str | None = None) -> None:
        """Remove sync mappings. If pair_id given, only that pair; otherwise all."""
        sm = self._data.get("sync_mappings", {})
        if pair_id is not None:
            sm.pop(pair_id, None)
        else:
            self._data["sync_mappings"] = {}
        await self._store.async_save(self._data)
