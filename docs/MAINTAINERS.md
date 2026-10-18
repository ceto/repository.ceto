# Maintainer guide

## One-time setup (manual; the automation cannot do these)

1. **Create the repo** `ceto/repository.ceto` (public) and push this project to `main`.
2. **Pages:** Settings → Pages → Build and deployment → Source: **GitHub Actions**.
3. **Environment:** Settings → Environments → `github-pages` (created by Pages). Optionally add yourself as *Required reviewer* for an approval step before every deployment, and restrict deployment branches to `main` (the workflow file always runs from `main`).
4. **Tokens** (fine-grained personal access tokens, nothing classic, nothing hardcoded):

   | Secret | Where | Scope |
   |---|---|---|
   | `SOURCE_READ_TOKEN` | `repository.ceto` → Settings → Secrets → Actions | Only `plugin.video.filmio` and `plugin.video.mediaklikk`; permission *Contents: Read*. Not needed once both repos are public (the default token then suffices). |
   | `REPOSITORY_DISPATCH_TOKEN` | in **each add-on repo** → Settings → Secrets → Actions | Only `repository.ceto`; permission *Contents: Read and write* (GitHub requires this for `repository_dispatch`). Set an expiry and rotate it. |

   The default `GITHUB_TOKEN` cannot read other private repos or trigger workflows in another repo.
5. **Add the notifier** to each add-on repo: copy [source-repo-workflow.yml](source-repo-workflow.yml) to `.github/workflows/notify-repository.yml` and set `ADDON_ID`.
6. **Keep publishing off until you are ready.** Do *not* set the repository variable `PUBLISH_ENABLED` yet. Without it every run is a **dry run** (see below).

## Making an add-on release-ready (in the add-on repo)

The release workflow refuses to stage an add-on unless:

- `addon.xml` `id` equals the registered ID (`addons.json`) and `version` is `MAJOR.MINOR[.PATCH[.BUILD]]`, numeric only;
- the release tag equals the version (`v0.9.1` or `0.9.1`);
- `<license>` (e.g. `GPL-3.0-or-later`) is present, and a `LICENSE` or `LICENSE.txt` file at the repo root is packaged if it exists;
- the summary/description/disclaimer says the add-on is unofficial (`unofficial` / `nem hivatalos`);
- every file named in `library` and `<assets>` exists;
- nothing secret-looking is in the packaged files (`_local_key.py`, `.env*`, `*.har`, `*.pem`, `*.key`, `*.secret*`) and there are no symlinks.

`<source>`, `<website>` and `<email>` only produce warnings.

## Dry run (default)

Actions → **Release add-on** → *Run workflow*, choose the add-on, give the tag, leave **publish** off. The `stage` job builds the ZIP, validates it, regenerates `addons.xml`/`.md5`, verifies the whole tree, and uploads it as the `staged-site` artifact. Nothing is committed or deployed. The same happens for a release in an add-on repo while `PUBLISH_ENABLED` is unset.

## Publishing a release

1. In the add-on repo bump `version` in `addon.xml` (it must be higher than the published one), update `<news>`, commit.
2. Create a **GitHub Release** with tag `vX.Y.Z` (not a draft; pre-releases are staged only). This runs the notifier, which sends a `repository_dispatch` to `repository.ceto`.
3. `release.yml` then:
   1. checks out the tag, validates `addon.xml` and ID, confirms version = tag;
   2. builds a deterministic ZIP with the single top-level `<id>/` folder;
   3. refuses to proceed if that version is already published with different content, or if the version is not higher than the newest published one (re-running the same release is a harmless no-op);
   4. adds the ZIP, regenerates `addons.xml` and `addons.xml.md5` from the ZIPs only, and verifies everything;
   5. **only if `PUBLISH_ENABLED=true`:** commits the whole tree to the `published` branch in one commit (failing if the branch moved meanwhile), deploys Pages, and checks the live URLs.

Publishing manually: Actions → Release add-on → set **publish** on. It still needs `PUBLISH_ENABLED`.

If a run fails after the push but before Pages finished, re-run it: staging is idempotent and it redeploys.

## Releasing the repository add-on itself

`repository.ceto/addon.xml` in this repo. Bump its version only when URLs or metadata change (Kodi refreshes the repo definition when the version increases), merge to `main`, then run the workflow manually with add-on `repository.ceto`. Changing the Pages URL requires a new repository ZIP that users install again.

## Rollback

Publishing is append-only. To withdraw a bad release, publish a fixed higher version. To remove one, edit the `published` branch by hand (delete the ZIP, then run `python3 scripts/kodirepo.py index --site <checkout>`), commit and re-run the deploy.

## Local commands

```sh
python3 scripts/kodirepo.py validate --source ../plugin.video.filmio --id plugin.video.filmio --tag v0.9.0
python3 scripts/kodirepo.py stage    --source ../plugin.video.filmio --id plugin.video.filmio --tag v0.9.0 --site /tmp/site
python3 scripts/kodirepo.py verify   --site /tmp/site
python3 scripts/kodirepo.py verify   --url https://ceto.github.io/repository.ceto/
```

## Assumptions not yet verified in a real Kodi

- Kodi fetching add-on icons/fanart from `<datadir>/<id>/<asset path>` (the tree provides them; if Kodi ignores them the only effect is default artwork).
- Kodi 22 behaviour.

## Action versions

`checkout@v7`, `setup-python@v7`, `upload-artifact@v7`, `download-artifact@v8`, `upload-pages-artifact@v5`, `deploy-pages@v5`: the latest major releases as of 2026-10 (all Node 24), checked against their release notes; actionlint passes. Pin them to commit SHAs if you want fully reproducible runs.
