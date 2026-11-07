#!/bin/sh
# Build the installable plugin zip and (optionally) install it into calibre.
#   ./build.sh            -> builds Springer-Metadata.zip
#   ./build.sh install    -> builds and installs into the local calibre
#   ./build.sh test       -> installs and runs the identify tests (needs network)
set -e
cd "$(dirname "$0")"
rm -f Springer-Metadata.zip
(cd springer_metadata && zip -q -r ../Springer-Metadata.zip . -x '__pycache__/*' '*.pyc')
zip -q Springer-Metadata.zip LICENSE
echo "Built Springer-Metadata.zip"
case "$1" in
    install) calibre-customize -a Springer-Metadata.zip ;;
    test) calibre-customize -a Springer-Metadata.zip && calibre-debug -e springer_metadata/__init__.py ;;
esac
