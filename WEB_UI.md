# CellNote Web MVP

The service binds to `127.0.0.1:8787` and is intentionally not exposed to the public network.

## Start and stop

```bash
cd /ssd/deecamp/cellnotes/cell-note-agent
tmux attach -t cellnote-web
```

The service is started by `run_cellnote_web.sh`. Its log is at
`runs/web-service/server.log`.

```bash
tmux kill-session -t cellnote-web
tmux new-session -d -s cellnote-web -c /ssd/deecamp/cellnotes/cell-note-agent \
  './run_cellnote_web.sh > runs/web-service/server.log 2>&1'
```

## Open from a personal computer

```bash
ssh -L 8787:127.0.0.1:8787 deecamp
```

Open `http://localhost:8787` in a browser while the SSH tunnel remains open.

## Enable Step API routing

```bash
cd /ssd/deecamp/cellnotes/cell-note-agent
cp configs/web.env.example configs/web.env
chmod 600 configs/web.env
```

Set `STEP_API_KEY` in `configs/web.env`, then restart the `cellnote-web` tmux
session. Keep this file out of Git and never put the key in browser code.
