/* Upgrade existing browser settings and Android bridges before app scripts run. */
(function (global) {
  "use strict";

  function migrateStorage(storage) {
    const marker = "obsidian_brand_migrated_v1";
    if (storage.getItem(marker) === "1") return;
    const keys = [];
    for (let i = 0; i < storage.length; i++) keys.push(storage.key(i));
    for (const oldKey of keys) {
      if (!oldKey || !oldKey.startsWith("aion_")) continue;
      const newKey = "obsidian_" + oldKey.slice("aion_".length);
      if (storage.getItem(newKey) === null) {
        storage.setItem(newKey, storage.getItem(oldKey));
      }
    }
    // Keep original values for rollback; the marker prevents cleared settings
    // from being resurrected on the next load.
    storage.setItem(marker, "1");
  }

  for (const name of ["localStorage", "sessionStorage"]) {
    try { migrateStorage(global[name]); } catch (_) { /* Storage may be disabled. */ }
  }
  for (const suffix of ["Audio", "Notifications", "Ble", "Adv", "Muse"]) {
    const current = "Obsidian" + suffix;
    const legacy = "Aion" + suffix;
    if (!global[current] && global[legacy]) global[current] = global[legacy];
  }
})(window);
