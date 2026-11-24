# Portable AppImage build

`HifiMagic-<version>-x86_64.AppImage` is a single executable containing:

- a Python 3.11 runtime (manylinux2014, so it runs on most x86-64 Linux distributions),
- the controller source files and browser UI,
- a desktop entry and icon.

No Python, pip or system packages are needed on the target machine.

## Build it yourself

```bash
./build-appimage.sh
```

The script downloads (once, into `build/`) a Python AppImage runtime and
`appimagetool`, extracts them, assembles `build/AppDir` and produces the final
AppImage in the project directory. It needs only `curl`, `tar`, `gzip` and
coreutils. To force fresh downloads, delete the files in `build/` first.

## Run it

```bash
chmod +x HifiMagic-<version>-x86_64.AppImage
./HifiMagic-<version>-x86_64.AppImage
```

It opens the UI at <http://127.0.0.1:8080> in your default browser. Useful
arguments work the same as for the Python entry point:

```bash
./HifiMagic-<version>-x86_64.AppImage --host 192.168.1.20 --port 9000 --no-browser
```

The player address defaults to `192.168.1.20`. Pass `--host` if your device
has a different IP.

## Portability notes

- App Images need FUSE to be mounted. If FUSE is unavailable, run the extracted
  form instead:
  `./HifiMagic-<version>-x86_64.AppImage --appimage-extract` then
  `./squashfs-root/AppRun`.
- To install into an applications menu, copy the AppImage somewhere permanent
  and point `cxn100.desktop` at it (edit the `Exec=` and `Icon=` lines).
- Only the local HTTP port is opened (`127.0.0.1` by default). Use
  `--bind 0.0.0.0` only if you intentionally want to control the player from
  other machines on your LAN.

## Rebuilding the icon

```bash
python3 tools/make_icon.py
```

This regenerates `icon.png` with the standard library only.
