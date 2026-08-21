#!/usr/bin/env python3
"""A stand-in for ffmpeg, so the pipeline can be tested without encoding.

It understands just enough of the two command lines dsbot builds:

* ``-f s16le ... -i pipe:0 ... out``  copies stdin to ``out``
* ``-f concat -i list.txt ... out``   concatenates the files listed in ``list.txt``
"""

import sys
from pathlib import Path


def main() -> int:
    argv = sys.argv[1:]
    out = Path(argv[-1])

    # Mimic the one bit of ffmpeg strictness that actually bites: without a
    # recognisable extension the output container has to be given explicitly.
    input_at = argv.index("-i")
    output_format = "-f" in argv[input_at:]
    if out.suffix != ".mp3" and not output_format:
        print(f"Unable to choose an output format for '{out}'", file=sys.stderr)
        return 234

    if "concat" in argv:
        listing = Path(argv[argv.index("-i") + 1])
        payload = b""
        for line in listing.read_text().splitlines():
            line = line.strip()
            if line.startswith("file '") and line.endswith("'"):
                payload += Path(line[6:-1]).read_bytes()
        out.write_bytes(payload)
    else:
        out.write_bytes(sys.stdin.buffer.read())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
