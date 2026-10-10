# Datasheet Parser landing

A buildless Persian case-study site. Self-hosted fonts, HTML/CSS/JS and report downloads;
scrolling only plays an illustrative CLI replay. It never invokes extraction or an LLM.

Run locally from the repository:

```powershell
uv run python -m http.server 4173 --bind 127.0.0.1 --directory landing
```

Open `http://127.0.0.1:4173`. There is no build or dependency installation.

The report is scoped to run `ce40e4a85acc4eb5` and the unapproved MAX3232CD reference.
Aligned pin/topology checks are limited comparisons, not overall accuracy. Tokens and
elapsed inference time come from successful unique checkpoints; the reported wall span
includes interruptions. Dollar API cost was not recorded. Source and inference telemetry
remain independent from the browser. `reports/comparison.json` preserves both input hashes.

The reference PDF excerpts are the supplied MAX3232 source, pages 6 and 11. They are included
for evidence inspection, not as newly authored diagrams. The hero chip graphic is illustrative.

The decision-model accordion links its official model card. Fine-tuning, per-role model
routing and automatic reviewer corrections are future work, not implemented features.

Deployment uses a standalone systemd service on dana. Its static document root is isolated
under `/opt/datasheet-landing`; it has no Mongo connection, API keys or repository secrets.

Live case study: http://185.243.48.232:8094

The new [live PDF sample](http://185.243.48.232:8095) runs separately on port 8095.
It selects the first catalog entry and displays extraction/blind/review progress.
Its frontend is `demo.html`, `demo.css`, `demo.js`; the backend is **not** part of the
static landing server. See [`../docs/web-demo.md`](../docs/web-demo.md) for details.

The service runs as `nobody`, with `MemoryMax=64M` and `CPUQuota=10%`. Its unit and
stdlib-only static server are in `deploy/`. Publish only `index.html`, `styles.css`,
`app.js`, `assets/` and `reports/` into the release directory. Keep `deploy/` outside
the public document root. No existing nginx configuration or Docker service is changed.

Health: `GET /healthz`. On dana, inspect using:

```sh
systemctl status datasheet-landing.service
journalctl -u datasheet-landing.service --since today
```

For an update, create a new release under `/opt/datasheet-landing/releases/`, verify
its assets, then replace the `current` symlink atomically. Roll back by pointing it
at the previous release. Static-content updates do not require restarting any service.

Vazirmatn fonts are distributed under the SIL Open Font License in `assets/OFL.txt`.
