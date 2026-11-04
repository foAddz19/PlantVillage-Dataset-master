"""Inspect/import a local raster MBTiles file; never downloads online tiles."""
import argparse
import json
from pathlib import Path
import sys

from leaflab.offline_maps import OfflineMapError, import_mbtiles, inspect_mbtiles


def main(argv=None):
    # Windows redirected consoles can otherwise default to cp1252 and fail on
    # Thai map names even though validation itself succeeded.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", help="Validate every raster tile and report map coverage.")
    inspect.add_argument("source", type=Path)
    install = commands.add_parser("import", help="Validate and atomically install a local MBTiles map.")
    install.add_argument("source", type=Path)
    install.add_argument("--output", type=Path, default=Path(__file__).resolve().parent / "maps" / "current.mbtiles")
    install.add_argument("--replace", action="store_true", help="Replace only the selected output map, never survey records.")
    args = parser.parse_args(argv)
    try:
        result = (inspect_mbtiles(args.source, validate_tiles=True) if args.command == "inspect"
                  else import_mbtiles(args.source, args.output, replace=args.replace))
    except OfflineMapError as error:
        print(f"Offline map: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
