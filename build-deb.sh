#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
VERSION=${VERSION:-0.3.0-1}
STAGE="$ROOT/build/package"
DIST="$ROOT/dist"
rm -rf "$STAGE"
mkdir -p "$STAGE/DEBIAN" "$STAGE/usr/bin" "$STAGE/usr/lib/python3/dist-packages" "$STAGE/usr/share/typix-reader" "$STAGE/usr/share/applications" "$STAGE/usr/share/doc/typix-reader" "$DIST"
cat > "$STAGE/DEBIAN/control" <<CONTROL
Package: typix-reader
Version: $VERSION
Architecture: all
Maintainer: TypixDeck <dev@typixnode.com>
Section: text
Priority: optional
Depends: python3 (>= 3.11), python3-gi, gir1.2-gtk-3.0, gir1.2-gdkpixbuf-2.0, ca-certificates
X-Typix-Compatible-OS: raspios-bookworm,raspios-trixie
Description: Local-first TypixDeck reader
 Native GTK3 reader for TXT, Markdown, EPUB, CBZ, and Calibre OPDS.
 Downloads are user-initiated; documents and progress remain on-device.
CONTROL
printf '%s\n' 'Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/' 'Upstream-Name: typix-reader' > "$STAGE/usr/share/doc/typix-reader/copyright"
cat > "$STAGE/usr/bin/typix-reader" <<'RUNNER'
#!/bin/sh
set -eu
exec /usr/bin/python3 -m typix_reader "$@"
RUNNER
chmod 755 "$STAGE/usr/bin/typix-reader"
cp -R "$ROOT/src/typix_reader" "$STAGE/usr/lib/python3/dist-packages/"
find "$STAGE/usr/lib/python3/dist-packages" -name '__pycache__' -type d -prune -exec rm -rf {} +
install -m 644 "$ROOT/src/typix_reader/typix-reader.css" "$STAGE/usr/share/typix-reader/typix-reader.css"
install -m 644 "$ROOT/packaging/typix-reader.desktop" "$STAGE/usr/share/applications/typix-reader.desktop"
dpkg-deb --root-owner-group --build "$STAGE" "$DIST/typix-reader_${VERSION}_all.deb"
