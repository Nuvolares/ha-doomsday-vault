# Doomsday Vault

Your preparedness vault in the Home Assistant side panel:

- **Library**: Wikipedia, medical references, repair guides, farming and food
  preservation, US Army manuals, FEMA preparedness guides and a North America
  map, stored on your own disk and usable with no internet connection.
- **Inventory**: your food, water, medical supplies and equipment, with
  expiration dates and photos.
- **Armory**: weapons, ammunition and accessories, synced with WeaponLog.

## Upgrading from Doomsday Library

Doomsday Vault is the new name of Doomsday Library. Home Assistant treats it as
a new app, so it is installed once more. Your downloaded library is kept and is
not downloaded again:

1. If you added or changed sources, copy `sources.yaml` from the old app's
   folder (`app_configs/<id>_doomsday_library`, or `addon_configs/...`) to a
   safe place, for example with the File editor or Samba share app.
2. Uninstall **Doomsday Library**. Your downloads in `/media/doomsday_library`
   stay on the disk.
3. Install and start **Doomsday Vault**. On its first start it moves the
   downloads to `/media/doomsday_vault` (instant, nothing is copied) and finds
   every archive that is already there.
4. If you saved `sources.yaml`, copy it into the new app's folder
   (`app_configs/<id>_doomsday_vault`) and restart the app.
5. Review the Archive tab and press **Initiate download**. Sources that are
   already on disk are not downloaded again.

## First start

1. Start the app and open **Doomsday Vault** from the side panel.
2. The vault opens empty. Review the sources in the **Archive** tab: switch
   off what you don't need and add your own.
3. Press **Initiate download** on the Library tab.

Small archives download first, so the library is useful long before the large
ones (Wikipedia is about 120 GB) finish. Downloads resume where they stopped
if the app restarts or the connection drops.

To stop a download, press **Cancel** next to it in the Archive tab, or use
**Cancel all downloads** on the Status tab. The part already downloaded is
kept: **Update** continues where it stopped and **Purge** deletes it. A
cancelled source is skipped by scheduled updates until you press Update.

## Updates

After the first download, the app checks for new releases on the configured
schedule. Only sources with a newer release are downloaded again. The old
version is deleted once the new one is complete and verified.

- **Kiwix sources**: the newest dated release on download.kiwix.org is used.
- **URL sources**: downloaded again only when the server reports a change
  (ETag / Last-Modified).

Each source can override the schedule (daily, weekly, monthly or manual).

## Options

| Option | Default | Meaning |
|---|---|---|
| `storage_path` | `/media/doomsday_vault` | Where content is stored. Must be under `/media` or `/share`. |
| `update_schedule` | `weekly` | Default update interval: daily, weekly, monthly or manual. |
| `update_hour` | `3` | Hour of the day (0-23) when scheduled checks run. |
| `auto_start_downloads` | `false` | Start the first download without pressing "Initiate download". |
| `min_free_gb` | `25` | Disk space that is always kept free. Downloads that don't fit are skipped. |
| `max_download_mbit` | `0` | Bandwidth limit in Mbit/s (0 = unlimited). |
| `verify_checksums` | `true` | Verify Kiwix downloads against their published SHA-256. |
| `log_level` | `info` | Log detail. |

## Editing sources as a file

The source list lives in `sources.yaml` in this app's config folder
(`app_configs/<id>_doomsday_vault/sources.yaml`, called `addon_configs` on
older installations, reachable with the File editor or Samba share apps). Edit
it there and restart the app, or use the Archive tab. New built-in sources from
app updates are added automatically; built-in sources you deleted stay deleted.

```yaml
sources:
  - id: my-manual
    name: Generator manual
    category: Energy & Engineering
    type: url
    url: https://example.org/generator-manual.pdf
    schedule: monthly
    enabled: true
  - id: se-physics
    name: Physics Q&A
    category: Science & Education
    type: kiwix
    folder: stack_exchange
    book: physics.stackexchange.com_en_all
    enabled: true
```

Kiwix `folder` and `book` come from https://download.kiwix.org/zim/: the
folder is the directory and the book is the file name without the trailing
`_YYYY-MM.zim`.

## Reading

Everything opens inside the side panel, including on the Home Assistant
mobile app:

- Archived pages are shown in the Doomsday Vault design, whatever site they
  came from. Maps, converted PDFs and scanned pages keep their original look,
  because their text is placed at exact positions. To show any source in its
  original design, edit it in the Archive tab and turn off **Show pages in
  the library design**.
- PDFs open in the built-in viewer, exactly as published. Only the viewer's
  toolbar uses the library design.
- Links to the internet are not followed, because the content behind them is
  not stored in the library. A short notice explains this.

## Search

The search box searches every word of every stocked archive (each archive
ships its own full-text index), plus the text of PDFs, TXT and HTML files
added as URL sources, which is indexed page by page.

Opening a result takes you straight to the match:

- **Articles** open scrolled to the passage that matches best, with every
  match highlighted. Use ▲ ▼ to step through them and ✕ to clear them.
- **PDFs** open at the matching page with the words highlighted.

PDFs stored inside an archive are not text-searchable yet. This affects the
Knots, Water, Medicine, Post-Disaster and Food Preparation libraries, which
are collections of PDF books, and parts of US Army Publications. Open them
from the Library tab and use the collection's own list.

## Inventory

The **Inventory** tab lists everything you have stocked, sorted by category
(Food, Water, Medical, Hygiene, Energy & Fuel, Tools, Equipment,
Communication, Clothing & Shelter, Documents, or any category you type in).

- Each item has a quantity and unit, a storage location, purchase and
  expiration dates, notes and up to 6 photos. On a phone, **+ Add photo** can
  use the camera. Photos are scaled down before they are stored.
- The counters at the top show what is expired and what expires within 30
  days. Those items are also listed first under **Use or replace soon**.
  **Expiring** and **Expired** show only those items, soonest first.
- The inventory is stored in the app's `/data` folder, so Home Assistant
  backups include it, and it is copied to a drive when you export (read-only
  there).

## Armory (WeaponLog)

The **Armory** tab mirrors your weapons, ammunition and accessories from
WeaponLog.

**Connect:** in WeaponLog open **Settings** and create an **API token**. (If
the option is missing, an admin has to enable API access for your account
first.) In the Armory tab press **Connect**, enter the WeaponLog address and the
token, and choose how often to sync (default every 60 minutes). **Sync now**
syncs right away.

**When WeaponLog is unreachable** the Armory keeps showing the last synced
copy, with a notice saying since when. You can keep adding, editing and
deleting. Changes are marked "waiting to sync" and are sent automatically as
soon as WeaponLog answers again.

**How changes are merged:**

- A field changed only in WeaponLog takes the WeaponLog value.
- A field changed only here is sent to WeaponLog.
- If the same field changed in both places, the change made here wins. The
  value it replaced is written to the **Sync log**.
- Ammunition counts are merged as differences: if you used 50 rounds here and
  20 were logged in WeaponLog, both are subtracted.
- Something deleted here is deleted in WeaponLog. Something deleted in
  WeaponLog disappears here, unless you changed it here in the meantime; then
  it is added back.

Not synced: range sessions, matches, hunting trips, transfers and invoice
files stay in WeaponLog. Photos show only if your WeaponLog lets API tokens
download them.

The token is stored only inside the app and is never shown again. The Armory is
never copied to an exported drive, because it holds serial numbers.

## Export to a drive (offline copy for Windows)

Copy the library and your inventory to an external drive and use them on any Windows PC, with no
Home Assistant, no internet and nothing to install.

**Connect the drive (Home Assistant 2026.11 or newer):**

1. Format the drive as **exFAT** or **NTFS** on a computer. FAT32 cannot hold
   files over 4 GB, such as Wikipedia, and Windows cannot read ext4.
2. Plug it into the Home Assistant server.
3. Go to **Settings → System → Storage → Add storage → Local disk**, choose
   usage **Media**, give it a name and leave **read-only** off.

A network share (NAS) added as Media or Share storage works too.

**Export:**

1. In the side panel, open **Archive → Export to drive** and pick the drive.
2. Choose what to copy. If it doesn't fit, untick sources or give them
   **Low** priority and press **Fit to drive**, which leaves out Low
   priority first, then Normal, largest first.
3. Press **Copy to drive**. Progress shows in the dialog and on the Status
   tab. Only content that is already downloaded can be exported.

Exporting again to the same drive only copies what changed (new releases,
added sources) and removes what you deselected.

**On the Windows PC:** open the `DoomsdayVault` folder on the drive and
double-click **Start Doomsday Vault.bat**. The library opens in the browser.
Keep the small window open while reading, and close it to stop. If Windows
shows "Windows protected your PC", choose **More info → Run anyway**.
`START HERE.html` explains the same steps and links the PDF documents
directly. The offline copy is read-only.

## Limitations

- Kiwix archives are replaced as whole files when a new release appears, so an
  update briefly needs room for both the old and the new copy.
- Updates need internet access. Without it, checks fail quietly and the
  existing library keeps working.

## Support and license

Report problems on the GitHub repository's issue tracker. The app itself is
MIT licensed.

## Content licenses

All built-in content is openly licensed or public domain (Wikipedia and other
Wikimedia projects: CC BY-SA; Stack Exchange: CC BY-SA; Project Gutenberg:
public domain in the US; US government publications: public domain). Archives
are packaged by the Kiwix project.
