# repository.ceto

A Kodi add-on repository for ceto's **unofficial, community-made** add-ons. Install the repository once and Kodi will offer the add-ons below and keep them updated through its normal update checks.

> **Disclaimer.** These add-ons are independent community projects. They are not affiliated with, endorsed by, or sponsored by Filmio, the National Film Institute Hungary (NFI), MTVA or MédiaKlikk. Names, logos and content belong to their respective owners. The add-ons do not host or redistribute media and do not bypass DRM, authentication, subscriptions or geographic restrictions. Filmio requires your own account and subscription.

## Add-ons

| Add-on | ID | Notes |
|---|---|---|
| Filmio | `plugin.video.filmio` | Requires a Filmio account. Playback needs Widevine; verified on Android/Fire OS (the add-on's own docs list the tested platforms). |
| MédiaKlikk | `plugin.video.mediaklikk` | Public on-demand programmes of mediaklikk.hu. Uses the same ID as an older community add-on: uninstall that one first. |

## Supported Kodi versions

Kodi 21 "Omega" and newer. The repository add-on uses only the long-standing `xbmc.addon.repository` extension point. Kodi 22 is expected to work but has **not been tested** by the maintainer; see the platform notes of each add-on for what was actually verified.

## Install

1. In Kodi: **Settings → System → Add-ons**, enable **Unknown sources**.
2. Download `repository.ceto-<version>.zip` from the link on <https://cetorepo.szabogabi.com/>.
3. **Settings → Add-ons → Install from zip file** and choose the ZIP.
4. **Settings → Add-ons → Install from repository → Ceto Add-on Repository (unofficial)** and pick an add-on.

Updates arrive through Kodi's normal add-on update check.

## How it works

- Hosted on GitHub Pages under the custom domain `cetorepo.szabogabi.com` (DNS on Cloudflare, CNAME to `ceto.github.io`), no server or database. The old `ceto.github.io/repository.ceto` address redirects there; repository add-on 1.0.0 still points at it, 1.0.1 and later use the custom domain. The published tree lives on the `published` branch and is deployed by GitHub Actions.
- `addons.xml` lists the newest version of each add-on; `addons.xml.md5` is the md5 of the exact published `addons.xml`; ZIPs are `<id>/<id>-<version>.zip`. Old versions are kept, and a published version is never overwritten with different content.
- Add-ons stay in their own repositories. Publishing a **GitHub Release** there triggers the release workflow here. See [docs/MAINTAINERS.md](docs/MAINTAINERS.md).

## Development

```sh
python3 -m unittest discover tests
python3 scripts/kodirepo.py stage --source repository.ceto --id repository.ceto --site /tmp/site
python3 scripts/kodirepo.py verify --site /tmp/site
```

Python 3.8+, standard library only (PyYAML is optional, used by two workflow tests).

## License

GPL-3.0-or-later, see [LICENSE](LICENSE).
