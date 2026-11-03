# Changelog

## 0.5.1

- Fix: Armory sync failed with "WeaponLog redirected the request" when the
  address was entered as `weaponlog.com`. WeaponLog only answers at
  `www.weaponlog.com`; the address is now corrected automatically when a
  redirect only adds or drops "www.", and your Armory records are kept. Other
  redirects still stop the sync, so the API token is never sent elsewhere,
  and the message now says where the request was sent.
- The Armory now holds only weapons you own. Rentals and sold weapons are no
  longer copied from WeaponLog, and ones already copied are removed from the
  Armory (they stay in WeaponLog). The Status field offers Owned and Sold;
  marking a weapon sold updates WeaponLog and removes it from the Armory. The
  Rental cost field is gone.

## 0.5.0

- **New name: Doomsday Vault** (was Doomsday Library), with a new logo. Home
  Assistant installs it as a new app; see "Upgrading from Doomsday Library" in
  the documentation. On first start the downloaded library is moved from
  `/media/doomsday_library` to `/media/doomsday_vault` and archives already
  on disk are used instead of being downloaded again.
- New: **Inventory** tab for food, water, medical supplies and equipment, by
  category, with quantity, storage location, purchase and expiration dates,
  notes and photos. Expired items and items expiring within 30 days are
  listed first. Included in drive exports (read-only there).
- New: **Armory** tab synced with WeaponLog (weapons, ammunition,
  accessories). Works from a local copy when WeaponLog is unreachable;
  changes made meanwhile are sent automatically later. Changes merge field
  by field, the change made here wins when both sides changed the same field,
  and ammo counts merge as differences. Never included in drive exports.
- Exported drives now use the folder `DoomsdayVault` and the launcher
  `Start Doomsday Vault.bat`. An earlier copy on the same drive is renamed and
  updated in place on the next export.

## 0.4.2

- New: **Cancel** downloads. A running or queued download has a Cancel button
  in the Archive tab, and the Status tab has Cancel and "Cancel all
  downloads". What was already downloaded is kept: press Update to continue
  where it stopped, or Purge to delete it. Scheduled updates skip a cancelled
  source until you press Update.
- Fix: restyled pages could look broken. Body text now uses a regular font at
  the page's own size instead of a wide monospace font and enlarged headings,
  so text no longer overflows its boxes or runs into pictures.
- Fix: light text on white boxes. Backgrounds that a site forces to stay
  white are cleared, and forced dark text is brightened.
- Pages that place text at exact positions over page images (converted PDFs
  and scans) keep their original design.

## 0.4.1

- PDFs are always shown exactly as published. The "Terminal" color mode is
  removed; search highlighting and opening at the matching page remain.

## 0.4.0

- New: **Export to drive** (Archive tab). Copies the library to a drive
  connected to Home Assistant as a portable app for Windows: double-click
  `Start Doomsday Vault.bat` on any Windows PC to use the same library,
  search and PDF viewer offline, with nothing to install.
  - Lists the connected drives with free space and warns about FAT32 and
    file systems Windows cannot read.
  - If the selection does not fit, deselect sources or set priorities and
    press "Fit to drive".
  - Exporting again only copies what changed and removes what you deselected.
- Fix: a freshly downloaded source could show "ready" a moment before it
  could be opened.
- Slightly smaller sub-headings in restyled pages.

## 0.3.0

- Search results now land on the right spot: articles open scrolled to the
  passage that matches best, with every match highlighted and ▲ ▼ buttons to
  step through them.
- PDFs and text files are indexed page by page. A search result opens the PDF
  at the matching page with the words highlighted. The existing index is
  rebuilt automatically after the update.
- PDF "Terminal" mode keeps photos and pictures in their original colors and
  recolors only text and drawings. Full-page scans are still recolored.

## 0.2.3

- Fix: garbled pages could still appear after 0.2.2 because browsers kept
  their cached copies. Archive pages now live under a new address, so old
  copies are never used, and restyled pages are no longer cached.

## 0.2.2

- Fix: archived pages showed unreadable characters. The library server sent
  compressed pages that were restyled without being decompressed first.

## 0.2.1

- Compatibility fix: use the `addon_config` folder mapping, which every
  Supervisor version understands, so the update shows up on older systems.

## 0.2.0

- All archived pages (Wikipedia, Stack Exchange, manuals ...) now use the
  Doomsday Vault design. Maps keep their original look. Turn it off per
  source with "Show pages in the library design".
- PDFs open in a built-in viewer inside the panel instead of the browser,
  so the Home Assistant mobile app never leaves the app. Includes a
  "Terminal" color mode and an "Original" mode.
- Links that point to the internet are blocked with a notice instead of
  leaving the app. Links that would open a new window open in the reader.
- The reader has a Back button for navigating between pages.
- Removed the CRT scanline overlay.
- Updated for the current Home Assistant app format: the base image is set in
  the Dockerfile (build.yaml removed), the new `map` syntax, option
  translations, and only the Supervisor's ingress proxy may connect.

## 0.1.0

- First release: side-panel library, built-in survival catalog (about 210 GB
  enabled by default), North America offline map, unified search, scheduled
  delta updates, resumable downloads.
