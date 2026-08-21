#!/usr/bin/env python3
"""A stand-in for ffmpeg, so the pipeline can be tested without encoding.

It understands just enough of the command lines dsbot builds:

* ``-f s16le ... -i pipe:0 ... -f adts out``  copies stdin to ``out``
* ``-i joined.aac -c copy -f ipod out``       copies the input file to ``out``

It also reproduces the one bit of ffmpeg strictness that has actually bitten us:
when the output filename has no extension ffmpeg recognises, the container must
be named explicitly or it refuses to start.  dsbot writes to ``.part`` files, so
a double that shrugged this off would hide a completely broken encoder.
"""

import sys
from pathlib import Path

KNOWN_EXTENSIONS = {".mp3", ".m4a", ".aac", ".wav", ".ogg"}


def main() -> int:
    argv = sys.argv[1:]
    out = Path(argv[-1])

    input_at = argv.index("-i")
    if out.suffix not in KNOWN_EXTENSIONS and "-f" not in argv[input_at:]:
        print(f"Unable to choose an output format for '{out}'", file=sys.stderr)
        return 234

    source = argv[input_at + 1]
    if source == "pipe:0":
        out.write_bytes(sys.stdin.buffer.read())
    else:
        out.write_bytes(Path(source).read_bytes())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
