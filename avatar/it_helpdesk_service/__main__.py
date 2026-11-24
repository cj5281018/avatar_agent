import argparse
import json
import os
import secrets
from pathlib import Path

from avatar_contract import manifest_digest
from .store import Store, set_setting

from .actions import ACTIONS, manifest


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("manifest")
    export.add_argument("output", type=Path)
    sub.add_parser("setup-code")
    args = parser.parse_args()
    if args.command == "manifest":
        contract = manifest(ACTIONS)
        args.output.write_text(json.dumps(contract, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(manifest_digest(contract))
    else:
        store = Store(os.environ.get("IT_DATA_DIR", "data/it"))
        code = secrets.token_urlsafe(18)
        with store.connection(write=True) as db:
            set_setting(db, "it.code", store.seal(code))
        print(code)


if __name__ == "__main__":
    main()
